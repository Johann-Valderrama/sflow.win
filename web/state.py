"""Estado compartido de módulo para los blueprints del dashboard (unidad 4.2).

Dueño ÚNICO de: la instancia de ``TranscriptionDB`` (JAMÁS otra ``TranscriptionDB()``
en un blueprint — duplicarla divergiría conexiones/candados), los singletons
``MEETING``/``PROACTIVE``, el estado de descarga del modelo local, el guard del
worker de la cola de URLs, la ruta del ``.env`` de datos, el helper JS compartido
del polling incremental y los helpers transversales de CSRF/settings.

Este módulo NO importa ningún blueprint (evita ciclos): los blueprints importan
de aquí, nunca al revés.
"""

import os
import threading
from urllib.parse import urlparse

from flask import jsonify, request
from dotenv import set_key

from db.database import TranscriptionDB
from config import APP_DATA_DIR
from core.meeting import MEETING
from core.proactive import PROACTIVE

# ---------------------------------------------------------------------------
# Single DB instance (avoids re-running DDL on every request)
# ---------------------------------------------------------------------------
_db = TranscriptionDB()

# ---------------------------------------------------------------------------
# Estado de descarga del modelo local (compartido entre endpoints)
# ---------------------------------------------------------------------------
_download_lock = threading.Lock()
_download_state = {
    "downloading": False,
    "model": None,      # str — nombre del modelo que se está descargando/descargó
    "progress": None,   # float 0.0–1.0 o None
    "error": None,      # str o None
}

_ENV_PATH = os.path.join(APP_DATA_DIR, ".env")

# ---------------------------------------------------------------------------
# Guard del worker serial de la cola de URLs (Fase 3, paso 2). El worker en sí
# (el thread, el loop y _process_next_url_item) vive en
# web/blueprints/url_queue.py; este módulo solo es dueño del lock y del flag
# compartido para que el guard "arrancar una sola vez" sea consistente sin
# importar el blueprint desde aquí (evita el ciclo state -> blueprint).
# ---------------------------------------------------------------------------
_url_worker_lock = threading.Lock()
_url_worker_started = False

# ---------------------------------------------------------------------------
# Helper JS compartido del polling incremental de /api/meeting (unidad 3.1).
# FUENTE ÚNICA: esta constante Python se pasa como variable de contexto Jinja
# `{{ mt_js|safe }}` a LOS DOS templates que la usan (unidad 4.1):
# web/templates/dashboard.html (→ loadMeeting) y web/templates/reunion.html
# (→ loadLive). Son documentos SEPARADOS: si un template olvidara la variable,
# el helper quedaría sin definir en ese documento y loadMeeting/loadLive
# morirían con ReferenceError — hay un test de integración que lo vigila
# (tests/test_meeting_incremental.py::TestHelperPresentInBothDocuments).
# OJO Jinja: este JS entra con `|safe` (sin escapar), pero NO se re-procesa
# como template; aun así, por higiene no debe contener '{{', '{%' ni '{#'
# (llaves simples son seguras).
#
# NOTA (unidad 4.2): el comentario de arriba dice "web/server.py" porque el
# código se movió verbatim desde ahí; la constante ahora vive en web/state.py.
# ---------------------------------------------------------------------------
_MT_INCREMENTAL_JS = """
// Polling incremental de /api/meeting (unidad 3.1). Bloque inyectado desde la
// constante Python _MT_INCREMENTAL_JS (web/server.py) — NO editar en el HTML
// renderizado; la fuente única vive en esa constante. Dos consumidores REALES
// con DOM y ciclos de vida distintos ('home' = panel embebido del dashboard,
// 'live' = vista /reunion) comparten este helper de fetch+cursor. Cada
// consumidor guarda su propio {gen, since} + su copia acumulada de segments
// (el server solo manda el delta desde `since`); el render de cada uno sigue
// recibiendo la lista COMPLETA acumulada, así su lógica de diffing existente
// (comparar longitud previa vs actual para hacer append) no cambia.
// Invalidación: gen distinto o total<since (reunión nueva/reiniciada)
// → se descarta lo acumulado y se repite con since=0 en el mismo poll.
const _mtCursors = {};
async function fetchMeetingIncremental(key) {
    let cur = _mtCursors[key];
    if (!cur) { cur = { gen: null, since: 0, segments: [] }; _mtCursors[key] = cur; }
    let res = await fetch('/api/meeting?since=' + cur.since);
    let data = await res.json();
    if (cur.gen !== null && (data.gen !== cur.gen || data.total < cur.since)) {
        cur.segments = [];
        res = await fetch('/api/meeting?since=0');
        data = await res.json();
    }
    cur.gen = data.gen;
    cur.segments = cur.segments.concat(data.segments || []);
    cur.since = data.total;
    return { status: data.status, insights: data.insights, last_minutes: data.last_minutes, segments: cur.segments };
}
"""

_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _is_local_url(url: str) -> bool:
    """Devuelve True solo si la URL apunta exactamente a un host local.

    Parsea el hostname con urlparse para evitar bypasses por prefijo como
    http://localhost.evil.com (hostname sería "localhost.evil.com", no "localhost").
    Cualquier puerto local es válido; solo el hostname es verificado.
    """
    host = urlparse(url).hostname
    return host in _LOCAL_HOSTNAMES


