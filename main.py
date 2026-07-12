#!/usr/bin/env python3
"""Vflow - Voice-to-text desktop tool powered by Groq Whisper."""

# IMPORTANTE — Fix de conflicto OpenMP entre ctranslate2 y PyQt6 (Windows).
#
# ctranslate2 (usado por faster-whisper) incluye su propio libiomp5md.dll
# (runtime Intel OpenMP).  Cuando PyQt6 carga primero sus DLLs de Qt y el
# runtime MSVC inicializa TLS/mutexes globales, un segundo intento de
# inicializar el runtime OpenMP de Intel desde ctranslate2 provoca un Access
# Violation (exit code 0xC0000005) que mata el proceso silenciosamente.
#
# Solución: importar ctranslate2 ANTES de cualquier import de PyQt6 para que
# sea ctranslate2 quien registre primero su runtime OpenMP.  Esta importación
# es un no-op en equipos donde faster-whisper no está instalado (se ignora
# silenciosamente), por lo que no afecta la ruta Groq.
try:
    import ctranslate2  # noqa: F401 — pre-carga libiomp5md.dll antes de Qt
except ImportError:
    pass  # faster-whisper no instalado; modo Groq no se ve afectado

import ctypes
import logging
import logging.handlers
import math
import os
import struct
import sys
import signal
import subprocess
import threading
import time
import webbrowser
import winreg
import winsound

# onnxruntime DEBE importarse ANTES que PyQt6: con el orden inverso su DLL de
# pybind falla al inicializar ("DLL initialization routine failed") y el VAD
# de Silero (dictado apply_vad + métricas de reunión) queda en fail-open
# silencioso. Reproducción: python -c "from PyQt6.QtCore import QObject; import onnxruntime".
try:
    import onnxruntime  # noqa: F401
except Exception:  # noqa: BLE001 — sin onnxruntime el VAD hace fail-open igual que antes
    pass

from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu,
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox,
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QAction

from dotenv import set_key, unset_key
from ui.pill_widget import PillWidget
from ui.hud_widget import HudWidget
from core.recorder import AudioRecorder
from core.transcriber import Transcriber
from core.hotkey import HotkeyListener
from core.meeting import MEETING
from core.clipboard import paste_text, copy_text, save_frontmost_app, get_saved_exe
from core import dictation_modes
from core.secrets import encrypt
from core import proactive as _proactive
from core.proactive import PROACTIVE, LullDetector, MonologueWatch
from core import assistant as _assistant
from db.database import TranscriptionDB
from web.server import start_web_server
from config import LOGO_PATH, APP_DATA_DIR, CHUNK_SECONDS, MAX_RECORDING_SECONDS, APP_VERSION

logger = logging.getLogger(__name__)

# El handle del mutex debe vivir durante todo el proceso para garantizar instancia única.
_MUTEX_HANDLE = None


def _setup_logging():
    """Configura logging a archivo rotativo en APP_DATA_DIR/vflow.log."""
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    log_path = os.path.join(APP_DATA_DIR, "vflow.log")
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=500_000,
        backupCount=2,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _migrate_plaintext_key():
    """Migra una GROQ_API_KEY en texto plano existente en .env al formato DPAPI cifrado.

    Si el .env contiene 'GROQ_API_KEY' en texto plano (valor que empieza con 'gsk_')
    y no existe 'GROQ_API_KEY_ENC', cifra el valor y actualiza el .env en el acto.
    No aborta la aplicación si falla — solo registra el error.
    """
    from dotenv import dotenv_values
    env_path = os.path.join(APP_DATA_DIR, ".env")
    if not os.path.exists(env_path):
        return
    try:
        values = dotenv_values(env_path)
        plain_key = values.get("GROQ_API_KEY", "")
        already_encrypted = values.get("GROQ_API_KEY_ENC", "")
        if plain_key.startswith("gsk_") and not already_encrypted:
            enc = encrypt(plain_key)
            set_key(env_path, "GROQ_API_KEY_ENC", enc)
            try:
                unset_key(env_path, "GROQ_API_KEY")
            except Exception:
                pass
            logger.info("Migración completada: GROQ_API_KEY cifrada con DPAPI en .env")
    except Exception as e:
        logger.warning("_migrate_plaintext_key: no se pudo migrar la clave — %s", e)


