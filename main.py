#!/usr/bin/env python3
"""Vflow - Voice-to-text desktop tool powered by Groq Whisper."""

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
import winreg
import winsound
from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu,
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox,
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QAction

from dotenv import set_key, unset_key
from ui.pill_widget import PillWidget
from core.recorder import AudioRecorder
from core.transcriber import Transcriber
from core.hotkey import HotkeyListener
from core.clipboard import paste_text, save_frontmost_app
from core.secrets import encrypt
from db.database import TranscriptionDB
from web.server import start_web_server
from config import LOGO_PATH, APP_DATA_DIR, GROQ_API_KEY, CHUNK_SECONDS, MAX_RECORDING_SECONDS

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
            if getattr(sys, "frozen", False):
                exe = sys.executable
            else:
                exe = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
            winreg.SetValueEx(key, _REGISTRY_APP_NAME, 0, winreg.REG_SZ, exe)
        else:
            try:
                winreg.DeleteValue(key, _REGISTRY_APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        logger.error("Error al configurar inicio con Windows: %s", e)


# ---------------------------------------------------------------------------
# Bandeja del sistema
# ---------------------------------------------------------------------------
def _setup_tray(app: QApplication, port: int) -> QSystemTrayIcon:
    """Crea el icono de bandeja del sistema con menú de dashboard, auto-inicio y salir."""
    pixmap = QPixmap(LOGO_PATH)
    if pixmap.isNull():
        icon = QIcon()
    else:
        icon = QIcon(pixmap.scaled(22, 22, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    tray = QSystemTrayIcon(icon, app)

    menu = QMenu()

    status = QAction("Vflow - Activo", menu)
    status.setEnabled(False)
    menu.addAction(status)
    menu.addSeparator()

    dashboard = QAction(f"Abrir Dashboard (:{port})", menu)
    dashboard.triggered.connect(lambda: subprocess.run(["cmd", "/c", "start", f"http://localhost:{port}"], capture_output=True))
    menu.addAction(dashboard)
    menu.addSeparator()

    login_action = QAction("Iniciar con Windows", menu)
    login_action.setCheckable(True)
    login_action.setChecked(_is_launch_at_login())
    login_action.toggled.connect(_set_launch_at_login)
    menu.addAction(login_action)
    menu.addSeparator()

    quit_action = QAction("Salir", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.setToolTip("Vflow - Voice to Text")
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

    def __init__(self):
        """Inicializa componentes (recorder, transcriber, DB, hotkey, pill) y conecta señales."""
        super().__init__()
        self.recorder = AudioRecorder()
        self.transcriber = Transcriber()
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

        self.hotkey = HotkeyListener()
        self.pill = PillWidget()

        # Referencia al tray para mostrar mensajes; se asigna desde main()
        self.tray: QSystemTrayIcon | None = None

        # Contador de generación y guard anti-duplicado
        self._generation = 0
        self._recording_active = False

        # Estado de chunking con lock para acceso seguro entre hilos
        self._chunk_results: dict[int, str] = {}
        self._chunk_seq = 0
        self._chunk_state_lock = threading.Lock()

        self._translate_mode = False
        self._chunk_timer = QTimer()
        self._chunk_timer.timeout.connect(self._flush_chunk)

        # Temporizador de seguridad: detiene grabaciones olvidadas automáticamente
        self._safety_timer = QTimer()
        self._safety_timer.setSingleShot(True)
        self._safety_timer.timeout.connect(self._on_hotkey_released)

        # Conectar visualizador a la cola de audio del recorder
        self.pill.visualizer.set_audio_queue(self.recorder.audio_queue)

        # DEBE usarse QueuedConnection: pynput emite desde su propio hilo
        self.hotkey.pressed.connect(self._on_hotkey_pressed, Qt.ConnectionType.QueuedConnection)
        self.hotkey.released.connect(self._on_hotkey_released, Qt.ConnectionType.QueuedConnection)
        self.hotkey.translate_pressed.connect(self._on_translate_pressed, Qt.ConnectionType.QueuedConnection)
        self.transcription_done.connect(self._on_transcription_done, Qt.ConnectionType.QueuedConnection)
        self.transcription_error.connect(self._on_transcription_error, Qt.ConnectionType.QueuedConnection)
        self.paste_finished.connect(self._on_paste_finished, Qt.ConnectionType.QueuedConnection)

    def start(self):
        """Inicia el listener de hotkeys y muestra la pill en estado idle."""
        self.hotkey.start()
        self.pill.show()
        self.pill.set_state(PillWidget.STATE_IDLE)

    @pyqtSlot()
    def _on_hotkey_pressed(self):
        """Guarda la ventana activa e inicia la grabación de audio."""
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
            self._chunk_seq = 0
        self._chunk_timer.start(CHUNK_SECONDS * 1000)
        self._safety_timer.start(MAX_RECORDING_SECONDS * 1000)
        self.pill.set_state(PillWidget.STATE_RECORDING)

    @pyqtSlot()
    def _on_translate_pressed(self):
        """Inicia grabación en modo traducción (→ inglés). Sin chunking."""
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
            self._chunk_seq = 0
        # Sin chunk_timer en modo traducción — se envía audio completo al endpoint de traducción
        self._safety_timer.start(MAX_RECORDING_SECONDS * 1000)
        self.pill.set_state(PillWidget.STATE_RECORDING)

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
            threading.Thread(
                target=self._chunk_worker,
                args=(chunk_buf, prompt, idx, gen),
                daemon=True,
            ).start()

    def _chunk_worker(self, wav_buffer, prompt, idx: int, gen: int):
        """Transcribe un chunk en background; descarta resultado si la generación cambió."""
        try:
            text = self.transcriber.transcribe(wav_buffer, prompt=prompt)
            if gen != self._generation:
                # Sesión vieja: descartar resultado
                return
            if text:
                with self._chunk_state_lock:
                    self._chunk_results[idx] = text
        except Exception as e:
            logger.error("Transcripción de chunk fallida: %s", e)

    @pyqtSlot()
    def _on_hotkey_released(self):
        """Detiene la grabación y lanza la transcripción de los frames restantes."""
        # Early-return: evita releases espurios del safety timer / listener desincronizado
        if not self._recording_active:
            return
        self._recording_active = False
        self.hotkey.reset()

        self._safety_timer.stop()
        self._chunk_timer.stop()
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

    def _transcribe_final(self, wav_buffer, duration, translate: bool = False, gen: int = 0):
        """Transcribe o traduce los frames restantes y emite el resultado."""
        try:
            if translate:
                target = os.getenv("TRANSLATE_TARGET_LANG", "en")
                text = self.transcriber.translate(wav_buffer, target_lang=target)
            else:
                with self._chunk_state_lock:
                    if self._chunk_results:
                        last_key = max(self._chunk_results)
                        prompt = self._chunk_results[last_key][-200:]
                    else:
                        prompt = None
                text = self.transcriber.transcribe(wav_buffer, prompt=prompt)
                # Asignar el tramo final al índice siguiente en el dict de chunks
                with self._chunk_state_lock:
                    final_idx = self._chunk_seq
                    if text:
                        self._chunk_results[final_idx] = text
                    # Ensamblar texto completo en orden
                    text = " ".join(self._chunk_results[k] for k in sorted(self._chunk_results))

            if text.strip():
                self.transcription_done.emit(text.strip(), duration, gen)
            else:
                self.transcription_error.emit("No speech detected", gen)
        except Exception as e:
            # Guardar audio fallido para diagnóstico
            try:
                failed_path = os.path.join(APP_DATA_DIR, "last_failed_recording.wav")
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
            return  # resultado de sesión vieja: descartar
        _play_sound(660)  # beep bajo = transcripción lista
        # Insertar en DB solo si el historial está habilitado (SAVE_HISTORY=true por defecto)
        if os.getenv("SAVE_HISTORY", "true").lower() == "true":
            self.db.insert(text=text, duration_seconds=duration)
        # La pill permanece en STATE_PROCESSING hasta que paste_finished confirme el resultado
        threading.Thread(target=self._paste_worker, args=(text,), daemon=True).start()

    def _paste_worker(self, text: str):
        """Ejecuta paste_text en un hilo background (es bloqueante ~0.5-2s)."""
        status = paste_text(text)
        self.paste_finished.emit(status)

    @pyqtSlot(str)
    def _on_paste_finished(self, status: str):
        """Actualiza la pill y muestra notificación según el resultado del pegado."""
        if status == "pasted":
            self.pill.set_state(PillWidget.STATE_DONE)
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
    tray = _setup_tray(app, port)  # noqa: F841 — debe mantenerse la referencia viva
    vflow.tray = tray  # exponer tray al controlador para mensajes de notificación

    logger.info("Vflow activo. Dashboard en http://localhost:%s", port)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