def _csrf_check():
    """Block cross-origin requests to mutating endpoints."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("Origin", "")
    referer = request.headers.get("Referer", "")
    # Permitir requests sin Origin ni Referer (mismo origen, curl, etc.)
    if not origin and not referer:
        return
    if origin and not _is_local_url(origin):
        return jsonify({"error": "CSRF: origin not allowed"}), 403
    if referer and not origin:
        if not _is_local_url(referer):
            return jsonify({"error": "CSRF: referer not allowed"}), 403


def _set_env_key(key: str, value: str):
    """Write key=value to .env and update the running process environment."""
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    set_key(_ENV_PATH, key, value)
    os.environ[key] = value


# Proveedores cuya API key se guarda cifrada (DPAPI) por equipo, como <ENVVAR>_ENC.
_SECRET_KEYS = {
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    # Secreto de firma HMAC del webhook saliente (unidad 6.1). Mismo mecanismo DPAPI:
    # se guarda como WEBHOOK_SECRET_ENC y NUNCA se devuelve en GET /api/settings.
    "webhook_secret": "WEBHOOK_SECRET",
}


def _save_secret_key(provider: str, value: str) -> bool:
    """Cifra (DPAPI) y persiste una API key como <ENVVAR>_ENC en el .env del usuario,
    elimina cualquier resto en texto plano, y la activa en el proceso actual (sin reiniciar).
    Devuelve True si se guardó."""
    env_var = _SECRET_KEYS.get(provider)
    value = (value or "").strip()
    if not env_var or not value:
        return False
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    try:
        from core.secrets import encrypt as _dpapi_encrypt
        set_key(_ENV_PATH, env_var + "_ENC", _dpapi_encrypt(value))
        try:  # quitar cualquier valor en texto plano que hubiera quedado
            from dotenv import unset_key as _unset
            _unset(_ENV_PATH, env_var)
        except Exception:
            pass
    except Exception:
        # Fallback (p.ej. plataforma sin DPAPI): guardar en claro como último recurso.
        set_key(_ENV_PATH, env_var, value)
    os.environ[env_var] = value
    return True


def _blacklisted_export_roots() -> list[str]:
    """Raíces del sistema donde NUNCA debe apuntar el dead-drop de pendientes.

    Resuelve rutas reales vía variables de entorno (no strings fijos), con
    fallback a los literales de Windows si la variable no existe. Nota:
    %APPDATA% NO se blacklistea completo (solo la subcarpeta Start Menu) para
    que %APPDATA%\\Vflow siga siendo un destino válido.
    """
    roots = [
        os.environ.get("SystemRoot") or r"C:\Windows",
        os.environ.get("ProgramFiles") or r"C:\Program Files",
        os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)",
    ]
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        roots.append(os.path.join(appdata, "Microsoft", "Windows", "Start Menu"))
    return [os.path.normcase(os.path.normpath(r)) for r in roots if r]


def _validate_export_dir(path: str) -> str | None:
    """Valida PENDING_EXPORT_DIR. Devuelve un mensaje de error, o None si es válido.

    Reglas (unidad 1.3): ruta absoluta obligatoria; rechaza carpetas protegidas
    del sistema (Windows, Program Files, Start Menu/Startup); exige que el
    directorio exista SOLO para rutas locales (una ruta UNC \\\\server\\share
    puede estar offline en el momento de guardar y aun así debe aceptarse).
    """
    path = (path or "").strip()
    if not path:
        return None  # vacío = sin configurar, válido (feature apagada)

    expanded = os.path.expandvars(os.path.expanduser(path))
    is_unc = expanded.startswith("\\\\") or expanded.startswith("//")
    normalized = os.path.normpath(expanded)

    if not os.path.isabs(normalized):
        return "Ruta relativa no permitida; usa una ruta absoluta (p. ej. C:\\carpeta o \\\\server\\share\\carpeta)."

    normcased = os.path.normcase(normalized)
    for root in _blacklisted_export_roots():
        if normcased == root or normcased.startswith(root + os.sep):
            return f"Ruta no permitida (carpeta protegida del sistema): {path}"

    if not is_unc and not os.path.isdir(normalized):
        return f"La carpeta no existe: {path}"

    return None


def _validate_briefing_path(path: str) -> str | None:
    """Valida OPS_BRIEFING_PATH. Devuelve un mensaje de error, o None si es válido.

    Solo exige extensión .md (case-insensitive); NO exige existencia — el
    módulo core/ops_briefing.py ya es fail-open total por diseño (unidad 7.1).
    """
    path = (path or "").strip()
    if not path:
        return None  # vacío = apagado, válido
    if not path.lower().endswith(".md"):
        return "La ruta del briefing debe apuntar a un archivo .md"
    return None


def _validate_meeting_retention_days(value: str) -> str | None:
    """Valida MEETING_RETENTION_DAYS. Devuelve un mensaje de error, o None si es válido.

    Solo exige que sea un entero (positivo, cero o negativo); días<=0 se
    interpreta aguas abajo (meetings_prune_older_than) como "conservar
    siempre". Rechazar aquí lo no-numérico evita que quede persistido en el
    .env un valor que luego reviente el GET con un ValueError (fix U3.2: la
    lectura del GET además usa _safe_int_env como segunda red de seguridad).
    """
    value = (value or "").strip()
    if not value:
        return None  # vacío -> tratado como "0" por el default de getenv
    try:
        int(value)
    except ValueError:
        return "El valor debe ser un número entero (días). Usa 0 para conservar siempre."
    return None


def _safe_int_env(key: str, default: int) -> int:
    """Lee una env var como entero sin reventar el caller si el valor no es numérico.

    Un .env editado a mano (o una migración vieja) puede dejar basura en una
    variable que el resto del código asume entera; convertir eso en un 500 en
    /api/settings sería peor que devolver el default.
    """
    raw = os.getenv(key, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default
