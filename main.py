#!/usr/bin/env python3
"""Vflow - Voice-to-text desktop tool powered by Groq Whisper."""

import os
import stat
import sys
import signal
import subprocess
import threading
import logging
import winreg
from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu,
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox,
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QAction

from ui.pill_widget import PillWidget
from core.recorder import AudioRecorder
from core.transcriber import Transcriber
from core.hotkey import HotkeyListener
from core.clipboard import paste_text, save_frontmost_app
from db.database import TranscriptionDB
from web.server import start_web_server
from config import LOGO_PATH, APP_DATA_DIR, GROQ_API_KEY, CHUNK_SECONDS, MAX_RECORDING_SECONDS

logger = logging.getLogger(__name__)

_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REGISTRY_APP_NAME = "Vflow"


# ---------------------------------------------------------------------------
# First-run dialog
# ---------------------------------------------------------------------------
class FirstRunDialog(QDialog):
    """Shown when GROQ_API_KEY is missing on first launch."""

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
        """Valida la API key, la guarda en .env y la establece en el entorno."""
        key = self.key_input.text().strip()
        if not key.startswith("gsk_") or len(key) < 20:
            QMessageBox.warning(self, "Error", "La clave debe comenzar con 'gsk_' y tener al menos 20 caracteres.")
            return

        env_path = os.path.join(APP_DATA_DIR, ".env")
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        with open(env_path, "w") as f:
            f.write(f"GROQ_API_KEY={key}\n")

        # Restrict .env permissions to current user (best-effort on Windows)
        try:
            os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

        # Set in current process so Transcriber picks it up
        os.environ["GROQ_API_KEY"] = key
        self.accept()


# ---------------------------------------------------------------------------
# Launch at Login (Windows Registry)
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
        logger.error("Error setting launch at login: %s", e)


