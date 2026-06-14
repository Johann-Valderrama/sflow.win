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

# Modo reunión (captura dual mic + loopback)
# La ventana se cierra en una PAUSA de silencio cerca del objetivo (no en el tiempo exacto),
# para no cortar a mitad de palabra. Con corte por silencio + carryover de prompt se puede
# usar un objetivo más corto (transcript más "en vivo") sin perder calidad.
MEETING_CHUNK_SECONDS = int(os.getenv("MEETING_CHUNK_SECONDS", "12"))      # objetivo mínimo de ventana
MEETING_CHUNK_MAX_SECONDS = int(os.getenv("MEETING_CHUNK_MAX_SECONDS", "22"))  # tope: corta aunque no haya pausa
MEETING_POLL_SECONDS = float(os.getenv("MEETING_POLL_SECONDS", "1.0"))     # cada cuánto revisa el loop
MEETING_SILENCE_MS = int(os.getenv("MEETING_SILENCE_MS", "400"))           # ventana de cola para medir silencio
MEETING_SILENCE_RMS = float(os.getenv("MEETING_SILENCE_RMS", "0.012"))     # RMS (0-1) por debajo = silencio
# Carpeta donde se exporta un .md por reunión (acta + transcript + frontmatter), legible
# por humanos y por agentes (tu OPS puede indexarla). Default: subcarpeta del data dir.
MEETINGS_DIR = os.getenv("MEETINGS_DIR", os.path.join(_DATA_DIR, "meetings"))

# Capa inteligente del modo reunión (Insight Stream + acta LLM)
# INSIGHTS_ENABLED: activa/desactiva temas-pendientes-propuestas en vivo + acta final.
# INSIGHTS_BACKEND: 'groq' (default; reutiliza GROQ_API_KEY). 'local'/'endpoint' = siguiente fase.
# INSIGHTS_MODEL: modelo LLM (Groq). 70B por defecto: calidad alta, ~$0.04/reunión.
# INSIGHTS_MIN_WORDS / INSIGHTS_INTERVAL_SECONDS: disparo del rolling state por delta acumulado
#   (cuántas palabras nuevas) O por tiempo, lo que ocurra primero. Prefiere callar a inventar.
INSIGHTS_ENABLED = os.getenv("INSIGHTS_ENABLED", "true")
INSIGHTS_BACKEND = os.getenv("INSIGHTS_BACKEND", "groq")
INSIGHTS_MODEL = os.getenv("INSIGHTS_MODEL", "llama-3.3-70b-versatile")
INSIGHTS_MIN_WORDS = int(os.getenv("INSIGHTS_MIN_WORDS", "30"))
# Umbral reducido para la PRIMERA actualización: que el análisis aparezca pronto
# (tras la primera ventana con algo de contenido) en vez de sentirse "congelado"
# durante el primer minuto. Las siguientes usan INSIGHTS_MIN_WORDS.
INSIGHTS_FIRST_WORDS = int(os.getenv("INSIGHTS_FIRST_WORDS", "12"))
# Techo de tiempo (fallback): aunque no se acumulen MIN_WORDS, refrescar al menos cada
# este intervalo para que en tramos tranquilos el análisis no se sienta congelado.
# Bajado de 90 a 60s para una cadencia más regular (evidencia: políticas adaptativas
# y deltas pequeños/regulares se perciben más fluidos que saltos grandes y espaciados).
INSIGHTS_INTERVAL_SECONDS = int(os.getenv("INSIGHTS_INTERVAL_SECONDS", "60"))
# Backend 'endpoint' (servidor OpenAI-compatible: LM Studio en local, o servidor on-prem).
# Camino B (probar modelos con LM Studio). El camino A (llama-cpp embebido) queda como
# evolución futura cuando haya un modelo satisfactorio. Para LM Studio: INSIGHTS_BACKEND=endpoint,
# INSIGHTS_ENDPOINT_URL=http://localhost:1234/v1, INSIGHTS_MODEL=<id del modelo, ej. qwen/qwen3.5-9b>.
INSIGHTS_ENDPOINT_URL = os.getenv("INSIGHTS_ENDPOINT_URL", "http://localhost:1234/v1")
INSIGHTS_ENDPOINT_KEY = os.getenv("INSIGHTS_ENDPOINT_KEY", "lm-studio")
# Modelo para el backend 'endpoint' (LM Studio). Separado de INSIGHTS_MODEL (Groq) porque
# los nombres difieren: así conmutar nube↔local desde el dashboard no rompe nada.
# Recomendados (probados): qwen/qwen2.5-vl-7b (calidad) o llama-3.2-3b-instruct (rápido).
INSIGHTS_ENDPOINT_MODEL = os.getenv("INSIGHTS_ENDPOINT_MODEL", "qwen/qwen2.5-vl-7b")
# Consolidación por evento (paso D): una pasada que revisa el análisis con el transcript
# completo (corrige deriva temprana, fusiona duplicados, añade lo que el incremental perdió).
# Se dispara por cambio de tema (con cooldown) o como máximo cada N segundos. UNA sola pasada
# (más iteraciones añaden alucinaciones, según la evidencia). 0 = desactivar consolidación.
INSIGHTS_CONSOLIDATE_SECONDS = int(os.getenv("INSIGHTS_CONSOLIDATE_SECONDS", "240"))
INSIGHTS_CONSOLIDATE_COOLDOWN = int(os.getenv("INSIGHTS_CONSOLIDATE_COOLDOWN", "120"))
# Suelo de max_tokens para el endpoint: los modelos de razonamiento (Qwen3, R1) gastan
# muchos tokens "pensando" antes del JSON; sin holgura se truncan y devuelven vacío.
# Los modelos sin razonamiento paran antes (finish=stop), así que subirlo no los penaliza.
INSIGHTS_ENDPOINT_MAX_TOKENS = int(os.getenv("INSIGHTS_ENDPOINT_MAX_TOKENS", "2500"))

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