def _generate_beep_wav(freq: int, duration_ms: int, volume: float = 0.009) -> bytes:
    """Genera un tono WAV mono 16-bit PCM en memoria (sin archivo temporal)."""
    sample_rate = 44100
    num_samples = int(sample_rate * duration_ms / 1000)
    pcm = bytearray()
    for i in range(num_samples):
        fade = 1.0 - (i / num_samples) ** 2  # atenuación cuadrática para evitar click
        sample = int(32767 * volume * fade * math.sin(2 * math.pi * freq * i / sample_rate))
        pcm += struct.pack('<h', max(-32768, min(32767, sample)))
    data_size = len(pcm)
    header  = struct.pack('<4sI4s', b'RIFF', 36 + data_size, b'WAVE')
    header += struct.pack('<4sIHHIIHH', b'fmt ', 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += struct.pack('<4sI', b'data', data_size)
    return bytes(header) + bytes(pcm)


def _play_sound(freq: int, duration_ms: int = 120):
    """Reproduce un tono sintetizado por la salida de audio (no el altavoz del PC).

    Usa winsound.PlaySound con SND_MEMORY para compatibilidad con Windows 10
    donde el dispositivo Beep está deshabilitado. Se ejecuta en un hilo daemon.
    """
    if os.getenv("SOUNDS_ENABLED", "true") != "true":
        return
    try:
        volume = int(os.getenv("BEEP_VOLUME_STEPS", "2")) * 0.0045
        wav = _generate_beep_wav(freq, duration_ms, volume=volume)
        threading.Thread(
            target=lambda: winsound.PlaySound(wav, winsound.SND_MEMORY),
            daemon=True,
        ).start()
    except Exception:
        pass


_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REGISTRY_APP_NAME = "Vflow"

# Número de comprobaciones consecutivas (cada 1 s) sin avance de muestras antes de
# declarar que el micrófono se desconectó a mitad de grabación.
_MIC_STALL_LIMIT = 2

# Presupuesto TOTAL de gracia (segundos) para esperar a que terminen los workers
# de chunk en vuelo antes de ensamblar el texto final del dictado largo (unidad
# 0.2). El timeout de la API Groq por chunk es 10s: un worker vivo termina o
# falla dentro de esta ventana; si sigue vivo tras la gracia, se asume perdido.
_CHUNK_JOIN_GRACE_SECONDS = 12.0
# Intervalo de sondeo dentro del join: mantiene la respuesta rápida al abort
# por cambio de generación sin bloquear en un único join() largo por thread.
_CHUNK_JOIN_POLL_SECONDS = 0.2

# Unidad 1.2: WAV de la última grabación fallida (ver _transcribe_final). Se
# sobrescribe en cada fallo nuevo (solo existe el último), así que un TTL al
# arrancar + borrado tras el siguiente dictado exitoso bastan para que la voz
# en claro no quede en disco indefinidamente si Groq falló durante algo
# confidencial.
FAILED_RECORDING_PATH = os.path.join(APP_DATA_DIR, "last_failed_recording.wav")
_FAILED_WAV_TTL_HOURS = 24


def _cleanup_stale_failed_wav(path: str = FAILED_RECORDING_PATH, max_age_hours: float = _FAILED_WAV_TTL_HOURS):
    """Borra `last_failed_recording.wav` al arrancar si supera el TTL (best-effort).

    Se llama una vez en main(), antes de crear cualquier componente. Un fallo
    aquí nunca debe abortar el arranque de la app — solo se registra en el log.
    """
    try:
        if not os.path.exists(path):
            return
        age_seconds = time.time() - os.path.getmtime(path)
        if age_seconds > max_age_hours * 3600:
            os.remove(path)
            logger.info(
                "last_failed_recording.wav eliminado por TTL (>%dh sin uso): %s",
                max_age_hours, path,
            )
    except OSError as e:
        logger.warning("No se pudo evaluar/eliminar last_failed_recording.wav por TTL: %s", e)


def _cleanup_failed_wav(path: str = FAILED_RECORDING_PATH):
    """Borra `last_failed_recording.wav` tras un dictado exitoso (best-effort).

    Se llama desde `_on_transcription_done`, el único punto de éxito que sigue
    a `_transcribe_final` (dictado normal Y traducción — ambos flujos son los
    que escriben este WAV al fallar; ver el except de `_transcribe_final`).
    Nunca debe romper el flujo de pegado/guardado si la eliminación falla.
    """
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info("last_failed_recording.wav eliminado tras dictado exitoso: %s", path)
    except OSError as e:
        logger.warning("No se pudo eliminar last_failed_recording.wav tras éxito: %s", e)


def _join_pending_chunks(threads, gen, get_generation, grace_seconds=_CHUNK_JOIN_GRACE_SECONDS):
    """Espera, con presupuesto acotado, a que terminen los workers de chunk en vuelo.

    Diseño (unidad 0.2, debate cerrado — implementar tal cual): el dictado largo
    por chunks (`_flush_chunk`) lanza un thread por tramo. Si el tramo final
    (`_transcribe_final`) le gana la carrera a un worker anterior que sigue
    esperando a la API (Groq lenta), su texto quedaba huérfano en
    `self._chunk_results` y se perdía en silencio al ensamblar. Esta función se
    llama ANTES de leer `_chunk_results`, con una gracia corta, y aborta de
    inmediato sin alarma si la generación cambió durante la espera (el usuario
    ya inició un dictado nuevo y los workers viejos se descartan solos por su
    propio check de gen).

    threads: threads ya lanzados para la generación `gen` (snapshot tomado por
        el caller bajo `_chunk_state_lock`).
    gen: generación a la que pertenecen esos threads.
    get_generation: callable sin argumentos que devuelve la generación vigente.
    grace_seconds: presupuesto TOTAL de espera (no por-thread).

    Devuelve True si, agotada la gracia, algún thread de `gen` seguía vivo (un
    chunk potencialmente perdido) — NUNCA por un hueco de índice en
    `_chunk_results` (un chunk sin texto detectado, `if text:` en
    `_chunk_worker`, es silencio legítimo, no una pérdida).
    Devuelve False si todos terminaron a tiempo, o si el join se abortó porque
    la generación cambió durante la espera.
    """
    deadline = time.monotonic() + grace_seconds
    for t in threads:
        while t.is_alive():
            if get_generation() != gen:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(min(_CHUNK_JOIN_POLL_SECONDS, remaining))
        if get_generation() != gen:
            return False
    return any(t.is_alive() for t in threads)


# ---------------------------------------------------------------------------
# Diálogo de primera ejecución
# ---------------------------------------------------------------------------
class FirstRunDialog(QDialog):
    """Mostrado cuando GROQ_API_KEY falta en el primer arranque."""

    def __init__(self):
        """Construye el diálogo con campo de entrada para la API key y botón de guardar."""
        super().__init__()
        self.setWindowTitle("Vflow - Setup")
        self.setFixedWidth(420)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Ingresa tu Groq API Key para transcripciones:"))

        link = QLabel('<a href="https://console.groq.com/keys">Obtener gratis en console.groq.com/keys</a>')
        link.setOpenExternalLinks(True)
        layout.addWidget(link)

        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("gsk_...")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.key_input)

        save_btn = QPushButton("Guardar y continuar")
        save_btn.clicked.connect(self._save_key)
        layout.addWidget(save_btn)

        self.setLayout(layout)

    def _save_key(self):
        """Valida la API key, la cifra con DPAPI y la guarda en .env como GROQ_API_KEY_ENC."""
        key = self.key_input.text().strip()
        if not key.startswith("gsk_") or len(key) < 20:
            QMessageBox.warning(self, "Error", "La clave debe comenzar con 'gsk_' y tener al menos 20 caracteres.")
            return

        env_path = os.path.join(APP_DATA_DIR, ".env")
        os.makedirs(APP_DATA_DIR, exist_ok=True)

        # Cifrar con DPAPI y escribir solo el blob cifrado en .env (nunca texto plano)
        enc = encrypt(key)
        set_key(env_path, "GROQ_API_KEY_ENC", enc)

        # Eliminar cualquier clave legacy en texto plano del .env
        try:
            unset_key(env_path, "GROQ_API_KEY")
        except Exception:
            pass

        # Establecer en el proceso actual para que Transcriber lo detecte
        os.environ["GROQ_API_KEY"] = key
        self.accept()


# ---------------------------------------------------------------------------
# Inicio automático con Windows (Registro)
# ---------------------------------------------------------------------------
def _expected_run_command() -> str:
    """Devuelve el comando que debe escribirse en el registro para el inicio automático.

    En modo bundle (.exe frozen) la ruta del ejecutable va entre comillas para
    soportar rutas con espacios (ej. 'C:\\Program Files\\Vflow\\Vflow.exe').
    En modo dev se incluyen intérprete y script, también entre comillas.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'


def _is_launch_at_login() -> bool:
    """Verifica si Vflow está configurado para iniciar con Windows (registro)."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _REGISTRY_APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


