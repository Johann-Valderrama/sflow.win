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

# Igual para OPENROUTER_API_KEY (acta / Asistente de reuniones con backend OpenRouter).
if not os.getenv("OPENROUTER_API_KEY") and os.getenv("OPENROUTER_API_KEY_ENC"):
    try:
        from core.secrets import decrypt as _dpapi_decrypt
        _plain = _dpapi_decrypt(os.getenv("OPENROUTER_API_KEY_ENC"))
        if _plain:
            os.environ["OPENROUTER_API_KEY"] = _plain
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("config: no se pudo descifrar OPENROUTER_API_KEY_ENC — %s", _e)

# Igual para ANTHROPIC_API_KEY (backend 'anthropic' de insights — API oficial de Anthropic).
if not os.getenv("ANTHROPIC_API_KEY") and os.getenv("ANTHROPIC_API_KEY_ENC"):
    try:
        from core.secrets import decrypt as _dpapi_decrypt
        _plain = _dpapi_decrypt(os.getenv("ANTHROPIC_API_KEY_ENC"))
        if _plain:
            os.environ["ANTHROPIC_API_KEY"] = _plain
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("config: no se pudo descifrar ANTHROPIC_API_KEY_ENC — %s", _e)

# Igual para WEBHOOK_SECRET (secreto de firma HMAC del webhook saliente, unidad 6.1).
# Se cifra con DPAPI exactamente como las API keys; en runtime se descifra a la variable
# en claro para que core/webhook.py firme el body sin volver a tocar disco.
if not os.getenv("WEBHOOK_SECRET") and os.getenv("WEBHOOK_SECRET_ENC"):
    try:
        from core.secrets import decrypt as _dpapi_decrypt
        _plain = _dpapi_decrypt(os.getenv("WEBHOOK_SECRET_ENC"))
        if _plain:
            os.environ["WEBHOOK_SECRET"] = _plain
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("config: no se pudo descifrar WEBHOOK_SECRET_ENC — %s", _e)

# Versión de la aplicación
APP_VERSION = "1.0.0"

# Groq API
GROQ_MODEL = "whisper-large-v3-turbo"

# Nota: TRANSCRIPTION_BACKEND, LOCAL_WHISPER_MODEL, LOCAL_MODEL_IDLE_MINUTES,
# GROQ_FALLBACK, AUDIO_SOURCE, VAD_ENABLED, GROQ_API_KEY y WHISPER_LANGUAGE ya NO
# tienen constante aquí (eran constantes muertas — sin uso — o una constante-trampa
# que no leía el entorno; unidad 4.3). Las variables de entorno SIGUEN vivas: se
# releen perezosamente en cada call-site (core/*, web/*) para soportar hot-reload
# desde el dashboard sin reiniciar la app. Ver ENV_CATALOG al final de este archivo
# (fuente de verdad programática, verificada por tests/test_env_catalog.py).

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

# Assets web auto-hospedados (Tailwind Play + fuente Inter) — read-only bundled.
# En dev = <proyecto>/web/static; en bundle = %_MEIPASS%/web/static (ver vflow.spec).
WEB_STATIC_DIR = os.path.join(_RESOURCE_DIR, "web", "static")

# Templates Jinja2 del dashboard/reunión — read-only bundled.
# En dev = <proyecto>/web/templates; en bundle = %_MEIPASS%/web/templates (ver vflow.spec).
WEB_TEMPLATES_DIR = os.path.join(_RESOURCE_DIR, "web", "templates")

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

