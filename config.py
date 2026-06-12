import os
import sys
from dotenv import load_dotenv


def _get_resource_dir() -> str:
    """Read-only bundled assets (logo, etc). PyInstaller puts them in sys._MEIPASS."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _get_data_dir() -> str:
    """Writable user data (DB, .env). In bundle → %APPDATA%\\Vflow."""
    if getattr(sys, "frozen", False):
        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(appdata, "Vflow")
    return os.path.dirname(os.path.abspath(__file__))


_RESOURCE_DIR = _get_resource_dir()
_DATA_DIR = _get_data_dir()

# Ensure data directory exists when running as bundle
if getattr(sys, "frozen", False):
    os.makedirs(_DATA_DIR, exist_ok=True)

# Load .env from data dir
load_dotenv(os.path.join(_DATA_DIR, ".env"))

# Descifrado DPAPI: si GROQ_API_KEY no está en claro pero existe GROQ_API_KEY_ENC,
# descifrar y establecer en el entorno de runtime (nunca se escribe a disco en texto plano).
if not os.getenv("GROQ_API_KEY") and os.getenv("GROQ_API_KEY_ENC"):
    try:
        from core.secrets import decrypt as _dpapi_decrypt
        _plain = _dpapi_decrypt(os.getenv("GROQ_API_KEY_ENC"))
        if _plain:
            os.environ["GROQ_API_KEY"] = _plain
        # Si _plain es None → clave de otra máquina o dato corrupto → dejamos vacío
        # para que main.py muestre el FirstRunDialog.
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("config: no se pudo descifrar GROQ_API_KEY_ENC — %s", _e)

# Versión de la aplicación
APP_VERSION = "1.0.0"

# Groq API
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = "whisper-large-v3-turbo"
WHISPER_LANGUAGE = "es"  # Explicit language for accurate accents (é, ó, ñ, etc.)

# Backend de transcripción activo.  Valores posibles: "groq" (default) o "local".
# "groq"  → Groq Whisper API (requiere GROQ_API_KEY, necesita internet).
# "local" → faster-whisper corriendo en CPU (sin internet, requiere modelo descargado).
# Se puede cambiar desde el dashboard sin reiniciar la app.
TRANSCRIPTION_BACKEND = os.getenv("TRANSCRIPTION_BACKEND", "groq")

# Backend local (faster-whisper)
# LOCAL_WHISPER_MODEL: tamaño del modelo a usar.  Opciones: "small" (~466 MB, rápido)
#   o "medium" (~1.5 GB, más preciso).  El modelo se descarga desde Hugging Face la
#   primera vez y queda en LOCAL_MODELS_DIR para uso offline posterior.
LOCAL_WHISPER_MODEL = os.getenv("LOCAL_WHISPER_MODEL", "small")

# LOCAL_MODEL_IDLE_MINUTES: minutos de inactividad antes de liberar el modelo de la RAM.
#   0 = nunca liberar (útil si el equipo tiene RAM suficiente y se usa frecuentemente).
LOCAL_MODEL_IDLE_MINUTES = int(os.getenv("LOCAL_MODEL_IDLE_MINUTES", "10") or "10")

# GROQ_FALLBACK: si "true", cuando el backend local falla (modelo no descargado,
#   error de inferencia, etc.) la app reintenta automáticamente con Groq Whisper API,
#   siempre que GROQ_API_KEY esté configurada.  Solo aplica cuando
#   TRANSCRIPTION_BACKEND=local.  Por defecto "false" (apagado) para garantizar que
#   el audio nunca salga a internet sin consentimiento explícito del usuario.
GROQ_FALLBACK = os.getenv("GROQ_FALLBACK", "false").lower() == "true"

# AUDIO_SOURCE: fuente de captura de audio.  Valores posibles: "mic" (default) o "system".
# "mic"    → micrófono del dispositivo (comportamiento original, sounddevice).
# "system" → audio del sistema vía WASAPI loopback (pyaudiowpatch); captura lo que
#            suena por los altavoces sin necesidad de micrófono.
# Cambiable desde el menú de bandeja o el panel Configuración del dashboard.
AUDIO_SOURCE = os.getenv("AUDIO_SOURCE", "mic")

# VAD_ENABLED: aplica Silero VAD al audio ANTES de enviarlo a la API Groq para
#   recortar silencios (reduce costo, latencia y alucinaciones).  El backend local
#   ya tiene su propio VAD interno, por lo que este ajuste solo afecta a Groq.
#   Apagar en caso de problemas (el audio se envía sin modificar — fail-open).
VAD_ENABLED = os.getenv("VAD_ENABLED", "true").lower() == "true"

# Directorio donde se almacenan los modelos descargados.
# En modo bundle → %APPDATA%\Vflow\models; en dev → <proyecto>/models/
LOCAL_MODELS_DIR = os.path.join(_DATA_DIR, "models")

# Audio
SAMPLE_RATE = 16000
CHANNELS = 1
AUDIO_DTYPE = "int16"
BLOCK_SIZE = 1024

# UI
PILL_WIDTH_IDLE = 30   # idle collapsed line
PILL_WIDTH_RECORDING = 100
PILL_WIDTH_STATUS = 52
PILL_HEIGHT = 34
PILL_HEIGHT_IDLE = 8   # thin line when idle (visible but subtle)
PILL_OPACITY = 0.90
PILL_CORNER_RADIUS = 17
PILL_MARGIN_BOTTOM = 12
LOGO_SIZE = 22

# Logo path (read-only bundled asset)
LOGO_PATH = os.path.join(_RESOURCE_DIR, "logo_small.png")

# Audio Visualizer
NUM_BARS = 20
VIZ_FPS = 60
BAR_GAIN = 8.0

# Chunked recording
CHUNK_SECONDS = 60        # Transcribe every 60s during recording
CHUNK_OVERLAP_SECONDS = 1 # Overlap between chunks to avoid cutting words

# Recording safety net
MAX_RECORDING_SECONDS = 600  # Auto-stop forgotten recordings (e.g. hands-free mode)

# Hotkey
DOUBLE_TAP_INTERVAL = 0.4  # seconds between taps for triple-tap detection
ARMING_DELAY = 0.15         # segundos que Ctrl+Alt deben sostenerse SIN otra tecla antes de grabar
                             # (evita disparos accidentales con atajos Ctrl+Alt+<tecla> de otras apps; 0 = inmediato)

# Database (writable user data)
DB_PATH = os.path.join(_DATA_DIR, "transcriptions.db")

# Exported for other modules
APP_DATA_DIR = _DATA_DIR