def _set_launch_at_login(enabled: bool):
    """Activa o desactiva el inicio automático de Vflow con Windows vía registro."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, _REGISTRY_APP_NAME, 0, winreg.REG_SZ, _expected_run_command())
        else:
            try:
                winreg.DeleteValue(key, _REGISTRY_APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        logger.error("Error al configurar inicio con Windows: %s", e)


def _repair_launch_at_login():
    """Repara la clave de registro si el inicio automático apunta a una ruta desactualizada.

    Si el inicio con Windows está activado pero el valor almacenado en el registro
    no coincide con la ubicación actual del ejecutable, reescribe la clave con el
    comando correcto. Esto cubre el caso de que el usuario haya movido o reinstalado
    el .exe. Toda la función está envuelta en try/except — nunca aborta el arranque.
    """
    try:
        if not _is_launch_at_login():
            return  # No activado: nada que reparar
        expected = _expected_run_command()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY, 0, winreg.KEY_READ)
        current_value, _ = winreg.QueryValueEx(key, _REGISTRY_APP_NAME)
        winreg.CloseKey(key)
        if current_value != expected:
            logger.info(
                "Auto-reparación de inicio con Windows: '%s' → '%s'",
                current_value,
                expected,
            )
            _set_launch_at_login(True)
    except Exception as e:
        logger.warning("_repair_launch_at_login: no se pudo comprobar/reparar la clave — %s", e)


# ---------------------------------------------------------------------------
# Fuente de audio — toggle desde la bandeja
# ---------------------------------------------------------------------------

def _set_audio_source_env(source: str):
    """Guarda AUDIO_SOURCE en .env y en el entorno del proceso en ejecución."""
    from dotenv import set_key as _set_key
    env_path = os.path.join(APP_DATA_DIR, ".env")
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    _set_key(env_path, "AUDIO_SOURCE", source)
    os.environ["AUDIO_SOURCE"] = source


# ---------------------------------------------------------------------------
# Bandeja del sistema
# ---------------------------------------------------------------------------
def _setup_tray(app: QApplication, port: int, vflow: "VflowApp") -> QSystemTrayIcon:
    """Crea el icono de bandeja del sistema con menú de dashboard, auto-inicio y salir."""
    pixmap = QPixmap(LOGO_PATH)
    if pixmap.isNull():
        icon = QIcon()
    else:
        icon = QIcon(pixmap.scaled(22, 22, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    tray = QSystemTrayIcon(icon, app)

    menu = QMenu()

    status = QAction(f"Vflow v{APP_VERSION} - Activo", menu)
    status.setEnabled(False)
    menu.addAction(status)
    menu.addSeparator()

    dashboard = QAction(f"Abrir Dashboard (:{port})", menu)
    dashboard.triggered.connect(lambda: subprocess.run(["cmd", "/c", "start", f"http://localhost:{port}"], capture_output=True))
    menu.addAction(dashboard)

    meeting_window = QAction("Abrir ventana de reunión", menu)
    meeting_window.triggered.connect(lambda: subprocess.run(["cmd", "/c", "start", f"http://localhost:{port}/reunion"], capture_output=True))
    menu.addAction(meeting_window)
    menu.addSeparator()

    login_action = QAction("Iniciar con Windows", menu)
    login_action.setCheckable(True)
    login_action.setChecked(_is_launch_at_login())
    login_action.toggled.connect(_set_launch_at_login)
    menu.addAction(login_action)
    menu.addSeparator()

    # Fuente de audio (checkable, estilo radio)
    current_source = os.getenv("AUDIO_SOURCE", "mic")
    src_mic = QAction("Fuente: Micrófono", menu)
    src_mic.setCheckable(True)
    src_mic.setChecked(current_source == "mic")
    src_sys = QAction("Fuente: Audio del sistema", menu)
    src_sys.setCheckable(True)
    src_sys.setChecked(current_source == "system")

    def _on_src_mic(checked):
        if checked:
            _set_audio_source_env("mic")
            src_sys.setChecked(False)

    def _on_src_sys(checked):
        if checked:
            _set_audio_source_env("system")
            src_mic.setChecked(False)

    src_mic.toggled.connect(_on_src_mic)
    src_sys.toggled.connect(_on_src_sys)
    menu.addAction(src_mic)
    menu.addAction(src_sys)
    menu.addSeparator()

    # Modo reunión (captura dual mic + sistema). Es un MODO, no una fuente de
    # dictado: comparte la misma vía que el hotkey AltGr+R (toggle iniciar/terminar).
    meeting_action = QAction("Iniciar reunión (AltGr+R)", menu)

    def _on_meeting_action():
        vflow.hotkey.meeting_toggle.emit()

    def _refresh_meeting_label():
        meeting_action.setText("Terminar reunión" if MEETING.is_active() else "Iniciar reunión (AltGr+R)")

    meeting_action.triggered.connect(_on_meeting_action)
    # Actualizar el texto del item al abrir el menú (refleja el estado real)
    menu.aboutToShow.connect(_refresh_meeting_label)
    menu.addAction(meeting_action)
    menu.addSeparator()

    quit_action = QAction("Salir", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.setToolTip(f"Vflow v{APP_VERSION} - Voice to Text")
    tray.show()
    return tray


# ---------------------------------------------------------------------------
# Controlador principal de la aplicación
# ---------------------------------------------------------------------------
class VflowApp(QObject):
    """Controlador principal. Conecta hotkey -> recorder -> transcriber -> clipboard."""

    transcription_done = pyqtSignal(str, float, int)   # text, duration, generation
    transcription_error = pyqtSignal(str, int)          # error_msg, generation
    paste_finished = pyqtSignal(str)                    # "pasted" | "clipboard_only" | "failed"
    meeting_stopped = pyqtSignal(object)                # dict resultado de MEETING.stop()
    lost_answer_ready = pyqtSignal(dict)                # resultado de answer_live() (unidad 5.3)
    chunk_loss_warning = pyqtSignal(int)                 # generación; un chunk se perdió tras la gracia (unidad 0.2)
    transcription_fallback_notice = pyqtSignal(str)      # unidad 5.5: aviso de fallback de red Groq -> local

    def __init__(self):
        """Inicializa componentes (recorder, transcriber, DB, hotkey, pill) y conecta señales."""
        super().__init__()
        self.recorder = AudioRecorder(source=os.getenv("AUDIO_SOURCE", "mic"))
        self.transcriber = Transcriber()
        # Unidad 5.5: el Transcriber (core/, sin Qt) notifica eventos de
        # fallback de red vía un callback plano; aquí lo traducimos a una
        # señal Qt para cruzar del hilo background del dictado al hilo
        # principal (mismo patrón que el resto de notificaciones de tray).
        self.transcriber.on_net_fallback_event = self._on_net_fallback_event
        self.db = TranscriptionDB()

        # Poda de historial por retención al arranque (HISTORY_RETENTION_DAYS=0 → conservar siempre)
        try:
            retention_days = int(os.getenv("HISTORY_RETENTION_DAYS", "0") or 0)
            if retention_days > 0:
                pruned = self.db.prune_older_than(retention_days)
                if pruned:
                    logger.info("Retención: eliminadas %d transcripciones (> %d días)", pruned, retention_days)
        except Exception as _prune_exc:
            logger.warning("Error en poda de historial por retención: %s", _prune_exc)

        # Poda de reuniones por retención al arranque (MEETING_RETENTION_DAYS=0 → conservar
        # siempre). Boot-only, mismo patrón que HISTORY_RETENTION_DAYS: es seguro contra una
        # reunión activa porque las filas de `meetings` solo nacen en MEETING.stop() y esta
        # poda corre aquí, antes de que el subsistema de reunión viva.
        try:
            meeting_retention_days = int(os.getenv("MEETING_RETENTION_DAYS", "0") or 0)
            if meeting_retention_days > 0:
                meetings_pruned = self.db.meetings_prune_older_than(meeting_retention_days)
                if meetings_pruned:
                    logger.info("Retención: eliminadas %d reuniones (> %d días)", meetings_pruned, meeting_retention_days)
        except Exception as _meeting_prune_exc:
            logger.warning("Error en poda de reuniones por retención: %s", _meeting_prune_exc)

        self.hotkey = HotkeyListener()
        self.pill = PillWidget()
        self.hud = HudWidget()
        # Indicador de captura en vivo del HUD (¿me está escuchando?): el HUD lee
        # los niveles por canal lock-free con su propio timer. get_levels() no toca
        # el lock de MEETING (floats atómicos), así que el VU no genera contención.
        self.hud.set_level_provider(MEETING.get_levels)

        # Referencia al tray para mostrar mensajes; se asigna desde main()
        self.tray: QSystemTrayIcon | None = None

        # Estado del Proactivo v2 (unidad 5.3): detectores puros alimentados en
        # el tick del _meeting_sync_timer, cuando hay reunión activa.
        self._lull_detector = LullDetector()
        self._monologue_watch = MonologueWatch()
        self._hud_visible = False
        self._hud_has_unseen_card = False  # controla el badge de la pill

        # Contador de generación y guard anti-duplicado
        self._generation = 0
        self._recording_active = False

        # Estado de chunking con lock para acceso seguro entre hilos
        self._chunk_results: dict[int, str] = {}
        # Crudo pre-diccionario por chunk (unidad 6.2); solo se llena cuando
        # transcribe()/translate() detectan diferencia (raw_text no None).
        self._chunk_raw: dict[int, str] = {}
        self._chunk_seq = 0
        # Registro de workers de chunk en vuelo (unidad 0.2): permite a
        # _transcribe_final joinarlos con gracia antes de ensamblar el texto
        # final, en vez de perder en silencio el tramo de un worker que sigue
        # esperando la API. Protegido por _chunk_state_lock; se resetea junto
        # con _chunk_results/_chunk_raw al arrancar cada grabación nueva.
        self._chunk_threads: list[threading.Thread] = []
        self._chunk_state_lock = threading.Lock()
        # Texto crudo pendiente de la última transcripción emitida, indexado por
        # generación (gen). Puente entre _transcribe_final (hilo background) y
        # _on_transcription_done (slot Qt) sin ampliar la firma de la señal.
        self._pending_raw: dict[int, str] = {}

        self._translate_mode = False
        self._chunk_timer = QTimer()
        self._chunk_timer.timeout.connect(self._flush_chunk)

        # Temporizador de seguridad: detiene grabaciones olvidadas automáticamente
        self._safety_timer = QTimer()
        self._safety_timer.setSingleShot(True)
        self._safety_timer.timeout.connect(self._on_hotkey_released)

        # Watchdog de micrófono: detecta si dejan de llegar muestras durante la grabación
        self._mic_watchdog_timer = QTimer()
        self._mic_watchdog_timer.setInterval(1000)  # comprobar cada 1 s
        self._mic_watchdog_timer.timeout.connect(self._check_mic_alive)
        self._last_samples_seen = 0
        self._mic_stall_count = 0

        # Sincronización de la pill con el estado de la reunión: la reunión puede
        # iniciarse/terminarse desde el dashboard (hilo Flask, fuera de este controlador),
        # así que sondeamos MEETING para reflejar el estado en la pill en todos los casos.
        self._meeting_active_seen = False
        self._meeting_stopping = False  # True mientras un stop por hotkey/tray está en curso
        self._meeting_sync_timer = QTimer()
        self._meeting_sync_timer.setInterval(1000)
        self._meeting_sync_timer.timeout.connect(self._sync_meeting_pill)

        # Conectar visualizador a la cola de audio del recorder
        self.pill.visualizer.set_audio_queue(self.recorder.audio_queue)

        # DEBE usarse QueuedConnection: pynput emite desde su propio hilo
        self.hotkey.pressed.connect(self._on_hotkey_pressed, Qt.ConnectionType.QueuedConnection)
        self.hotkey.released.connect(self._on_hotkey_released, Qt.ConnectionType.QueuedConnection)
        self.hotkey.translate_pressed.connect(self._on_translate_pressed, Qt.ConnectionType.QueuedConnection)
        self.hotkey.meeting_toggle.connect(self._on_meeting_toggle, Qt.ConnectionType.QueuedConnection)
        self.hotkey.highlight_pressed.connect(self._on_highlight, Qt.ConnectionType.QueuedConnection)
        self.hotkey.hud_toggle.connect(self._on_hud_toggle, Qt.ConnectionType.QueuedConnection)
        self.hotkey.lost_pressed.connect(self._on_lost_pressed, Qt.ConnectionType.QueuedConnection)
        self.meeting_stopped.connect(self._on_meeting_stopped, Qt.ConnectionType.QueuedConnection)
        self.transcription_done.connect(self._on_transcription_done, Qt.ConnectionType.QueuedConnection)
        self.transcription_error.connect(self._on_transcription_error, Qt.ConnectionType.QueuedConnection)
        self.paste_finished.connect(self._on_paste_finished, Qt.ConnectionType.QueuedConnection)
        self.lost_answer_ready.connect(self._on_lost_answer_ready, Qt.ConnectionType.QueuedConnection)
        self.chunk_loss_warning.connect(self._on_chunk_loss_warning, Qt.ConnectionType.QueuedConnection)
        self.transcription_fallback_notice.connect(
            self._on_transcription_fallback_notice, Qt.ConnectionType.QueuedConnection
        )

        # Señales del HUD hacia el resto de la app (feedback/"me perdí"/pregunta libre)
        self.hud.feedback_requested.connect(self._on_hud_feedback)
        self.hud.lost_requested.connect(self._on_lost_pressed)
        self.hud.ask_requested.connect(self._on_hud_ask)

    def start(self):
        """Inicia el listener de hotkeys y muestra la pill en estado idle."""
        self.hotkey.start()
        self.pill.show()
        self.pill.set_state(PillWidget.STATE_IDLE)
        self._meeting_sync_timer.start()

    @pyqtSlot()
    def _on_hotkey_pressed(self):
        """Guarda la ventana activa e inicia la grabación de audio.

        Envuelve el cuerpo en un try/except defensivo externo para que cualquier
        excepción inesperada (fuera del fallo de recorder.start()) quede registrada
        con traceback y caiga a STATE_ERROR sin propagar la excepción al caller.
        El try/except interno de recorder.start() se conserva íntegro con su return
        temprano para mantener la semántica de fallo puntual de micrófono.
        """
        try:
            # Unidad 1.7: el bump de generación va BAJO _chunk_state_lock, igual que
            # el check+escritura de _chunk_worker — sin esto quedaba una data race
            # residual entre este incremento y la lectura del worker bajo el lock.
            with self._chunk_state_lock:
                self._generation += 1
            _play_sound(880)  # beep alto = inicio de grabación
            save_frontmost_app()
            try:
                self.recorder.start()
            except Exception as e:
                logger.error("Error al iniciar grabación (¿micrófono no disponible?): %s", e)
                self.pill.set_state(PillWidget.STATE_ERROR)
                return
            self._recording_active = True
            with self._chunk_state_lock:
                self._chunk_results.clear()
                self._chunk_raw.clear()
                self._chunk_seq = 0
                self._chunk_threads.clear()
            self._chunk_timer.start(CHUNK_SECONDS * 1000)
            self._safety_timer.start(MAX_RECORDING_SECONDS * 1000)
            # Arrancar watchdog de micrófono (desactivado en modo system: loopback puede silenciar)
            self._last_samples_seen = 0
            self._mic_stall_count = 0
            if self.recorder.watchdog_enabled:
                self._mic_watchdog_timer.start()
            self.pill.set_state(PillWidget.STATE_RECORDING)
        except Exception as e:
            logger.error("_on_hotkey_pressed: fallo inesperado: %s", e, exc_info=True)
            try:
                self.pill.set_state(PillWidget.STATE_ERROR)
            except Exception:
                pass

    @pyqtSlot()
    def _on_translate_pressed(self):
        """Inicia grabación en modo traducción (→ inglés). Sin chunking.

        Envuelve el cuerpo en un try/except defensivo externo para que cualquier
        excepción inesperada quede registrada con traceback y caiga a STATE_ERROR.
        El try/except interno de recorder.start() se conserva con su return temprano
        y el reset de _translate_mode para dejar el estado limpio ante fallo de micrófono.
        """
        try:
            # Unidad 1.7: bump bajo el lock (ver _on_hotkey_pressed).
            with self._chunk_state_lock:
                self._generation += 1
            self._translate_mode = True
            _play_sound(880)
            save_frontmost_app()
            try:
                self.recorder.start()
            except Exception as e:
                logger.error("Error al iniciar grabación para traducción: %s", e)
                self.pill.set_state(PillWidget.STATE_ERROR)
                self._translate_mode = False
                return
            self._recording_active = True
            with self._chunk_state_lock:
                self._chunk_results.clear()
                self._chunk_raw.clear()
                self._chunk_seq = 0
                self._chunk_threads.clear()
            # Sin chunk_timer en modo traducción — se envía audio completo al endpoint de traducción
            self._safety_timer.start(MAX_RECORDING_SECONDS * 1000)
            # Arrancar watchdog de micrófono (desactivado en modo system)
            self._last_samples_seen = 0
            self._mic_stall_count = 0
            if self.recorder.watchdog_enabled:
                self._mic_watchdog_timer.start()
            self.pill.set_state(PillWidget.STATE_RECORDING)
        except Exception as e:
            logger.error("_on_translate_pressed: fallo inesperado: %s", e, exc_info=True)
            self._translate_mode = False
            try:
                self.pill.set_state(PillWidget.STATE_ERROR)
            except Exception:
                pass

    def _flush_chunk(self):
        """Extrae y transcribe el chunk de audio acumulado en un hilo background."""
        chunk_buf = self.recorder.extract_chunk()
        if chunk_buf:
            with self._chunk_state_lock:
                idx = self._chunk_seq
                self._chunk_seq += 1
                # Prompt: últimos 200 chars del índice completado más alto (best-effort)
                if self._chunk_results:
                    last_key = max(self._chunk_results)
                    prompt = self._chunk_results[last_key][-200:]
                else:
                    prompt = None
            gen = self._generation
            t = threading.Thread(
                target=self._chunk_worker,
                args=(chunk_buf, prompt, idx, gen),
                daemon=True,
            )
            # Registrar ANTES de start() para que un snapshot concurrente (bajo
            # el mismo lock) tomado por _transcribe_final nunca pueda perderse
            # este worker (unidad 0.2).
            with self._chunk_state_lock:
                self._chunk_threads.append(t)
            t.start()

    def _chunk_worker(self, wav_buffer, prompt, idx: int, gen: int):
        """Transcribe un chunk en background; descarta resultado si la generación cambió.

        Unidad 1.7: el check de generación y la escritura del resultado ocurren
        JUNTOS bajo _chunk_state_lock — con el check fuera del lock había una
        ventana TOCTOU donde un chunk de la sesión anterior podía colarse en
        _chunk_results DESPUÉS del clear() de la sesión nueva.
        """
        try:
            text, raw = self.transcriber.transcribe(
                wav_buffer, prompt=prompt, return_raw=True, net_fallback=True,
            )
            with self._chunk_state_lock:
                if gen != self._generation:
                    # Sesión vieja: descartar resultado
                    return
                if text:
                    self._chunk_results[idx] = text
                    if raw is not None:
                        self._chunk_raw[idx] = raw
        except Exception as e:
            logger.error("Transcripción de chunk fallida: %s", e)

    @pyqtSlot()
    def _on_hotkey_released(self):
        """Detiene la grabación y lanza la transcripción de los frames restantes.

        El early-return por release espurio queda FUERA del bloque defensivo para
        que nunca se lo trague el except. El cleanup de estado (_recording_active,
        hotkey.reset, stop de timers) ocurre antes de cualquier operación que pueda
        lanzar, garantizando que un fallo posterior no deje la app en estado colgado.
        Si algo falla tras el cleanup, el except registra traceback y pone STATE_ERROR.
        """
        # Early-return: evita releases espurios del safety timer / listener desincronizado
        if not self._recording_active:
            return

        # --- Cleanup de estado PRIMERO: garantiza que no quede colgada aunque falle lo siguiente ---
        self._recording_active = False
        self.hotkey.reset()
        self._safety_timer.stop()
        self._chunk_timer.stop()
        self._mic_watchdog_timer.stop()

        try:
            duration = self.recorder.stop()
            self.pill.set_state(PillWidget.STATE_PROCESSING)

            if duration < 0.3:
                self.pill.set_state(PillWidget.STATE_IDLE)
                return

            translate = self._translate_mode
            self._translate_mode = False  # resetear antes de iniciar el hilo background
            gen = self._generation
            wav_buffer = self.recorder.get_wav_buffer()
            thread = threading.Thread(
                target=self._transcribe_final,
                args=(wav_buffer, duration, translate, gen),
                daemon=True,
            )
            thread.start()
        except Exception as e:
            logger.error("_on_hotkey_released: fallo inesperado: %s", e, exc_info=True)
            self._translate_mode = False
            try:
                self.pill.set_state(PillWidget.STATE_ERROR)
            except Exception:
                pass

    def _transcribe_final(self, wav_buffer, duration, translate: bool = False, gen: int = 0):
        """Transcribe o traduce los frames restantes y emite el resultado."""
        try:
            raw_full = None
            if translate:
                # Modo traducción: no se captura crudo (el diccionario del usuario
                # aplica al idioma dictado, no al idioma traducido de salida).
                target = os.getenv("TRANSLATE_TARGET_LANG", "en")
                text = self.transcriber.translate(wav_buffer, target_lang=target, net_fallback=True)
            else:
                with self._chunk_state_lock:
                    if self._chunk_results:
                        last_key = max(self._chunk_results)
                        prompt = self._chunk_results[last_key][-200:]
                    else:
                        prompt = None
                text, raw = self.transcriber.transcribe(
                    wav_buffer, prompt=prompt, return_raw=True, net_fallback=True,
                )

                # Unidad 0.2: joinar (con gracia acotada) los workers de chunk en
                # vuelo de ESTA generación ANTES de leer/ensamblar _chunk_results.
                # Sin esto, un worker que sigue esperando a la API (Groq lenta)
                # puede perder la carrera contra este tramo final y su texto se
                # descarta en silencio.
                with self._chunk_state_lock:
                    pending_threads = list(self._chunk_threads)
                if _join_pending_chunks(pending_threads, gen, lambda: self._generation):
                    logger.warning(
                        "Dictado largo: un chunk seguía transcribiéndose tras %.0fs de gracia "
                        "— se descarta del ensamblado final (gen=%d).",
                        _CHUNK_JOIN_GRACE_SECONDS,
                        gen,
                    )
                    self.chunk_loss_warning.emit(gen)

                # Asignar el tramo final al índice siguiente en el dict de chunks
                with self._chunk_state_lock:
                    final_idx = self._chunk_seq
                    if text:
                        self._chunk_results[final_idx] = text
                        if raw is not None:
                            self._chunk_raw[final_idx] = raw
                    # Ensamblar texto completo en orden
                    text = " ".join(self._chunk_results[k] for k in sorted(self._chunk_results))
                    # Ensamblar crudo en el mismo orden; usar el texto final del
                    # chunk como fallback cuando ese chunk no tuvo diferencia
                    # (raw None), para no perder alineación entre tramos.
                    if self._chunk_raw:
                        raw_full = " ".join(
                            self._chunk_raw.get(k, self._chunk_results[k])
                            for k in sorted(self._chunk_results)
                        )

            if text.strip():
                text = text.strip()
                if raw_full is not None and raw_full.strip() == text:
                    raw_full = None

                # Modos de dictado por app activa (unidad 6.3) — opt-in, apagado por
                # defecto. Solo aplica al dictado normal (modo 1/2): NO en traducción
                # (translate=True) ni en modo reunión/AUDIO_SOURCE=system (no hay app
                # destino con foco real; el HUD/panel de reunión usa su propio flujo).
                if (
                    not translate
                    and self.recorder.source != "system"
                    and dictation_modes.modes_enabled()
                ):
                    exe_name = get_saved_exe()
                    preset = dictation_modes.preset_for_exe(exe_name)
                    if preset:
                        reformatted = dictation_modes.reformat_text(text, preset)
                        if reformatted and reformatted != text:
                            # El texto MÁS crudo va a raw_full: si el diccionario ya
                            # había producido un raw_full (crudo pre-diccionario),
                            # ese sigue siendo más crudo que el post-diccionario/
                            # pre-reformateo — se conserva. Si no había raw_full aún
                            # (diccionario no cambió nada), el pre-reformateo pasa a
                            # ser el crudo, para que el Undo (6.2) revierta también
                            # el reformateo del LLM.
                            if raw_full is None:
                                raw_full = text
                            text = reformatted

                if raw_full is not None:
                    self._pending_raw[gen] = raw_full.strip()
                self.transcription_done.emit(text, duration, gen)
            else:
                self.transcription_error.emit("No speech detected", gen)
        except Exception as e:
            # Mensaje especial accionable si el modelo local no está descargado
            if "no descargado" in str(e).lower() or "modelo local" in str(e).lower():
                self.transcription_error.emit(
                    "Modelo local no descargado — descárgalo desde el dashboard (Configuración → Modelo local)",
                    gen,
                )
                return
            # Guardar audio fallido para diagnóstico
            try:
                failed_path = FAILED_RECORDING_PATH
                wav_buffer.seek(0)
                with open(failed_path, "wb") as f:
                    f.write(wav_buffer.read())
                logger.info("Audio fallido guardado en: %s", failed_path)
            except Exception as save_err:
                logger.error("No se pudo guardar el audio fallido: %s", save_err)
            error_msg = f"{e} — audio guardado en last_failed_recording.wav"
            self.transcription_error.emit(error_msg, gen)

    @pyqtSlot(str, float, int)
    def _on_transcription_done(self, text: str, duration: float, gen: int):
        """Pega el texto transcrito en la app activa y lo guarda en la base de datos."""
        if gen != self._generation:
            self._pending_raw.pop(gen, None)
            return  # resultado de sesión vieja: descartar
        _play_sound(660)  # beep bajo = transcripción lista
        raw_text = self._pending_raw.pop(gen, None)
        # Unidad 1.2: este es el único punto de éxito tras _transcribe_final
        # (dictado normal y traducción comparten este slot vía transcription_done),
        # que es también el único flujo que escribe last_failed_recording.wav al
        # fallar — un éxito posterior implica que el WAV de diagnóstico ya no hace falta.
        _cleanup_failed_wav()
        # Insertar en DB solo si el historial está habilitado (SAVE_HISTORY=true por defecto)
        if os.getenv("SAVE_HISTORY", "true").lower() == "true":
            self.db.insert(text=text, duration_seconds=duration, source=self.recorder.source, raw_text=raw_text)
        # La pill permanece en STATE_PROCESSING hasta que paste_finished confirme el resultado
        threading.Thread(target=self._paste_worker, args=(text,), daemon=True).start()

    def _paste_worker(self, text: str):
        """Ejecuta paste_text en un hilo background (es bloqueante ~0.5-2s).

        En modo "system" (audio del sistema) no se simula Ctrl+V: el usuario suele
        estar mirando el video, no escribiendo. El texto queda en el portapapeles
        y en el historial, con notificación.
        """
        if self.recorder.source == "system":
            status = "copied_system" if copy_text(text) else "failed"
        else:
            status = paste_text(text)
        self.paste_finished.emit(status)

    @pyqtSlot(str)
    def _on_paste_finished(self, status: str):
        """Actualiza la pill y muestra notificación según el resultado del pegado."""
        if status == "pasted":
            self.pill.set_state(PillWidget.STATE_DONE)
        elif status == "copied_system":
            self.pill.set_state(PillWidget.STATE_DONE)
            if self.tray:
                self.tray.showMessage(
                    "Vflow",
                    "Transcripción lista: copiada al portapapeles y guardada en el historial.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
        elif status == "clipboard_only":
            self.pill.set_state(PillWidget.STATE_DONE)
            if self.tray:
                self.tray.showMessage(
                    "Vflow",
                    "No se pudo pegar automáticamente. El texto está en el portapapeles: usa Ctrl+V.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    4000,
                )
        else:  # "failed"
            self.pill.set_state(PillWidget.STATE_ERROR)
            if self.tray:
                self.tray.showMessage(
                    "Vflow",
                    "No se pudo copiar el texto al portapapeles.",
                    QSystemTrayIcon.MessageIcon.Critical,
                    4000,
                )

    # ------------------------------------------------------------------
    # Modo reunión (captura dual mic + loopback)
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_meeting_toggle(self):
        """Inicia o termina una reunión (AltGr+R, tray o dashboard usan esta vía).

        Iniciar es rápido; terminar bloquea (flush + transcripción final), así que
        el stop corre en un hilo y notifica el resultado vía la señal meeting_stopped.
        """
        if MEETING.is_active():
            # Terminar: puede tardar (flush final). Pill a "procesando".
            self._meeting_stopping = True  # el poller no debe pisar este flujo
            self.pill.set_state(PillWidget.STATE_PROCESSING)
            threading.Thread(target=self._meeting_stop_worker, daemon=True).start()
            return

        if MEETING.is_stopping():
            # F1 (fix concurrencia): is_active() ya es False pero el stop() anterior
            # sigue drenando/generando el acta — arrancar ahora pisaría atributos que
            # ese stop() todavía lee/muta. Mismo feedback que un start fallido (mic).
            self.pill.set_state(PillWidget.STATE_ERROR)
            if self.tray:
                self.tray.showMessage(
                    "Vflow — Reunión",
                    "Guardando la reunión anterior… espera unos segundos e intenta de nuevo.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    3000,
                )
            return

        # Iniciar (start() re-verifica is_stopping bajo su propio lock: cubre la
        # carrera si el stop() anterior terminó justo entre el check de arriba y esta
        # llamada — en ese caso procede normal; si sigue en curso, rechaza igual con
        # el mismo shape de error que maneja el "else" de abajo).
        res = MEETING.start()
        if res.get("ok"):
            self._meeting_active_seen = True  # sincronizar con el poller
            self._apply_meeting_viz(True)     # visualizador del pill = audio de la reunión
            self.pill.set_state(PillWidget.STATE_RECORDING)
            self.pill.set_meeting_state(True, source_system=(os.getenv("AUDIO_SOURCE", "mic") == "system"))
            if self.tray:
                extra = "" if res.get("sys_available", True) else " (solo micrófono: no se detectó audio del sistema)"
                self.tray.showMessage(
                    "Vflow — Reunión",
                    f"Reunión iniciada.{extra} Abre el dashboard para ver el transcript en vivo.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
        else:
            self.pill.set_state(PillWidget.STATE_ERROR)
            if self.tray:
                self.tray.showMessage(
                    "Vflow — Reunión",
                    res.get("error", "No se pudo iniciar la reunión."),
                    QSystemTrayIcon.MessageIcon.Critical,
                    4000,
                )

    @pyqtSlot()
    def _on_highlight(self):
        """Marca el instante actual como momento destacado (AltGr+H).

        Silencioso si no hay reunión activa (evita ruido si el usuario pulsa el
        atajo por error fuera de una reunión). Con reunión activa: persiste el
        highlight en MEETING y da feedback inmediato (beep + notificación tray).
        """
        if not MEETING.is_active():
            return
        item = MEETING.add_highlight()
        if not item:
            return
        _play_sound(1046)  # beep agudo distinto = confirmación de highlight
        if self.tray:
            self.tray.showMessage(
                "Vflow — Reunión",
                f"✓ Momento destacado ({item['time']})",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
        if self._hud_visible:
            self.hud.show_highlight_confirmation(item["time"])

    # ------------------------------------------------------------------
    # HUD proactivo (unidad 5.3)
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_hud_toggle(self):
        """AltGr+A o clic derecho de la pill: abre/cierra el HUD.

        Sin reunión activa se ignora (contrato de la unidad: el HUD solo tiene
        sentido durante una reunión).
        """
        if not MEETING.is_active():
            return
        if self._hud_visible:
            self.hud.hide()
            self._hud_visible = False
        else:
            # Panel lateral (unidad 5.4): ya no es un popup anclado a la pill.
            # Respeta la geometría que el usuario haya dejado (posición/tamaño);
            # solo aplica el default (lado derecho) la primera vez o tras "restablecer".
            self.hud.ensure_initial_geometry()
            self.hud.set_registro(MEETING.get_registro())
            self.hud.show()
            self._hud_visible = True
            self._hud_has_unseen_card = False
            self.pill.set_meeting_state(
                True, paused=self._meeting_paused_cached(),
                elapsed_fmt=MEETING.status().get("elapsed_fmt", "00:00"),
                level_ellos=0.0,
                source_system=(os.getenv("AUDIO_SOURCE", "mic") == "system"),
                badge=None,
            )

    def _meeting_paused_cached(self) -> bool:
        try:
            return bool(MEETING.status().get("paused"))
        except Exception:  # noqa: BLE001
            return False

    @pyqtSlot()
    def _on_lost_pressed(self):
        """AltGr+M o botón del HUD: abre el HUD si estaba cerrado y dispara el
        resumen de los últimos 2 minutos vía answer_live (SÍNCRONO, 5-15s) en un
        hilo aparte — NUNCA en el hilo Qt (ver core/assistant.answer_live)."""
        if not MEETING.is_active():
            return
        if not self._hud_visible:
            self._on_hud_toggle()
        self.hud.show_lost_spinner()
        threading.Thread(target=self._lost_worker, daemon=True).start()

    def _lost_worker(self):
        """Corre en un hilo daemon: answer_live() es síncrono/bloqueante (5-15s
        con claude-cli). El resultado vuelve al hilo Qt vía QueuedConnection."""
        try:
            res = _assistant.answer_live("Resume brevemente los últimos 2 minutos de la reunión")
        except Exception as exc:  # noqa: BLE001
            logger.error("HUD: error en 'me perdí': %s", exc)
            res = {"ok": False, "error": str(exc)}
        self.lost_answer_ready.emit(res)

    @pyqtSlot(dict)
    def _on_lost_answer_ready(self, res: dict):
        if res.get("ok"):
            self.hud.show_lost_answer(res.get("answer") or "(sin respuesta)")
        else:
            self.hud.show_lost_answer("No se pudo generar el resumen: " + str(res.get("error") or "error desconocido"))

    def _on_hud_feedback(self, key: str, tipo: str, texto: str, value: int):
        """Botones ✓/✗ de una tarjeta del HUD → persiste igual que el feedback web."""
        try:
            MEETING.add_feedback(key, tipo, texto, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HUD: error registrando feedback: %s", exc)

    def _on_hud_ask(self, message: str):
        """Pregunta libre del mini-input del HUD → mismo worker que 'me perdí'
        (answer_live síncrono en un hilo, resultado por señal Qt)."""
        if not MEETING.is_active():
            return
        self.hud.show_lost_spinner()
        threading.Thread(target=self._ask_worker, args=(message,), daemon=True).start()

    def _ask_worker(self, message: str):
        try:
            res = _assistant.answer_live(message)
        except Exception as exc:  # noqa: BLE001
            logger.error("HUD: error en pregunta libre: %s", exc)
            res = {"ok": False, "error": str(exc)}
        self.lost_answer_ready.emit(res)

    def _meeting_stop_worker(self):
        """Detiene la reunión en background (bloquea) y emite el resultado al hilo Qt."""
        try:
            res = MEETING.stop()
        except Exception as exc:  # noqa: BLE001
            logger.error("Error al detener la reunión: %s", exc)
            res = {"ok": False, "error": str(exc)}
        self.meeting_stopped.emit(res)

    @pyqtSlot(object)
    def _on_meeting_stopped(self, res: dict):
        """Persiste la reunión finalizada y notifica el resultado."""
        self._meeting_stopping = False
        self._meeting_active_seen = False  # sincronizar con el poller
        self._apply_meeting_viz(False)     # restaurar visualizador al recorder de dictado
        self.pill.set_meeting_state(False)
        if not res.get("ok"):
            self.pill.set_state(PillWidget.STATE_ERROR)
            if self.tray:
                self.tray.showMessage(
                    "Vflow — Reunión",
                    res.get("error", "Error al terminar la reunión."),
                    QSystemTrayIcon.MessageIcon.Critical,
                    4000,
                )
            return

        transcript = (res.get("transcript") or "").strip()
        segments = res.get("segments") or []
        duration = res.get("duration_seconds", 0)
        saved = res.get("saved", False)  # la persistencia ocurre dentro de MEETING.stop()

        self.pill.set_state(PillWidget.STATE_DONE)
        if self.tray:
            mins = int(duration // 60)
            secs = int(duration % 60)
            if not transcript:
                msg = "Reunión terminada (sin transcripción: no se detectó voz)."
            elif saved:
                msg = f"Reunión guardada: {len(segments)} intervenciones, {mins:02d}:{secs:02d}."
            else:
                msg = f"Reunión terminada: {len(segments)} intervenciones (historial desactivado, no guardada)."
            self.tray.showMessage("Vflow — Reunión", msg, QSystemTrayIcon.MessageIcon.Information, 5000)

    @pyqtSlot()
    def _sync_meeting_pill(self):
        """Sincroniza la pill con el estado real de la reunión (poll cada 1s).

        Necesario porque la reunión puede iniciarse/terminarse desde el dashboard
        (hilo Flask), fuera de este controlador. Cubre el bug de la pill "atascada"
        en modo grabación cuando inicias con AltGr+R y terminas desde el dashboard.
        No toca la pill si hay un dictado en curso, ni pisa el flujo de stop por hotkey.
        También empuja el overlay de reunión (timer/paused/nivel "Ellos") cada tick,
        no solo en las transiciones — es lo que hace avanzar el timer mm:ss visible.
        """
        active = MEETING.is_active()
        if active != self._meeting_active_seen:
            self._meeting_active_seen = active
            if not self._recording_active:
                if active:
                    # Reunión iniciada desde fuera (dashboard) → reflejar en la pill
                    self._apply_meeting_viz(True)
                    self.pill.set_state(PillWidget.STATE_RECORDING)
                elif not self._meeting_stopping:
                    # Reunión terminada desde fuera (dashboard). Si fue por hotkey/tray,
                    # _on_meeting_stopped ya gestiona la pill (_meeting_stopping=True).
                    self._apply_meeting_viz(False)
                    self.pill.set_state(PillWidget.STATE_DONE)
            if not active:
                # Reunión terminada: cerrar el HUD y resetear el gate proactivo
                # (nueva reunión = estado limpio, sin arrastrar cooldowns/cola).
                if self._hud_visible:
                    self.hud.hide()
                    self._hud_visible = False
                self._hud_has_unseen_card = False
                PROACTIVE.reset()
                self._lull_detector.reset()
                self._monologue_watch.reset()

        if active:
            try:
                status = MEETING.status()
                levels = status.get("levels") or {}
                level_yo = float(levels.get("yo", 0.0) or 0.0)
                level_ellos = float(levels.get("ellos", 0.0) or 0.0)
                self._tick_proactive(levels)
                badge = "!" if (self._hud_has_unseen_card and not self._hud_visible) else None
                self.pill.set_meeting_state(
                    True,
                    paused=bool(status.get("paused")),
                    elapsed_fmt=status.get("elapsed_fmt", "00:00"),
                    level_ellos=level_ellos,
                    level_yo=level_yo,
                    source_system=(os.getenv("AUDIO_SOURCE", "mic") == "system"),
                    badge=badge,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("_sync_meeting_pill: error actualizando overlay de reunión: %s", exc)
        elif self.pill._meeting_mode:
            self.pill.set_meeting_state(False)

    def _tick_proactive(self, levels: dict):
        """Un tick (~1s) del Proactivo v2: alimenta lull/monólogo y entrega la
        siguiente tarjeta de la cola si corresponde (unidad 5.3).

        Llamado desde _sync_meeting_pill mientras hay reunión activa. Las clases
        de detección/memoria cruzada (5.1/5.2) encolan en PROACTIVE desde sus
        propios puntos de integración; aquí solo se drena la cola y se corre el
        coaching de monólogo (modo trainer).
        """
        try:
            level_yo = float(levels.get("yo", 0.0) or 0.0)
            level_ellos = float(levels.get("ellos", 0.0) or 0.0)
            lull = self._lull_detector.update(level_yo, level_ellos)

            if _proactive.get_mode() == "trainer":
                coaching_card = self._monologue_watch.update(level_yo, level_ellos)
                if coaching_card is not None:
                    key = f"coaching-monologue-{int(MEETING.status().get('elapsed', 0))}"
                    PROACTIVE.enqueue({**coaching_card, "key": key})

            card = PROACTIVE.pop_deliverable(lull)
            if card is not None:
                if self._hud_visible:
                    self.hud.add_card(card)
                else:
                    self._hud_has_unseen_card = True

            if self._hud_visible:
                self.hud.set_timer_label(MEETING.status().get("elapsed_fmt", "00:00"))
                self.hud.set_registro(MEETING.get_registro())
        except Exception as exc:  # noqa: BLE001
            logger.warning("_tick_proactive: error (se ignora este tick): %s", exc)

    def _apply_meeting_viz(self, active: bool):
        """Apunta el visualizador del pill al audio de la reunión (tu voz) o lo
        restaura al grabador de dictado. Así las barras se mueven durante la reunión."""
        try:
            self.pill.visualizer.set_audio_queue(MEETING.viz_queue if active else self.recorder.audio_queue)
        except Exception as exc:  # noqa: BLE001
            logger.debug("No se pudo cambiar la cola del visualizador: %s", exc)

    @pyqtSlot()
    def _check_mic_alive(self):
        """Detecta si el micrófono dejó de entregar audio durante la grabación.

        Se llama cada 1 s por _mic_watchdog_timer. Si tras _MIC_STALL_LIMIT
        comprobaciones consecutivas el contador de muestras no avanzó, asume
        que el dispositivo se desconectó y aborta la grabación con STATE_ERROR.
        """
        if not self._recording_active:
            # Grabación ya terminada por otra ruta — apagar el watchdog y salir
            self._mic_watchdog_timer.stop()
            return

        current = self.recorder.samples_count()

        if current > self._last_samples_seen:
            # Llegaron muestras nuevas: el micrófono sigue vivo
            self._mic_stall_count = 0
            self._last_samples_seen = current
            return

        # Sin avance. Ignorar la primera vez si aún no llegó ninguna muestra
        # (el stream puede tardar unos ms en entregar el primer bloque).
        if current == 0 and self._mic_stall_count == 0:
            return

        self._mic_stall_count += 1
        if self._mic_stall_count >= _MIC_STALL_LIMIT:
            logger.error(
                "Watchdog: micrófono sin muestras nuevas durante %d s — asumiendo desconexión.",
                _MIC_STALL_LIMIT,
            )
            self._mic_watchdog_timer.stop()
            self._chunk_timer.stop()
            self._safety_timer.stop()
            self._recording_active = False
            self.hotkey.reset()
            try:
                self.recorder.stop()
            except Exception as exc:
                logger.warning("Watchdog: error al cerrar stream del recorder: %s", exc)
            # Incrementar generación para invalidar chunks en vuelo de esta sesión
            # (unidad 1.7: bajo el lock, como todos los bumps de _generation)
            with self._chunk_state_lock:
                self._generation += 1
            self.pill.set_state(PillWidget.STATE_ERROR)
            if self.tray:
                self.tray.showMessage(
                    "Vflow",
                    "Micrófono desconectado durante la grabación.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    4000,
                )

    def _on_net_fallback_event(self, message: str):
        """Callback plano (hilo background) del Transcriber (unidad 5.5).

        core/transcriber.py no conoce Qt: solo llama a un callable con un
        string. Aquí lo convertimos a una señal Qt (QueuedConnection) para
        cruzar de forma segura al hilo principal, igual que el resto de
        notificaciones de tray de esta clase.
        """
        self.transcription_fallback_notice.emit(message)

    @pyqtSlot(str)
    def _on_transcription_fallback_notice(self, message: str):
        """Muestra en bandeja un evento de fallback de red Groq -> local (unidad 5.5).

        No depende de ``self._generation``: a diferencia de transcription_error/
        transcription_done, este aviso describe el ESTADO del breaker de red
        (p. ej. "sin internet, usando el modelo local"), no el resultado de un
        dictado puntual — sigue siendo relevante aunque el usuario ya haya
        iniciado otro dictado.
        """
        if self.tray:
            self.tray.showMessage(
                "Vflow",
                message,
                QSystemTrayIcon.MessageIcon.Information,
                5000,
            )

    @pyqtSlot(int)
    def _on_chunk_loss_warning(self, gen: int):
        """Notifica en bandeja que un fragmento del dictado no se pudo transcribir a tiempo.

        Unidad 0.2: emitida por _transcribe_final (hilo background) cuando, tras
        la gracia de _join_pending_chunks, un worker de chunk de esta generación
        seguía vivo. No inserta marcadores en el texto pegado — el texto
        disponible se pega normal; esto es solo la notificación de tray.
        """
        if gen != self._generation:
            return  # el usuario ya inició otro dictado: notificación obsoleta
        if self.tray:
            self.tray.showMessage(
                "Vflow",
                "Parte del dictado no se pudo transcribir (un fragmento sigue pendiente).",
                QSystemTrayIcon.MessageIcon.Warning,
                4000,
            )

    @pyqtSlot(str, int)
    def _on_transcription_error(self, error: str, gen: int):
        """Muestra estado de error en la pill y notificación en bandeja cuando falla la transcripción."""
        if gen != self._generation:
            return  # resultado de sesión vieja: descartar
        self.pill.set_state(PillWidget.STATE_ERROR)
        if self.tray:
            truncated = error[:120] + "…" if len(error) > 120 else error
            self.tray.showMessage(
                "Vflow - Error",
                truncated,
                QSystemTrayIcon.MessageIcon.Critical,
                4000,
            )


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------
def main():
    """Punto de entrada: configura logging, verifica instancia única, inicia la app."""
    global _MUTEX_HANDLE

    # Configurar logging a archivo antes de cualquier otra operación
    _setup_logging()
    logger.info("Iniciando Vflow")

    # Migrar clave legacy en texto plano a DPAPI cifrado (operación idempotente)
    _migrate_plaintext_key()

    # Unidad 1.2: TTL de last_failed_recording.wav — restos de una grabación
    # fallida con más de 24h en disco se eliminan antes de crear ningún componente.
    _cleanup_stale_failed_wav()

    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)

    app = QApplication(sys.argv)
    app.setApplicationName("Vflow")
    app.setQuitOnLastWindowClosed(False)

    # Instancia única: mutex con nombre global de sesión local
    _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\VflowSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        QMessageBox.information(
            None,
            "Vflow",
            "Vflow ya está en ejecución (revisa la bandeja del sistema).",
        )
        sys.exit(0)

    # Permitir que Ctrl+C cierre la app
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Reparar la clave de registro si el exe fue movido/reinstalado
    try:
        _repair_launch_at_login()
    except Exception as _repair_exc:
        logger.warning("Error inesperado en _repair_launch_at_login: %s", _repair_exc)

    # Primera ejecución: pedir API key si falta
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        dialog = FirstRunDialog()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)

    # Iniciar dashboard web
    port = start_web_server()

    # Iniciar controlador principal
    vflow = VflowApp()
    vflow.start()

    # Limpiar listener de hotkeys al salir
    app.aboutToQuit.connect(vflow.hotkey.stop)

    # Icono de bandeja del sistema
    tray = _setup_tray(app, port, vflow)  # noqa: F841 — debe mantenerse la referencia viva
    vflow.tray = tray  # exponer tray al controlador para mensajes de notificación

    # Unidad 1.2: aviso de DB recuperada de corrupción. db/database.py no conoce
    # Qt, así que solo deja la bandera en el objeto (_init_db corrió dentro de
    # VflowApp.__init__, ANTES de que el tray existiera) — se notifica aquí, ya
    # con el tray disponible, en vez de perderse en un logger.warning silencioso.
    if getattr(vflow.db, "recovered_from_corruption", False):
        tray.showMessage(
            "Vflow",
            "La base de datos estaba dañada y se recreó vacía. "
            "El archivo anterior quedó como .corrupt junto a la DB.",
            QSystemTrayIcon.MessageIcon.Warning,
            8000,
        )

    # Clic corto en la pill con reunión activa → abrir el dashboard en /reunion.
    # QueuedConnection no es estrictamente necesaria aquí (la señal se emite desde
    # mouseReleaseEvent, ya en el hilo Qt), pero se usa por consistencia con el resto
    # de conexiones de señales de la app.
    vflow.pill.open_dashboard_requested.connect(
        lambda: webbrowser.open(f"http://localhost:{port}/reunion"),
        Qt.ConnectionType.QueuedConnection,
    )
    vflow.pill.hud_toggle_requested.connect(
        vflow._on_hud_toggle, Qt.ConnectionType.QueuedConnection,
    )

    logger.info("Vflow v%s activo. Dashboard en http://localhost:%s", APP_VERSION, port)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