# Webhook saliente al generar el acta (unidad 6.1) — flags de entorno de lectura
# PEREZOSA (se leen en cada uso, en core/webhook.py, NO aquí: así se apagan sin
# reiniciar la app). OPT-IN, apagado por defecto (nada sale a internet sin decisión
# explícita del usuario). Se documentan aquí como catálogo:
#   WEBHOOK_ENABLED (default "false")     — enciende el POST al cerrar una reunión persistida.
#   WEBHOOK_URL (default "")              — destino https del POST (patrón Fireflies).
#   WEBHOOK_SCOPE (default "pendientes")  — "pendientes" (metadatos + pendientes) | "acta"
#       (además minutes_json + capítulos). El transcript crudo NUNCA se envía.
#   WEBHOOK_SECRET (runtime)             — secreto de firma HMAC; en disco vive cifrado como
#       WEBHOOK_SECRET_ENC (DPAPI), descifrado arriba. Nunca se devuelve en GET /api/settings.
#   WEBHOOK_ALLOW_LOCAL (default "false") — opt-in para permitir destinos internos (LAN,
#       loopback) y http:// (anti-SSRF relajado, bajo riesgo del usuario).
#   PENDING_EXPORT_DIR (default "")       — dead-drop LOCAL: si está seteado, escribe una
#       tarea v1 (YAML + vista humana, unidad 5.4) por reunión con ≥1 pendiente a
#       <dir>/vflow-pendientes-<instalacion>-<meeting_id>-<hash8>.md. Local, sin anti-SSRF.
#       Sugerencia de buzón para el consumidor OPS: C:\OPS\_inbox-vflow\ (documentación
#       únicamente; el default real sigue vacío/apagado).

# Modos de dictado por app activa (unidad 6.3) — flags de entorno de lectura
# PEREZOSA (se leen en cada dictado, en core/dictation_modes.py, NO aquí: así se
# apagan sin reiniciar la app). OPT-IN, apagado por defecto.
#   DICTATION_MODES_ENABLED (default "false") — activa el reformateo LLM post-dictado.
#   DICTATION_MODE_MAP (default: ver core/dictation_modes.DEFAULT_MODE_MAP) —
#       mapa "exe:preset,exe:preset,..." (presets: email | chat | codigo). App no
#       mapeada → sin reformateo (pega el texto tal cual, comportamiento actual).

# Proactividad en reunión (Ola 5) — flags de entorno de lectura PEREZOSA (se leen
# en cada uso, en core/meeting.py y core/proactive.py, NO aquí: así se apagan sin
# reiniciar la app). Se documentan aquí como catálogo:
#   PROACTIVE_MODE (default "copilot")            — silent | copilot | trainer.
#   PROACTIVE_DETECT_PREGUNTAS (default "true")   — 5.1: preguntas sin responder.
#   PROACTIVE_DETECT_COMPROMISOS (default "true") — 5.1: compromisos detectados.
#   PROACTIVE_DETECT_ACUERDOS (default "true")    — 5.1: acuerdos vagos (sin fecha/dueño).
#   PROACTIVE_DETECT_CRUZADA (default "true")     — 5.2: memoria cruzada en vivo
#       (tarjeta "El dd/mm se acordó: …" desde actas pasadas; retrieval puro, cero LLM).

# Copiloto con contexto OPS — briefing v1 (unidad 7.1) — flag de entorno de
# lectura PEREZOSA (se lee en cada llamada, en core/ops_briefing.py, NO aquí:
# así se apaga sin reiniciar la app). OPT-IN, apagado por defecto.
#   OPS_BRIEFING_PATH (default "") — ruta a un .md curado por el usuario (proyectos
#       activos, compromisos, metas). Si está seteado, su contenido se inyecta SOLO
#       en el chat en vivo "Preguntar" (core/assistant.py answer_live), nunca en el
#       insight stream. Vacío = apagado.