# ---------------------------------------------------------------------------
# System tray
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
# Main app controller
# ---------------------------------------------------------------------------
class VflowApp(QObject):
    """Main application controller. Wires hotkey -> recorder -> transcriber -> clipboard."""

    transcription_done = pyqtSignal(str, float)
    transcription_error = pyqtSignal(str)

    def __init__(self):
        """Inicializa componentes (recorder, transcriber, DB, hotkey, pill) y conecta señales."""
        super().__init__()
        self.recorder = AudioRecorder()
        self.transcriber = Transcriber()
        self.db = TranscriptionDB()
        self.hotkey = HotkeyListener()
        self.pill = PillWidget()

        # Chunked transcription state
        self._chunk_texts: list[str] = []
        self._chunk_timer = QTimer()
        self._chunk_timer.timeout.connect(self._flush_chunk)

        # Safety timer: auto-stop forgotten recordings
        self._safety_timer = QTimer()
        self._safety_timer.setSingleShot(True)
        self._safety_timer.timeout.connect(self._on_hotkey_released)

        # Connect visualizer to recorder's audio queue
        self.pill.visualizer.set_audio_queue(self.recorder.audio_queue)

        # MUST use QueuedConnection: pynput emits from its own thread
        self.hotkey.pressed.connect(self._on_hotkey_pressed, Qt.ConnectionType.QueuedConnection)
        self.hotkey.released.connect(self._on_hotkey_released, Qt.ConnectionType.QueuedConnection)
        self.hotkey.toggle_pill.connect(self._on_toggle_pill, Qt.ConnectionType.QueuedConnection)
        self.transcription_done.connect(self._on_transcription_done, Qt.ConnectionType.QueuedConnection)
        self.transcription_error.connect(self._on_transcription_error, Qt.ConnectionType.QueuedConnection)

    def start(self):
        """Inicia el listener de hotkeys y muestra la pill en estado idle."""
        self.hotkey.start()
        self.pill.show()
        self.pill.set_state(PillWidget.STATE_IDLE)

    @pyqtSlot()
    def _on_hotkey_pressed(self):
        """Guarda la ventana activa e inicia la grabación de audio."""
        save_frontmost_app()
        try:
            self.recorder.start()
        except Exception as e:
            logger.error("Failed to start recording (no microphone?): %s", e)
            self.pill.set_state(PillWidget.STATE_ERROR)
            return
        self._chunk_texts.clear()
        self._chunk_timer.start(CHUNK_SECONDS * 1000)
        self._safety_timer.start(MAX_RECORDING_SECONDS * 1000)
        self.pill.set_state(PillWidget.STATE_RECORDING)

    def _flush_chunk(self):
        """Extract and transcribe accumulated audio chunk in background."""
        chunk_buf = self.recorder.extract_chunk()
        if chunk_buf:
            prompt = self._chunk_texts[-1][-200:] if self._chunk_texts else None
            threading.Thread(
                target=self._chunk_worker,
                args=(chunk_buf, prompt),
                daemon=True,
            ).start()

    def _chunk_worker(self, wav_buffer, prompt):
        """Transcribe a single chunk in background, accumulate text."""
        try:
            text = self.transcriber.transcribe(wav_buffer, prompt=prompt)
            if text:
                self._chunk_texts.append(text)
        except Exception as e:
            logger.error("Chunk transcription failed: %s", e)

    @pyqtSlot()
    def _on_hotkey_released(self):
        """Detiene la grabación y lanza la transcripción de los frames restantes."""
        self._safety_timer.stop()
        self._chunk_timer.stop()
        duration = self.recorder.stop()
        self.pill.set_state(PillWidget.STATE_PROCESSING)

        if duration < 0.3:
            self.pill.set_state(PillWidget.STATE_IDLE)
            return

        wav_buffer = self.recorder.get_wav_buffer()
        thread = threading.Thread(
            target=self._transcribe_final,
            args=(wav_buffer, duration),
            daemon=True,
        )
        thread.start()

    def _transcribe_final(self, wav_buffer, duration):
        """Transcribe remaining frames, join with chunk texts, emit result."""
        try:
            prompt = self._chunk_texts[-1][-200:] if self._chunk_texts else None
            text = self.transcriber.transcribe(wav_buffer, prompt=prompt)
            if text:
                self._chunk_texts.append(text)
            full_text = " ".join(self._chunk_texts)
            if full_text.strip():
                self.transcription_done.emit(full_text.strip(), duration)
            else:
                self.transcription_error.emit("No speech detected")
        except Exception as e:
            self.transcription_error.emit(str(e))

    @pyqtSlot()
    def _on_toggle_pill(self):
        """Toggle pill widget visibility with Alt+J."""
        self.pill.setVisible(not self.pill.isVisible())

    @pyqtSlot(str, float)
    def _on_transcription_done(self, text: str, duration: float):
        """Pega el texto transcrito en la app activa y lo guarda en la base de datos."""
        paste_text(text)
        self.db.insert(text=text, duration_seconds=duration)
        self.pill.set_state(PillWidget.STATE_DONE)

    @pyqtSlot(str)
    def _on_transcription_error(self, error: str):
        """Muestra estado de error en la pill cuando falla la transcripción."""
        self.pill.set_state(PillWidget.STATE_ERROR)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    """Punto de entrada: configura Qt, verifica API key, inicia dashboard y app."""
    import ctypes
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)

    app = QApplication(sys.argv)
    app.setApplicationName("Vflow")
    app.setQuitOnLastWindowClosed(False)

    # Allow Ctrl+C to kill the app
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # First-run: ask for API key if missing
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        dialog = FirstRunDialog()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)

    # Start web dashboard
    port = start_web_server()

    # Start the app
    vflow = VflowApp()
    vflow.start()

    # Clean up hotkey listener on quit
    app.aboutToQuit.connect(vflow.hotkey.stop)

    # System tray icon
    tray = _setup_tray(app, port)  # noqa: F841 — must keep reference alive

    print("\nVflow running. Ctrl+Alt (hold) to record, double Ctrl to toggle hands-free.")
    print(f"Dashboard available at http://localhost:{port}\n")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