# Identidad del usuario en prompts (unidad 5.2) — flags de entorno de lectura
# PEREZOSA (se leen en cada llamada, en core/insights.py y core/assistant.py, NO
# aquí: así se actualizan sin reiniciar la app). OPT-IN: USER_NAME vacío = apagado,
# nada se inyecta en ningún prompt.
#   USER_NAME (default "")   — nombre del usuario; motor de la feature. Se inyecta
#       como 1 línea de identidad en el Insight Stream (solo si el modo proactivo
#       no es "silent"), en el acta batch (generate_minutes) y en el Asistente de
#       reuniones (answer/answer_live), siempre con coletilla anti-atribución.
#   USER_ROLE (default "")   — rol del usuario; solo complementa si hay USER_NAME.
#   USER_DOMAIN (default "") — dominio/industria del usuario; solo complementa si
#       hay USER_NAME.

# Capa inteligente del modo reunión (Insight Stream + acta LLM)
# INSIGHTS_ENABLED, INSIGHTS_BACKEND, INSIGHTS_MODEL, INSIGHTS_ENDPOINT_URL,
# INSIGHTS_ENDPOINT_KEY, INSIGHTS_ENDPOINT_MODEL e INSIGHTS_ENDPOINT_MAX_TOKENS ya
# NO tienen constante aquí (eran constantes muertas sin uso real — unidad 4.3); las
# variables de entorno siguen vivas, releídas perezosamente en core/insights.py y
# documentadas en ENV_CATALOG al final de este archivo.
# INSIGHTS_MIN_WORDS / INSIGHTS_INTERVAL_SECONDS: disparo del rolling state por delta acumulado
#   (cuántas palabras nuevas) O por tiempo, lo que ocurra primero. Prefiere callar a inventar.
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
# Consolidación por evento (paso D): una pasada que revisa el análisis con el transcript
# completo (corrige deriva temprana, fusiona duplicados, añade lo que el incremental perdió).
# Se dispara por cambio de tema (con cooldown) o como máximo cada N segundos. UNA sola pasada
# (más iteraciones añaden alucinaciones, según la evidencia). 0 = desactivar consolidación.
INSIGHTS_CONSOLIDATE_SECONDS = int(os.getenv("INSIGHTS_CONSOLIDATE_SECONDS", "240"))
INSIGHTS_CONSOLIDATE_COOLDOWN = int(os.getenv("INSIGHTS_CONSOLIDATE_COOLDOWN", "120"))

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


# ---------------------------------------------------------------------------
# ENV_CATALOG — catálogo único de variables de entorno (unidad 4.3, PLAN-MEJORAS)
# ---------------------------------------------------------------------------
# CONTRATO (opción A — registro documentado, NO una capa de indirección):
#   - Fuente de verdad PROGRAMÁTICA de toda variable de entorno leída en el código
#     de producción (main.py, config.py, core/, web/, db/, mcp_server/). Verificada
#     por tests/test_env_catalog.py, que falla si el código y el catálogo divergen.
#   - Los call-sites SIGUEN haciendo os.getenv(...)/os.environ.get(...) directamente
#     en su propio archivo — la relectura perezosa (hot-reload: cambiar el valor
#     desde el dashboard sin reiniciar la app) es intencional y SAGRADA. Este
#     catálogo documenta, no envuelve ni reemplaza esas llamadas.
#   - Claves por variable:
#       default    -> el literal EXACTO pasado como default en el/los call-site(s)
#                     REALES (consumidores en core/*, web/*, main.py; no el chequeo
#                     de presencia de config.py previo al descifrado DPAPI de un
#                     secreto — ver ENV_KNOWN_DIVERGENCES para esos casos).
#                     None cuando el default NO es un literal (p. ej. un
#                     os.path.join(...) o una constante de otro módulo): el test
#                     de sincronía tolera y documenta estos casos por nombre, no
#                     los compara por valor.
#       kind       -> "static" (config.py lo lee UNA vez al arranque y lo expone
#                     como constante que otros módulos importan) o "lazy" (se
#                     relee en cada call-site).
#       killswitch -> True si es un flag booleano on/off de una feature completa
#                     (no un selector de modo/valor como "mic"/"system").
#       doc        -> una línea en español explicando qué hace.
#       dynamic    -> True si el/los call-site(s) reales leen la variable con un
#                     NOMBRE COMPUTADO (no un string literal en el propio
#                     os.getenv), por lo que el escaneo AST no puede encontrarlos
#                     como lecturas nombradas de esta variable. Documentada aquí a
#                     mano; el test de sincronía la excluye de la comprobación de
#                     default (no hay AST que comparar) pero SÍ exige que exista.
#       known_divergence -> nota (o None) de una divergencia de comportamiento real
#                     y conocida entre call-sites, NO corregida en esta unidad
#                     (deuda documentada en docs/PENDIENTES.md). El test de
#                     sincronía la asserta explícitamente para AVISAR si el bug se
#                     arregla algún día (en cuyo caso la nota sobra y debe borrarse).
#
#   ENV_KNOWN_DIVERGENCES (aparte): variables cuyo DEFAULT literal diverge entre
#   call-sites de forma benigna (ambos valores son falsy y ningún código distingue
#   entre ellos) — típicamente el chequeo de presencia de config.py antes de
#   descifrar un secreto (`os.getenv("X")` → None) contra el default real usado
#   por los consumidores (`os.getenv("X", "")` → ""). El test tolera exactamente
#   los defaults listados en "allowed_defaults" para esa variable.
# ---------------------------------------------------------------------------

ENV_CATALOG = {
    # --- Estáticas: leídas una vez en config.py, exportadas como constante ----
    "MEETING_CHUNK_SECONDS": {
        "default": "12", "kind": "static", "killswitch": False,
        "doc": "Objetivo mínimo (segundos) de ventana de captura en modo reunión.",
    },
    "MEETING_CHUNK_MAX_SECONDS": {
        "default": "22", "kind": "static", "killswitch": False,
        "doc": "Tope máximo (segundos): corta la ventana aunque no haya pausa de silencio.",
    },
    "MEETING_POLL_SECONDS": {
        "default": "1.0", "kind": "static", "killswitch": False,
        "doc": "Cada cuántos segundos el loop de reunión revisa el buffer de audio.",
    },
    "MEETING_SILENCE_MS": {
        "default": "400", "kind": "static", "killswitch": False,
        "doc": "Ventana de cola (ms) para medir silencio y decidir el corte de ventana.",
    },
    "MEETING_SILENCE_RMS": {
        "default": "0.012", "kind": "static", "killswitch": False,
        "doc": "Umbral RMS (0-1): por debajo se considera silencio.",
    },
    "MEETINGS_DIR": {
        "default": None, "kind": "static", "killswitch": False,
        "doc": "Carpeta donde se exporta un .md por reunión (acta+transcript+frontmatter). "
               "Default computado: <data dir>/meetings (no literal, ver nota de 'default').",
    },
    "INSIGHTS_MIN_WORDS": {
        "default": "30", "kind": "static", "killswitch": False,
        "doc": "Palabras nuevas acumuladas que disparan una actualización del Insight Stream.",
    },
    "INSIGHTS_FIRST_WORDS": {
        "default": "12", "kind": "static", "killswitch": False,
        "doc": "Umbral reducido para la PRIMERA actualización del Insight Stream (evita sensación de congelado).",
    },
    "INSIGHTS_INTERVAL_SECONDS": {
        "default": "60", "kind": "static", "killswitch": False,
        "doc": "Techo de tiempo (fallback) entre actualizaciones del Insight Stream aunque no se acumulen palabras.",
    },
    "INSIGHTS_CONSOLIDATE_SECONDS": {
        "default": "240", "kind": "static", "killswitch": False,
        "doc": "Intervalo máximo entre pasadas de consolidación del acta en vivo; 0 desactiva.",
    },
    "INSIGHTS_CONSOLIDATE_COOLDOWN": {
        "default": "120", "kind": "static", "killswitch": False,
        "doc": "Cooldown (segundos) entre consolidaciones disparadas por cambio de tema.",
    },

    # --- Transcripción / audio (lazy) -----------------------------------------
    "TRANSCRIPTION_BACKEND": {
        "default": "groq", "kind": "lazy", "killswitch": False,
        "doc": "Backend de transcripción activo: 'groq' (API, requiere internet) o 'local' (faster-whisper offline).",
    },
    "LOCAL_WHISPER_MODEL": {
        "default": "small", "kind": "lazy", "killswitch": False,
        "doc": "Tamaño del modelo local: 'small' (~466 MB) o 'medium' (~1.5 GB).",
    },
    "LOCAL_MODEL_IDLE_MINUTES": {
        "default": "10", "kind": "lazy", "killswitch": False,
        "doc": "Minutos de inactividad antes de liberar el modelo local de RAM; 0 = nunca liberar.",
    },
    "GROQ_FALLBACK": {
        "default": "false", "kind": "lazy", "killswitch": True,
        "doc": "Si 'true', reintenta con Groq cuando el backend local falla (requiere GROQ_API_KEY).",
    },
    "AUDIO_SOURCE": {
        "default": "mic", "kind": "lazy", "killswitch": False,
        "doc": "Fuente de captura de audio: 'mic' o 'system' (WASAPI loopback).",
    },
    "VAD_ENABLED": {
        "default": "true", "kind": "lazy", "killswitch": True,
        "doc": "Aplica Silero VAD al audio antes de enviarlo a Groq para recortar silencios.",
    },
    "AUDIO_DEVICE_NAME": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Substring del nombre del micrófono a usar; vacío = dispositivo por defecto del sistema.",
    },
    "WHISPER_LANGUAGE": {
        "default": "es", "kind": "lazy", "killswitch": False,
        "doc": "Idioma de entrada para la transcripción (código Whisper).",
    },
    "TRANSLATE_TARGET_LANG": {
        "default": "en", "kind": "lazy", "killswitch": False,
        "doc": "Idioma destino para el modo traducción.",
    },

    # --- Secretos / API keys (lazy) -------------------------------------------
    "GROQ_API_KEY": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Clave de API de Groq (se cifra con DPAPI en disco vía GROQ_API_KEY_ENC).",
    },
    "GROQ_API_KEY_ENC": {
        "default": None, "kind": "lazy", "killswitch": False,
        "doc": "Valor cifrado (DPAPI) de GROQ_API_KEY persistido en disco; variable interna, no se edita a mano.",
    },
    "ANTHROPIC_API_KEY": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Clave de API oficial de Anthropic para el backend de insights 'anthropic'.",
    },
    "ANTHROPIC_API_KEY_ENC": {
        "default": None, "kind": "lazy", "killswitch": False,
        "doc": "Valor cifrado (DPAPI) de ANTHROPIC_API_KEY; variable interna.",
    },
    "OPENROUTER_API_KEY": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Clave de API de OpenRouter para el backend de insights 'openrouter'.",
    },
    "OPENROUTER_API_KEY_ENC": {
        "default": None, "kind": "lazy", "killswitch": False,
        "doc": "Valor cifrado (DPAPI) de OPENROUTER_API_KEY; variable interna.",
    },
    "WEBHOOK_SECRET": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Secreto de firma HMAC-SHA256 del webhook saliente.",
    },
    "WEBHOOK_SECRET_ENC": {
        "default": None, "kind": "lazy", "killswitch": False,
        "doc": "Valor cifrado (DPAPI) de WEBHOOK_SECRET; variable interna.",
    },

    # --- Insights / backends LLM (lazy) ---------------------------------------
    "INSIGHTS_ENABLED": {
        "default": "true", "kind": "lazy", "killswitch": True,
        "doc": "Activa/desactiva el Insight Stream (temas/pendientes/propuestas en vivo) y el acta final.",
    },
    "INSIGHTS_BACKEND": {
        "default": "groq", "kind": "lazy", "killswitch": False,
        "doc": "Backend LLM de insights (fallback global si no hay override por tarea): "
               "groq | endpoint | openrouter | anthropic | claude-cli.",
    },
    "INSIGHTS_BACKEND_BATCH": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Override del backend de insights SOLO para tareas batch (acta/consolidación/chat); vacío = usa INSIGHTS_BACKEND.",
    },
    "INSIGHTS_BACKEND_LIVE": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Override del backend de insights SOLO para el análisis en vivo; vacío = usa INSIGHTS_BACKEND.",
    },
    "INSIGHTS_MODEL": {
        "default": "llama-3.3-70b-versatile", "kind": "lazy", "killswitch": False,
        "doc": "Modelo LLM para el backend Groq de insights.",
    },
    "INSIGHTS_ENDPOINT_URL": {
        "default": "http://localhost:1234/v1", "kind": "lazy", "killswitch": False,
        "doc": "URL del servidor OpenAI-compatible (LM Studio local u on-prem) para el backend 'endpoint'.",
    },
    "INSIGHTS_ENDPOINT_KEY": {
        "default": "lm-studio", "kind": "lazy", "killswitch": False,
        "doc": "API key para el backend 'endpoint' (LM Studio no la valida, pero el cliente OpenAI la exige).",
    },
    "INSIGHTS_ENDPOINT_MODEL": {
        "default": "qwen/qwen2.5-vl-7b", "kind": "lazy", "killswitch": False,
        "doc": "Modelo para el backend 'endpoint' (LM Studio).",
    },
    "INSIGHTS_ENDPOINT_MAX_TOKENS": {
        "default": "2500", "kind": "lazy", "killswitch": False,
        "doc": "Suelo de max_tokens para el backend 'endpoint' (modelos de razonamiento necesitan holgura).",
    },
    "INSIGHTS_FALLBACK": {
        "default": "true", "kind": "lazy", "killswitch": True,
        "doc": "Si 'true', reintenta con groq/openrouter cuando el backend primario de insights falla.",
    },
    "INSIGHTS_FALLBACK_COOLDOWN": {
        "default": "300", "kind": "lazy", "killswitch": False,
        "doc": "Segundos que el circuit breaker de fallback evita reintentar un backend roto.",
    },
    "OPENROUTER_MODEL": {
        "default": "google/gemini-3.1-flash-lite", "kind": "lazy", "killswitch": False,
        "doc": "Modelo LLM para el backend OpenRouter de insights.",
    },
    "OPENROUTER_BASE_URL": {
        "default": "https://openrouter.ai/api/v1", "kind": "lazy", "killswitch": False,
        "doc": "URL base de la API de OpenRouter.",
    },
    "OPENROUTER_REASONING_EFFORT": {
        "default": "medium", "kind": "lazy", "killswitch": False,
        "doc": "Esfuerzo de razonamiento extendido para OpenRouter cuando se solicita reasoning.",
    },
    "ASSISTANT_CONTEXT_BUDGET_CHARS": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Override manual (caracteres) del presupuesto de contexto del prompt; vacío = calculado por backend.",
    },
    "ANTHROPIC_MODEL_BATCH": {
        "default": "claude-sonnet-5", "kind": "lazy", "killswitch": False,
        "doc": "Modelo Anthropic para tareas batch (acta/consolidación/chat) cuando el backend activo es 'anthropic'.",
    },
    "ANTHROPIC_MODEL_LIVE": {
        "default": "claude-haiku-4-5", "kind": "lazy", "killswitch": False,
        "doc": "Modelo Anthropic para el análisis en vivo cuando el backend activo es 'anthropic'.",
    },
    "CLAUDE_CLI_PATH": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Ruta explícita al binario de Claude Code CLI; vacío = autodescubrir (PATH, npm global).",
    },
    "CLAUDE_CLI_MODEL_BATCH": {
        "default": "sonnet", "kind": "lazy", "killswitch": False,
        "doc": "Alias de modelo para tareas batch cuando el backend activo es 'claude-cli'.",
        "known_divergence": (
            "core/insights.py _model() (~línea 218) usa SIEMPRE esta variable para "
            "backend='claude-cli', incluso cuando task='live' — nunca lee "
            "CLAUDE_CLI_MODEL_LIVE desde esa función. El dispatch real de inferencia "
            "(_chat_claude_cli, ~líneas 574-577) SÍ branchea correctamente por task. "
            "Bug conocido, no corregido en unidad 4.3 (ver docs/PENDIENTES.md)."
        ),
    },
    "CLAUDE_CLI_MODEL_LIVE": {
        "default": "haiku", "kind": "lazy", "killswitch": False,
        "doc": "Alias de modelo para el análisis en vivo cuando el backend activo es 'claude-cli'.",
        "known_divergence": (
            "Ver known_divergence de CLAUDE_CLI_MODEL_BATCH: _model() nunca lee esta "
            "variable para backend='claude-cli' (siempre resuelve a CLAUDE_CLI_MODEL_BATCH "
            "en esa función), aunque el dispatch real de inferencia sí la usa para task='live'."
        ),
    },

    # --- Historial / retención (lazy) -----------------------------------------
    "SAVE_HISTORY": {
        "default": "true", "kind": "lazy", "killswitch": True,
        "doc": "Si 'false', las transcripciones se pegan pero nunca se guardan en la base de datos.",
    },
    "HISTORY_RETENTION_DAYS": {
        "default": "0", "kind": "lazy", "killswitch": False,
        "doc": "Días de retención de transcripciones; 0 = conservar siempre. Se aplica al arrancar.",
    },
    "MEETING_RETENTION_DAYS": {
        "default": "0", "kind": "lazy", "killswitch": False,
        "doc": "Días de retención de reuniones (actas+transcripts); 0 = conservar siempre. Se aplica al arrancar.",
    },

    # --- Audio feedback / portapapeles (lazy) ---------------------------------
    "SOUNDS_ENABLED": {
        "default": "true", "kind": "lazy", "killswitch": True,
        "doc": "Habilita los beeps de feedback de audio.",
    },
    "BEEP_VOLUME_STEPS": {
        "default": "2", "kind": "lazy", "killswitch": False,
        "doc": "Volumen del beep (1-10).",
    },
    "RESTORE_CLIPBOARD": {
        "default": "false", "kind": "lazy", "killswitch": True,
        "doc": "Si 'true', restaura el contenido del portapapeles tras pegar.",
    },

    # --- Webhook saliente (unidad 6.1, lazy) ----------------------------------
    "WEBHOOK_ENABLED": {
        "default": "false", "kind": "lazy", "killswitch": True,
        "doc": "Activa el POST saliente firmado al cerrar una reunión persistida con acta.",
    },
    "WEBHOOK_URL": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "URL destino del webhook saliente (patrón Fireflies).",
    },
    "WEBHOOK_SCOPE": {
        "default": "pendientes", "kind": "lazy", "killswitch": False,
        "doc": "Alcance del payload del webhook: 'pendientes' o 'acta'.",
    },
    "WEBHOOK_ALLOW_LOCAL": {
        "default": "false", "kind": "lazy", "killswitch": True,
        "doc": "Si 'true', permite destinos loopback/privados como URL del webhook (desactiva la protección anti-SSRF).",
    },
    "PENDING_EXPORT_DIR": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Carpeta del dead-drop local de pendientes (contrato de tarea v1, YAML "
               "+ vista humana, unidad 5.4); vacío = desactivado. Sugerencia de buzón "
               "para el consumidor OPS: C:\\OPS\\_inbox-vflow\\ (no es el default real; "
               "el usuario lo configura explícitamente desde Ajustes).",
    },

    # --- Modos de dictado por app activa (unidad 6.3, lazy) -------------------
    "DICTATION_MODES_ENABLED": {
        "default": "false", "kind": "lazy", "killswitch": True,
        "doc": "Activa el reformateo LLM post-dictado según la app en foco.",
    },
    "DICTATION_MODE_MAP": {
        "default": None, "kind": "lazy", "killswitch": False,
        "doc": "Mapa 'exe:preset,...' que asigna un preset de reformateo por ejecutable en foco. "
               "Default computado: core.dictation_modes.DEFAULT_MODE_MAP (no literal).",
    },

    # --- Proactividad en reunión (Ola 5, lazy) --------------------------------
    "PROACTIVE_MODE": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Modo proactivo: silent | copilot | trainer. El literal del getenv es '' "
               "(no reconocido); core.proactive.get_mode() cae a 'copilot' por fallback interno.",
    },
    "PROACTIVE_DETECT_PREGUNTAS": {
        "default": "true", "kind": "lazy", "killswitch": True, "dynamic": True,
        "doc": "5.1: detección de preguntas sin responder.",
    },
    "PROACTIVE_DETECT_COMPROMISOS": {
        "default": "true", "kind": "lazy", "killswitch": True, "dynamic": True,
        "doc": "5.1: detección de compromisos adquiridos.",
    },
    "PROACTIVE_DETECT_ACUERDOS": {
        "default": "true", "kind": "lazy", "killswitch": True, "dynamic": True,
        "doc": "5.1: detección de acuerdos vagos (sin fecha/dueño).",
    },
    "PROACTIVE_DETECT_CRUZADA": {
        "default": "true", "kind": "lazy", "killswitch": True,
        "doc": "5.2: memoria cruzada en vivo (retrieval puro contra actas pasadas, cero LLM).",
    },

    # --- Copiloto con contexto OPS (unidad 7.1, lazy) -------------------------
    "OPS_BRIEFING_PATH": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Ruta a un .md curado por el usuario, inyectado SOLO en el chat en vivo 'Preguntar'.",
    },

    # --- Identidad del usuario en prompts (unidad 5.2, lazy) -------------------
    "USER_NAME": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Nombre del usuario; motor de la feature. Vacío = apagado (nada se inyecta).",
    },
    "USER_ROLE": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Rol del usuario (opcional); solo complementa si USER_NAME está seteado.",
    },
    "USER_DOMAIN": {
        "default": "", "kind": "lazy", "killswitch": False,
        "doc": "Dominio/industria del usuario (opcional); solo complementa si USER_NAME está seteado.",
    },
}

# Divergencias de DEFAULT toleradas explícitamente (benignas: ambos valores son
# falsy y ningún código distingue None de "" para estas 4 claves de secreto — ver
# contrato arriba). config.py las lee sin default (None) SOLO en su chequeo de
# presencia previo al descifrado DPAPI; todo consumidor real usa default "".
ENV_KNOWN_DIVERGENCES = {
    "GROQ_API_KEY": {"allowed_defaults": [None, ""]},
    "ANTHROPIC_API_KEY": {"allowed_defaults": [None, ""]},
    "OPENROUTER_API_KEY": {"allowed_defaults": [None, ""]},
    "WEBHOOK_SECRET": {"allowed_defaults": [None, ""]},
}

# Variables de entorno del SISTEMA OPERATIVO (Windows) usadas para resolver rutas
# (APPDATA para %APPDATA%\npm y el cwd neutro de claude-cli; SystemRoot/ProgramFiles*
# para la blacklist anti-SSRF de PENDING_EXPORT_DIR). NO son configuración de Vflow
# (el usuario nunca las setea en su .env) y quedan fuera de ENV_CATALOG a propósito;
# tests/test_env_catalog.py las excluye explícitamente por este motivo.
ENV_CATALOG_EXCLUDED_SYSTEM_VARS = {"APPDATA", "SystemRoot", "ProgramFiles", "ProgramFiles(x86)"}
