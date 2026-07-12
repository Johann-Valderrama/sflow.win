import os
import re
import secrets
import socket
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template, request, send_file
from dotenv import set_key
from db.database import TranscriptionDB
from config import APP_DATA_DIR, MEETINGS_DIR, WEB_STATIC_DIR, WEB_TEMPLATES_DIR
from core import dictionary as _dictionary
from core.meeting import MEETING
from core import meeting_export as _meeting_export
from core import meeting_templates as _meeting_templates
from core import insights as _insights
from core import assistant as _assistant
from core import proactive as _proactive
from core import dictation_modes as _dictation_modes
from core import ops_briefing as _ops_briefing

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
# Worker serial de la cola de URLs (Fase 3, paso 2)
# ---------------------------------------------------------------------------
_url_worker_lock = threading.Lock()
_url_worker_started = False


def _process_next_url_item(worker_db) -> bool:
    """Procesa UN item pendiente de la cola url_queue (una iteración del worker).

    Extraído del ``while True`` de ``_url_queue_worker`` (F8/F9, unidad 0.4) para
    que sea testeable sin depender de un loop infinito.

    - Toma el item 'pending' más antiguo.
    - Lo marca 'processing'.
    - Llama a transcribe_url con on_progress → actualiza stage en DB.
    - Si ok: inserta en transcriptions (respetando SAVE_HISTORY, con
      ``source_queue_id=item_id`` para idempotencia) y marca 'done'. Si la fila
      ya existe (IntegrityError del índice único post-crash), NO es error: se
      loguea y se marca 'done' igual (F9).
    - Si no ok: marca 'error'.

    F8: ``item_id`` se inicializa a None ANTES del try, así el except nunca
    reutiliza el id de una iteración anterior si la excepción ocurre antes de
    tomar el item (p. ej. ``url_queue_next_pending()`` lanza).

    Devuelve True si había un item que procesar (independientemente de si
    terminó en 'done' o 'error'), False si la cola estaba vacía.
    """
    import logging as _log
    import sqlite3
    from core.url_transcribe import transcribe_url  # noqa: PLC0415

    _logger = _log.getLogger(__name__)

    item_id = None
    try:
        item = worker_db.url_queue_next_pending()
        if item is None:
            return False

        item_id = item["id"]
        url = item["url"]
        allow_instagram = bool(item.get("allow_instagram", 0))

        _logger.info("URL queue: procesando id=%d url=%s", item_id, url)
        worker_db.url_queue_set_processing(item_id, "iniciando")

        def _on_progress(stage: str, _id=item_id) -> None:
            try:
                worker_db.url_queue_update_stage(_id, stage)
            except Exception:
                pass

        result = transcribe_url(url, allow_instagram=allow_instagram, on_progress=_on_progress)

        if result["ok"]:
            title = result.get("title") or ""
            if os.getenv("SAVE_HISTORY", "true").lower() == "true":
                model_label = (
                    "youtube-subtitles" if result.get("method") == "subtitles"
                    else "url-audio"
                )
                try:
                    worker_db.insert(
                        text=result["text"],
                        language=result.get("language"),
                        duration_seconds=result.get("duration"),
                        model=model_label,
                        source=result.get("source") or "url",
                        source_queue_id=item_id,
                    )
                except sqlite3.IntegrityError:
                    # Idempotencia post-crash (F9): el proceso murió entre insert()
                    # y url_queue_set_done() en un run anterior; el repair de
                    # huérfanos re-encoló el item y ya se re-insertó una vez con
                    # este source_queue_id. NO es un error de la cola.
                    _logger.info(
                        "URL queue: id=%d ya insertado (idempotencia post-crash), "
                        "no se reintenta ni se marca error",
                        item_id,
                    )
            worker_db.url_queue_set_done(item_id, title)
            _logger.info("URL queue: id=%d completado — %s", item_id, title)
        else:
            error_msg = result.get("error") or "Error desconocido"
            worker_db.url_queue_set_error(item_id, error_msg)
            _logger.warning("URL queue: id=%d error — %s", item_id, error_msg)

    except Exception as exc:
        _logger.error("URL queue worker: excepción inesperada: %s", exc, exc_info=True)
        # Intentar marcar el item como error para no bloquear la cola. item_id
        # es None si la excepción ocurrió ANTES de tomar un item (F8): en ese
        # caso NO se toca ningún item (nunca el de la iteración anterior).
        try:
            if item_id is not None:
                worker_db.url_queue_set_error(item_id, f"Error interno: {exc}")
        except Exception:
            pass

    return True


def _url_queue_worker() -> None:
    """Loop infinito que procesa la cola url_queue de forma serial (FIFO).

    Cada iteración delega en ``_process_next_url_item``. Pausa ~1.5s entre
    items procesados (cortesía anti-baneo), 1.0s si la cola estaba vacía.
    Items 'processing' huérfanos al arranque (crash anterior) se reencolan
    como 'pending'.
    """
    import time
    import logging as _log

    _logger = _log.getLogger(__name__)

    # DB con su propia conexión (thread distinto)
    from db.database import TranscriptionDB  # noqa: PLC0415
    from config import DB_PATH  # noqa: PLC0415
    worker_db = TranscriptionDB(DB_PATH)

    # Reparar items 'processing' huérfanos de un crash anterior
    try:
        repaired = worker_db.url_queue_repair_orphans()
        if repaired > 0:
            _logger.info("Reparados %d items 'processing' huérfanos de crash anterior", repaired)
    except Exception as _exc:
        _logger.warning("No se pudo reparar items processing huérfanos: %s", _exc)

    while True:
        had_item = _process_next_url_item(worker_db)
        time.sleep(1.5 if had_item else 1.0)


def _start_url_queue_worker() -> None:
    """Arranca el worker de la cola de URLs una sola vez (guard contra doble arranque)."""
    global _url_worker_started
    with _url_worker_lock:
        if _url_worker_started:
            return
        _url_worker_started = True
    t = threading.Thread(target=_url_queue_worker, daemon=True, name="url-queue-worker")
    t.start()


app = Flask(__name__, static_folder=WEB_STATIC_DIR, static_url_path="/static", template_folder=WEB_TEMPLATES_DIR)
app.config["JSON_AS_ASCII"] = False
app.config["SECRET_KEY"] = secrets.token_hex(32)

# Single DB instance (avoids re-running DDL on every request)
_db = TranscriptionDB()

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

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YOUTUBE_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:youtube\.com/watch|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/)",
    re.IGNORECASE,
)

_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


def _is_local_url(url: str) -> bool:
    """Devuelve True solo si la URL apunta exactamente a un host local.

    Parsea el hostname con urlparse para evitar bypasses por prefijo como
    http://localhost.evil.com (hostname sería "localhost.evil.com", no "localhost").
    Cualquier puerto local es válido; solo el hostname es verificado.
    """
    host = urlparse(url).hostname
    return host in _LOCAL_HOSTNAMES


@app.before_request
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


@app.route("/")
def index():
    """Sirve la página principal del dashboard de transcripciones."""
    return render_template("dashboard.html", mt_js=_MT_INCREMENTAL_JS)


def _meeting_page_html() -> str:
    """Renderiza web/templates/reunion.html con las variables de plantillas (unidad 4.3)
    generadas desde ``core.meeting_templates.TEMPLATES`` — una sola fuente de verdad para
    los textos de las 4 plantillas, sin duplicarlos a mano en el JS."""
    import json as _json  # noqa: PLC0415 — import local (mismo patrón que el resto del módulo)
    options_html = "\n        ".join(
        f'<option value="{name}">{tpl["label"]}</option>'
        for name, tpl in _meeting_templates.TEMPLATES.items()
    )
    chips_json = _json.dumps(_meeting_templates.chips_map(), ensure_ascii=False)
    return render_template(
        "reunion.html",
        mt_js=_MT_INCREMENTAL_JS,
        template_options_html=options_html,
        chips_json=chips_json,
    )


@app.route("/reunion")
def reunion():
    """Ventana dedicada al modo reunión: en vivo (transcript + análisis + acta) + historial."""
    return _meeting_page_html()


@app.route("/logo")
def logo():
    """Sirve el logo de la app para el dashboard."""
    from config import LOGO_PATH
    return send_file(LOGO_PATH, mimetype="image/png")


@app.route("/api/transcriptions")
def get_transcriptions():
    """Retorna las últimas 200 transcripciones en formato JSON."""
    return jsonify(_db.get_recent(limit=200))


@app.route("/api/stats")
def get_stats():
    """Agregados de uso para las metric cards del dashboard (read-only, indexado)."""
    return jsonify(_db.stats())


@app.route("/api/transcriptions/search")
def search_transcriptions():
    """Búsqueda para la command palette (LIKE sobre todo el historial, no solo las 200 recientes)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    return jsonify(_db.search(q, limit=8))


@app.route("/api/transcriptions/<int:tid>", methods=["DELETE"])
def delete_transcription(tid):
    """Elimina una transcripción individual por su ID."""
    deleted = _db.delete_by_id(tid)
    return jsonify({"deleted": deleted})


@app.route("/api/transcriptions", methods=["DELETE"])
def delete_transcriptions_bulk():
    """Elimina transcripciones en lote por rango: day, week, month o all."""
    range_type = request.args.get("range", "")
    if range_type == "all":
        deleted = _db.delete_all()
    elif range_type == "day":
        date = request.args.get("date", "")
        if not date or not _DATE_RE.match(date):
            return jsonify({"error": "date parameter required (YYYY-MM-DD)"}), 400
        deleted = _db.delete_by_date(date)
    elif range_type == "week":
        since = (datetime.utcnow() - timedelta(weeks=1)).strftime("%Y-%m-%d")
        deleted = _db.delete_since(since)
    elif range_type == "month":
        since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        deleted = _db.delete_since(since)
    else:
        return jsonify({"error": "invalid range"}), 400
    return jsonify({"deleted": deleted})


@app.route("/api/transcriptions/delete-batch", methods=["POST"])
def delete_transcriptions_batch():
    """Elimina múltiples transcripciones por una lista de IDs en el body JSON."""
    data = request.get_json()
    if not data or "ids" not in data:
        return jsonify({"error": "ids field required"}), 400
    try:
        ids = [int(i) for i in data["ids"]]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400
    deleted = _db.delete_by_ids(ids)
    return jsonify({"deleted": deleted})


@app.route("/api/transcriptions/<int:tid>", methods=["PUT"])
def update_transcription(tid):
    """Actualiza el texto de una transcripción existente.

    [O4] Al detectar una corrección manual puntual (1-2 palabras) se generan
    sugerencias de diccionario deshabilitadas (source='suggested'); NUNCA se
    auto-aplican. Best-effort: un fallo aquí no debe impedir guardar la edición.
    """
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "text field required"}), 400
    new_text = data["text"]

    existing = _db.get_by_id(tid)
    updated = _db.update_text(tid, new_text)
    if updated == 0:
        return jsonify({"error": "not found"}), 404

    suggested = 0
    if existing:
        try:
            old_text = existing.get("text") or ""
            pairs = _dictionary.suggest_dictionary_pairs(old_text, new_text)
            for replace_from, replace_to in pairs:
                if _db.add_suggested_entry(replace_from=replace_from, replace_to=replace_to) is not None:
                    suggested += 1
            if suggested:
                _dictionary.invalidate()
        except Exception:  # noqa: BLE001 — best-effort: no debe romper el guardado
            suggested = 0

    return jsonify({"ok": True, "suggested": suggested})


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


@app.route("/api/keys")
def get_api_keys():
    """Estado de cada API key (configurada o no), sin exponer nunca el valor."""
    return jsonify({p: bool(os.getenv(env, "").strip()) for p, env in _SECRET_KEYS.items()})


@app.route("/api/keys", methods=["POST"])
def set_api_keys():
    """Guarda una o varias API keys cifradas. Body JSON: {groq?: "...", openrouter?: "..."}."""
    data = request.get_json(silent=True) or {}
    saved = []
    for provider in _SECRET_KEYS:
        val = data.get(provider)
        if isinstance(val, str) and val.strip() and _save_secret_key(provider, val):
            saved.append(provider)
    if not saved:
        return jsonify({"error": "No se recibió ninguna API key válida."}), 400
    status = {p: bool(os.getenv(env, "").strip()) for p, env in _SECRET_KEYS.items()}
    return jsonify({"ok": True, "saved": saved, "status": status})


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


_SETTINGS_VALIDATORS = {
    "pending_export_dir": _validate_export_dir,
    "ops_briefing_path": _validate_briefing_path,
    "meeting_retention_days": _validate_meeting_retention_days,
}


@app.route("/api/settings")
def get_settings():
    """Devuelve la configuración actual (idioma, micrófono, sonidos, backend)."""
    return jsonify({
        "language": os.getenv("WHISPER_LANGUAGE", "es"),
        "translate_target": os.getenv("TRANSLATE_TARGET_LANG", "en"),
        "device_name": os.getenv("AUDIO_DEVICE_NAME", ""),
        "sounds_enabled": os.getenv("SOUNDS_ENABLED", "true") == "true",
        "beep_volume": int(os.getenv("BEEP_VOLUME_STEPS", "2")),
        "save_history": os.getenv("SAVE_HISTORY", "true").lower() == "true",
        "retention_days": int(os.getenv("HISTORY_RETENTION_DAYS", "0") or 0),
        "transcription_backend": os.getenv("TRANSCRIPTION_BACKEND", "groq"),
        "local_whisper_model": os.getenv("LOCAL_WHISPER_MODEL", "small"),
        "groq_fallback": os.getenv("GROQ_FALLBACK", "false").lower() == "true",
        "audio_source": os.getenv("AUDIO_SOURCE", "mic"),
        "proactive_mode": _proactive.get_mode(),
        "insights_backend": os.getenv("INSIGHTS_BACKEND", "groq"),
        "insights_backend_live": (os.getenv("INSIGHTS_BACKEND_LIVE", "").strip().lower()
                                   or os.getenv("INSIGHTS_BACKEND", "groq").strip().lower()
                                   or "groq"),
        "insights_backend_batch": (os.getenv("INSIGHTS_BACKEND_BATCH", "").strip().lower()
                                    or os.getenv("INSIGHTS_BACKEND", "groq").strip().lower()
                                    or "groq"),
        "insights_endpoint_model": os.getenv("INSIGHTS_ENDPOINT_MODEL", "qwen/qwen2.5-vl-7b"),
        "anthropic_model_live": os.getenv("ANTHROPIC_MODEL_LIVE", "claude-haiku-4-5"),
        "anthropic_model_batch": os.getenv("ANTHROPIC_MODEL_BATCH", "claude-sonnet-5"),
        "claude_cli_model_batch": os.getenv("CLAUDE_CLI_MODEL_BATCH", "sonnet"),
        "insights_fallback": os.getenv("INSIGHTS_FALLBACK", "true").strip().lower() == "true",
        "claude_cli_available": _insights._claude_cli_path() is not None,
        "has_groq_key": bool(os.getenv("GROQ_API_KEY", "").strip()),
        "has_openrouter_key": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        # Webhook saliente (unidad 6.1). El secreto NUNCA se devuelve: solo un booleano.
        "webhook_enabled": os.getenv("WEBHOOK_ENABLED", "false").strip().lower() == "true",
        "webhook_url": os.getenv("WEBHOOK_URL", ""),
        "webhook_scope": (os.getenv("WEBHOOK_SCOPE", "pendientes").strip().lower() or "pendientes"),
        "webhook_allow_local": os.getenv("WEBHOOK_ALLOW_LOCAL", "false").strip().lower() == "true",
        "has_webhook_secret": bool(os.getenv("WEBHOOK_SECRET", "").strip()),
        "pending_export_dir": os.getenv("PENDING_EXPORT_DIR", ""),
        # Modos de dictado por app activa (unidad 6.3)
        "dictation_modes_enabled": os.getenv("DICTATION_MODES_ENABLED", "false").strip().lower() == "true",
        "dictation_mode_map": os.getenv("DICTATION_MODE_MAP", _dictation_modes.DEFAULT_MODE_MAP),
        # Copiloto con contexto OPS — briefing v1 (unidad 7.1)
        "ops_briefing_path": os.getenv("OPS_BRIEFING_PATH", ""),
        # Retención opcional de reuniones (unidad 3.2). Default 0 = conservar
        # siempre; es la única operación destructiva de este plan, por eso
        # _safe_int_env nunca deja que un valor corrupto tumbe este GET.
        "meeting_retention_days": _safe_int_env("MEETING_RETENTION_DAYS", 0),
    })


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Guarda configuración en .env y actualiza el proceso en ejecución."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400
    allowed = {
        "language": "WHISPER_LANGUAGE",
        "translate_target": "TRANSLATE_TARGET_LANG",
        "device_name": "AUDIO_DEVICE_NAME",
        "sounds_enabled": "SOUNDS_ENABLED",
        "beep_volume": "BEEP_VOLUME_STEPS",
        "save_history": "SAVE_HISTORY",
        "retention_days": "HISTORY_RETENTION_DAYS",
        "transcription_backend": "TRANSCRIPTION_BACKEND",
        "local_whisper_model": "LOCAL_WHISPER_MODEL",
        "groq_fallback": "GROQ_FALLBACK",
        "proactive_mode": "PROACTIVE_MODE",
        "insights_backend": "INSIGHTS_BACKEND",
        "insights_backend_live": "INSIGHTS_BACKEND_LIVE",
        "insights_backend_batch": "INSIGHTS_BACKEND_BATCH",
        "insights_fallback": "INSIGHTS_FALLBACK",
        "insights_endpoint_model": "INSIGHTS_ENDPOINT_MODEL",
        "audio_source": "AUDIO_SOURCE",
        "anthropic_model_live": "ANTHROPIC_MODEL_LIVE",
        "anthropic_model_batch": "ANTHROPIC_MODEL_BATCH",
        "claude_cli_model_batch": "CLAUDE_CLI_MODEL_BATCH",
        # Webhook saliente (unidad 6.1). El secreto NO va aquí: se guarda cifrado por
        # /api/keys (write-only). Estas son las opciones no-secretas del webhook.
        "webhook_enabled": "WEBHOOK_ENABLED",
        "webhook_url": "WEBHOOK_URL",
        "webhook_scope": "WEBHOOK_SCOPE",
        "webhook_allow_local": "WEBHOOK_ALLOW_LOCAL",
        "pending_export_dir": "PENDING_EXPORT_DIR",
        # Modos de dictado por app activa (unidad 6.3)
        "dictation_modes_enabled": "DICTATION_MODES_ENABLED",
        "dictation_mode_map": "DICTATION_MODE_MAP",
        # Copiloto con contexto OPS — briefing v1 (unidad 7.1)
        "ops_briefing_path": "OPS_BRIEFING_PATH",
        # Retención opcional de reuniones (unidad 3.2)
        "meeting_retention_days": "MEETING_RETENTION_DAYS",
    }
    errors: dict[str, str] = {}
    for field, env_key in allowed.items():
        if field not in data:
            continue
        value = str(data[field]).strip()
        validator = _SETTINGS_VALIDATORS.get(field)
        if validator is not None:
            err = validator(value)
            if err:
                errors[field] = err
                continue  # campo rechazado: no se persiste; los demás siguen su curso
        _set_env_key(env_key, value)

    # Si cambió la ruta del briefing (y fue válida), invalidar la caché para que
    # se vea sin esperar el TTL de 60s (core/ops_briefing.py).
    if "ops_briefing_path" in data and "ops_briefing_path" not in errors:
        _ops_briefing.invalidate()

    # Si se activó el backend local y el modelo está descargado, disparar warmup
    if data.get("transcription_backend") == "local":
        _trigger_local_warmup_if_ready()

    if errors:
        return jsonify({"error": errors}), 400

    return jsonify({"ok": True})


@app.route("/api/microphones")
def get_microphones():
    """Lista los dispositivos de entrada de audio (excluye salidas/speakers)."""
    import sounddevice as sd
    mics = [
        {"index": i, "name": dev["name"]}
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] > 0 and dev["max_output_channels"] == 0
    ]
    return jsonify(mics)


def _trigger_local_warmup_if_ready() -> None:
    """Lanza warmup del backend local en un thread de fondo si el modelo está descargado.

    Usa el singleton de ``get_backend('local')`` para precalentar la misma
    instancia que usa ``Transcriber``, de modo que el warmup sea efectivo.
    """
    def _do_warmup():
        try:
            from core.backends import get_backend  # noqa: PLC0415
            b = get_backend("local")
            if b.is_ready():
                b.warmup()
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning("Warmup del backend local fallido: %s", exc)

    threading.Thread(target=_do_warmup, daemon=True).start()


def _run_model_download(model_name: str) -> None:
    """Descarga el modelo faster-whisper en un thread de fondo.

    Actualiza ``_download_state`` con progreso (basado en heurística de tiempo
    si huggingface_hub no reporta progreso granular) y estado final.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    with _download_lock:
        _download_state["downloading"] = True
        _download_state["model"] = model_name
        _download_state["progress"] = 0.0
        _download_state["error"] = None

    try:
        from core.backends.local_backend import _get_models_dir  # noqa: PLC0415
        from faster_whisper import WhisperModel                   # noqa: PLC0415

        models_dir = _get_models_dir()
        os.makedirs(models_dir, exist_ok=True)

        _logger.info("Iniciando descarga del modelo '%s' en '%s'", model_name, models_dir)

        # faster-whisper descarga automáticamente si el modelo no está en download_root.
        # No expone progreso granular, así que usamos una heurística de tiempo.
        # El progreso se simula de 0→0.9 durante la descarga.
        _progress_stop = threading.Event()

        def _fake_progress():
            start = __import__("time").time()
            # Tamaños aproximados: small~466MB, medium~1.5GB.  Asumimos ~5 MB/s.
            sizes = {"small": 466, "medium": 1500}
            mb = sizes.get(model_name, 500)
            total_est = mb / 5.0  # segundos estimados
            while not _progress_stop.is_set():
                elapsed = __import__("time").time() - start
                prog = min(0.9, elapsed / max(total_est, 1))
                with _download_lock:
                    _download_state["progress"] = prog
                __import__("time").sleep(1)

        prog_thread = threading.Thread(target=_fake_progress, daemon=True)
        prog_thread.start()

        try:
            # Cargar el modelo fuerza la descarga si no existe
            WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                download_root=models_dir,
            )
        finally:
            _progress_stop.set()

        with _download_lock:
            _download_state["progress"] = 1.0
            _download_state["downloading"] = False
            _download_state["error"] = None
        _logger.info("Descarga del modelo '%s' completada", model_name)

    except Exception as exc:
        _logger.error("Error durante descarga del modelo '%s': %s", model_name, exc)
        with _download_lock:
            _download_state["downloading"] = False
            _download_state["progress"] = None
            _download_state["error"] = str(exc)


@app.route("/api/local-model/status")
def local_model_status():
    """Devuelve el estado del modelo local: descargado, descargando, progreso, error.

    Si ``_download_state`` corresponde a un modelo distinto del configurado
    actualmente (``LOCAL_WHISPER_MODEL``), no se exponen progress ni error de
    esa descarga para evitar mostrar información obsoleta.
    """
    model_name = os.getenv("LOCAL_WHISPER_MODEL", "small")
    try:
        from core.backends.local_backend import _is_model_downloaded  # noqa: PLC0415
        downloaded = _is_model_downloaded(model_name)
    except Exception:
        downloaded = False

    with _download_lock:
        state = dict(_download_state)

    # Si el estado de descarga pertenece a otro modelo, ignorarlo.
    state_for_current = state.get("model") == model_name or state.get("model") is None
    if not state_for_current:
        state = {"downloading": False, "model": model_name, "progress": None, "error": None}

    return jsonify({
        "model": model_name,
        "downloaded": downloaded,
        "downloading": state["downloading"] if state_for_current else False,
        "progress": state["progress"] if state_for_current else None,
        "error": state["error"] if state_for_current else None,
    })


@app.route("/api/local-model/download", methods=["POST"])
def local_model_download():
    """Inicia la descarga del modelo local en un thread de fondo."""
    data = request.get_json() or {}
    model_name = data.get("model", os.getenv("LOCAL_WHISPER_MODEL", "small")).strip().lower()
    if model_name not in ("small", "medium"):
        return jsonify({"error": "Modelo no soportado; usa 'small' o 'medium'"}), 400

    with _download_lock:
        if _download_state["downloading"]:
            return jsonify({"ok": True, "message": "Descarga ya en curso"})

    # Actualizar la env si cambió el modelo seleccionado
    _set_env_key("LOCAL_WHISPER_MODEL", model_name)

    thread = threading.Thread(target=_run_model_download, args=(model_name,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "model": model_name})


@app.route("/api/dictionary")
def get_dictionary():
    """Retorna todas las entradas del diccionario personal, incluyendo budget de vocabulario."""
    entries = _db.list_dictionary()
    budget = _dictionary.vocab_budget_info()
    return jsonify({"entries": entries, "budget": budget})


@app.route("/api/dictionary/suggested")
def get_suggested_dictionary():
    """Bandeja de revisión: entradas sugeridas (source='suggested') pendientes de aceptar/descartar."""
    return jsonify({"entries": _db.list_suggested_dictionary()})


@app.route("/api/dictionary/suggested/<int:eid>/accept", methods=["POST"])
def accept_suggested_dictionary(eid):
    """Acepta una sugerencia: enabled=1, source pasa a 'manual'."""
    updated = _db.accept_suggested_entry(eid)
    if updated == 0:
        return jsonify({"error": "not found"}), 404
    _dictionary.invalidate()
    return jsonify({"ok": True})


@app.route("/api/dictionary/suggested/<int:eid>", methods=["DELETE"])
def discard_suggested_dictionary(eid):
    """Descarta una sugerencia (DELETE directo; no toca entradas manuales)."""
    deleted = _db.delete_dictionary_entry(eid)
    if deleted == 0:
        return jsonify({"error": "not found"}), 404
    return "", 204


@app.route("/api/dictionary/export")
def export_dictionary():
    """Exporta el diccionario como CSV (replace_from,replace_to,pinned)."""
    import io
    import csv as _csv
    entries = _db.list_dictionary()
    output = io.StringIO()
    writer = _csv.writer(output)
    writer.writerow(["replace_from", "replace_to", "pinned"])
    for e in entries:
        writer.writerow([e.get("replace_from") or "", e.get("replace_to", ""), e.get("pinned", 0)])
    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM para Excel
    return app.response_class(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=vflow-diccionario.csv"},
    )


@app.route("/api/dictionary/import", methods=["POST"])
def import_dictionary():
    """Importa entradas desde CSV (multipart file o body texto). Límite 1000 filas."""
    import io
    import csv as _csv

    # Aceptar multipart (campo 'file') o body CSV crudo
    if request.files and "file" in request.files:
        f = request.files["file"]
        content = f.read().decode("utf-8-sig", errors="replace")
    else:
        content = request.get_data(as_text=True)

    if not content.strip():
        return jsonify({"error": "empty body"}), 400

    # Detectar separador (Excel en español exporta con ';')
    header_line = content.lstrip().splitlines()[0]
    delimiter = ";" if header_line.count(";") > header_line.count(",") else ","
    reader = _csv.DictReader(io.StringIO(content), delimiter=delimiter)
    if reader.fieldnames is None or "replace_to" not in reader.fieldnames:
        return jsonify({"error": "CSV sin columna 'replace_to' (cabecera esperada: replace_from,replace_to,pinned)"}), 400
    imported = 0
    skipped = 0
    row_count = 0
    for row in reader:
        if row_count >= 1000:
            skipped += 1
            continue
        row_count += 1
        replace_to = (row.get("replace_to") or "").strip()
        replace_from = (row.get("replace_from") or "").strip() or None
        pinned_val = (row.get("pinned") or "0").strip()
        pinned = pinned_val in ("1", "true", "yes")
        # Validar
        if not replace_to or len(replace_to) > 100:
            skipped += 1
            continue
        if replace_from is not None:
            if len(replace_from) > 100 or replace_from.lower() == replace_to.lower():
                skipped += 1
                continue
        try:
            eid = _db.add_dictionary_entry(replace_to=replace_to, replace_from=replace_from)
            if pinned:
                _db.set_dictionary_pinned(eid, True)
            imported += 1
        except Exception:
            skipped += 1
    _dictionary.invalidate()
    return jsonify({"imported": imported, "skipped": skipped})


@app.route("/api/dictionary", methods=["POST"])
def add_dictionary_entry():
    """Añade o actualiza una entrada del diccionario (upsert por replace_from)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "replace_to is required"}), 400
    replace_to = data.get("replace_to", "").strip()
    if not replace_to:
        return jsonify({"error": "replace_to is required"}), 400
    if len(replace_to) > 100:
        return jsonify({"error": "replace_to must be 100 characters or fewer"}), 400
    replace_from = data.get("replace_from", "").strip() or None
    if replace_from is not None:
        if len(replace_from) > 100:
            return jsonify({"error": "replace_from must be 100 characters or fewer"}), 400
        if replace_from.lower() == replace_to.lower():
            return jsonify({"error": "replace_from and replace_to must differ"}), 400
    entry_id = _db.add_dictionary_entry(replace_to=replace_to, replace_from=replace_from)
    _dictionary.invalidate()
    return jsonify({"id": entry_id}), 201


@app.route("/api/dictionary/<int:eid>", methods=["DELETE"])
def delete_dictionary_entry(eid):
    """Elimina una entrada del diccionario por ID."""
    deleted = _db.delete_dictionary_entry(eid)
    if deleted == 0:
        return jsonify({"error": "not found"}), 404
    _dictionary.invalidate()
    return "", 204


@app.route("/api/dictionary/<int:eid>", methods=["PATCH"])
def patch_dictionary_entry(eid):
    """Activa/desactiva o fija/desfija una entrada del diccionario."""
    data = request.get_json()
    if data is None or ("enabled" not in data and "pinned" not in data):
        return jsonify({"error": "enabled or pinned field required"}), 400
    updated = 0
    if "enabled" in data:
        updated = _db.set_dictionary_enabled(eid, bool(data["enabled"]))
    if "pinned" in data:
        updated = _db.set_dictionary_pinned(eid, bool(data["pinned"]))
    if updated == 0:
        return jsonify({"error": "not found"}), 404
    _dictionary.invalidate()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Endpoints de la cola de URLs (Fase 3, paso 2)
# ---------------------------------------------------------------------------

_URL_LINE_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _parse_url_body(data: dict) -> tuple[list[str], list[str]]:
    """Extrae URLs válidas y rechazadas del body JSON.

    Acepta {text: "...\\n..."} (multilínea) o {urls: [...]} más allow_instagram.
    Devuelve (validas, rechazadas).
    """
    from core.url_transcribe import detect_platform  # noqa: PLC0415

    raw_lines: list[str] = []
    if "urls" in data:
        raw_lines = [str(u).strip() for u in (data["urls"] or [])]
    elif "text" in data:
        raw_lines = [line.strip() for line in str(data.get("text", "")).splitlines()]
    else:
        raw_lines = []

    validas: list[str] = []
    rechazadas: list[str] = []
    for line in raw_lines:
        if not line:
            continue
        plat = detect_platform(line)
        if plat is None:
            rechazadas.append(line)
        else:
            validas.append(line)
    return validas, rechazadas


@app.route("/api/url-queue", methods=["POST"])
def url_queue_enqueue():
    """Encola una o varias URLs para transcripción en background.

    Body JSON:
        {text: "url1\\nurl2\\n..."} o {urls: ["url1", "url2"]}
        Opcional: {allow_instagram: true}

    Responde:
        {enqueued: N, rejected: [...]}
    """
    from core.url_transcribe import detect_platform  # noqa: PLC0415

    data = request.get_json() or {}
    allow_instagram = bool(data.get("allow_instagram", False))

    validas, rechazadas = _parse_url_body(data)

    enqueued = 0
    for url in validas:
        platform = detect_platform(url)
        _db.url_queue_enqueue(url, platform=platform, allow_instagram=allow_instagram)
        enqueued += 1

    return jsonify({"enqueued": enqueued, "rejected": rechazadas})


@app.route("/api/url-queue")
def url_queue_list():
    """Devuelve la lista completa de items de la cola y un resumen por status."""
    items = _db.url_queue_list()
    summary = _db.url_queue_summary()
    return jsonify({"items": items, "summary": summary})


@app.route("/api/url-queue/clear", methods=["POST"])
def url_queue_clear():
    """Elimina filas con status 'done' o 'error'. Deja pending/processing intactos."""
    deleted = _db.url_queue_clear_finished()
    return jsonify({"deleted": deleted})


@app.route("/api/url-queue/cancel-pending", methods=["POST"])
def url_queue_cancel_pending():
    """Elimina filas 'pending' sin tocar la que está procesando."""
    deleted = _db.url_queue_cancel_pending()
    return jsonify({"deleted": deleted})


# ---------------------------------------------------------------------------
# Modo reunión (captura dual mic + loopback)
# ---------------------------------------------------------------------------

@app.route("/api/meeting", methods=["GET"])
def meeting_status():
    """Devuelve el estado, el transcript en vivo y el Insight Stream (para polling).

    Polling incremental (unidad 3.1, `?since=N`): si se pasa `since` (nº de
    segmentos que el cliente ya tiene), la respuesta trae SOLO el delta de
    segmentos desde ese índice (más status/insights/last_minutes completos, que
    ya son livianos) en vez del transcript entero — shape
    `{gen, status, insights, last_minutes, segments_from, segments, total}`.

    Sin `since` (ausente o no-parseable como entero — `request.args.get(...,
    type=int)` devuelve None en ambos casos): respuesta COMPLETA, byte-idéntica
    al modo legado (compatibilidad con cualquier otro consumidor del endpoint).
    `since` negativo se trata como 0 (clamp); `since` > total también clampa
    (delta vacío, sin 500).
    """
    since = request.args.get("since", type=int)
    # El slice se hace aquí, sobre la lista que ya devuelve transcript_segments()
    # (su sorted() interno se conserva sin cambios).
    segments = MEETING.transcript_segments()
    if since is None:
        return jsonify({
            "status": MEETING.status(),
            "segments": segments,
            "insights": MEETING.get_insights(),
            "last_minutes": MEETING.get_last_minutes(),  # acta de la última reunión terminada
        })

    total = len(segments)
    since = max(0, min(since, total))
    # Orden de lectura elegido para la "foto atómica" (gen + segmentos): los
    # SEGMENTOS se leen PRIMERO y el GEN se lee DESPUÉS. Motivo: get_generation()
    # y transcript_segments() toman el lock por separado (no se cambia la firma
    # de transcript_segments() para fusionarlos en una sola sección crítica), así
    # que una start() concurrente puede colarse entre las dos lecturas. Con este
    # orden, si eso ocurre, el `gen` reportado será el de la generación NUEVA (o
    # igual) respecto a los segmentos ya leídos — nunca uno viejo emparejado con
    # segmentos nuevos. Eso garantiza que el cliente SIEMPRE detecte el cambio de
    # generación por el mismatch de `gen` (invalidación primaria), en vez de
    # depender solo de `total < since` (que tiene un caso límite si los tamaños
    # coinciden por casualidad justo en el boundary).
    gen = MEETING.get_generation()
    return jsonify({
        "gen": gen,
        "status": MEETING.status(),
        "insights": MEETING.get_insights(),
        "last_minutes": MEETING.get_last_minutes(),
        "segments_from": since,
        "segments": segments[since:],
        "total": total,
    })


@app.route("/api/meeting/start", methods=["POST"])
def meeting_start():
    """Inicia una reunión (captura dual mic + audio del sistema)."""
    res = MEETING.start()
    if res.get("ok"):
        code = 200
    elif res.get("stopping"):
        # F1 (fix concurrencia): stop() de la reunión anterior sigue drenando/
        # generando el acta. 409 Conflict — no es un error de servidor (500), es
        # un estado transitorio esperado: reintentar en unos segundos basta.
        code = 409
    else:
        code = 500
    return jsonify(res), code


@app.route("/api/meeting/template", methods=["POST"])
def meeting_set_template():
    """Cambia la plantilla activa (general/ventas/one_on_one/clase, unidad 4.3).

    Única fuente de verdad en el servidor (MEETING._template): el hotkey AltGr+R
    (proceso Python) y el dropdown del dashboard ven siempre el mismo estado.
    """
    data = request.get_json(silent=True) or {}
    name = str(data.get("template", "")).strip()
    ok = MEETING.set_template(name)
    return jsonify({"ok": ok, "template": MEETING.get_template()}), (200 if ok else 400)


@app.route("/api/meeting/stop", methods=["POST"])
def meeting_stop():
    """Detiene la reunión, persiste el acta y devuelve el transcript final."""
    res = MEETING.stop()
    return jsonify(res)


@app.route("/api/meeting/highlight", methods=["POST"])
def meeting_highlight():
    """Marca el instante actual como momento destacado (equivalente web de AltGr+H)."""
    item = MEETING.add_highlight()
    return jsonify({"ok": item is not None, "item": item})


@app.route("/api/meeting/note", methods=["POST"])
def meeting_note():
    """Añade una nota rápida al instante actual de la reunión."""
    data = request.get_json(silent=True) or {}
    item = MEETING.add_note(data.get("text", ""))
    return jsonify({"ok": item is not None, "item": item})


@app.route("/api/meeting/feedback", methods=["POST"])
def meeting_feedback():
    """Registra el feedback ✓/✗ de una tarjeta de pendiente (único push en vivo)."""
    data = request.get_json(silent=True) or {}
    item = MEETING.add_feedback(
        data.get("key", ""),
        data.get("tipo", ""),
        data.get("texto", ""),
        data.get("value"),
    )
    return jsonify({"ok": item is not None, "item": item})


@app.route("/api/meeting/pause", methods=["POST"])
def meeting_pause():
    """Pausa la captura de la reunión (congela el reloj)."""
    res = MEETING.pause()
    return jsonify(res)


@app.route("/api/meeting/resume", methods=["POST"])
def meeting_resume():
    """Reanuda la captura de la reunión tras una pausa."""
    res = MEETING.resume()
    return jsonify(res)


@app.route("/api/meetings", methods=["GET"])
def meetings_list():
    """Lista de reuniones pasadas (sin transcript completo) para el historial.

    Cada fila incluye "metrics" parseado (dict o None) para la tarjeta del
    historial (unidad 3.2); el metrics_json crudo no se expone en la lista.
    """
    import json as _json
    meetings = _db.meetings_recent(limit=200)
    for m in meetings:
        try:
            m["metrics"] = _json.loads(m.pop("metrics_json", None) or "null")
        except Exception:  # noqa: BLE001
            m["metrics"] = None
    return jsonify({"meetings": meetings})


@app.route("/api/meetings/<int:meeting_id>", methods=["GET"])
def meeting_detail(meeting_id):
    """Devuelve una reunión completa (transcript + acta + insights) para el visor."""
    m = _db.meeting_get(meeting_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    import json as _json
    for k in ("minutes_json", "insights_json", "segments_json", "chapters_json", "metrics_json"):
        try:
            m[k.replace("_json", "")] = _json.loads(m.get(k) or "null")
        except Exception:  # noqa: BLE001
            m[k.replace("_json", "")] = None
    return jsonify(m)


@app.route("/api/meetings/<int:meeting_id>/chapters", methods=["POST"])
def meeting_chapters_generate(meeting_id):
    """Genera (o regenera) la línea de tiempo de capítulos de una reunión."""
    m = _db.meeting_get(meeting_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    import json as _json
    try:
        segments = _json.loads(m.get("segments_json") or "[]") or []
    except Exception:  # noqa: BLE001
        segments = []
    chapters = _insights.generate_chapters(m.get("transcript") or "", segments)
    _db.meeting_set_chapters(meeting_id, _json.dumps(chapters, ensure_ascii=False))
    return jsonify({"chapters": chapters})


@app.route("/api/meetings/<int:meeting_id>/delete", methods=["POST"])
def meeting_delete_endpoint(meeting_id):
    """Elimina una reunión (DB + su .md)."""
    n = _db.meeting_delete(meeting_id)
    _meeting_export.delete_meeting_files(MEETINGS_DIR, meeting_id)
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/meetings/clear", methods=["POST"])
def meetings_clear():
    """Elimina TODAS las reuniones (DB + .md)."""
    n = _db.meetings_delete_all()
    _meeting_export.clear_all_files(MEETINGS_DIR)
    return jsonify({"ok": True, "deleted": n})


@app.route("/api/meetings/open-folder", methods=["POST"])
def meetings_open_folder():
    """Abre en el Explorador la carpeta donde se guardan los .md de reuniones."""
    try:
        os.makedirs(MEETINGS_DIR, exist_ok=True)
        os.startfile(MEETINGS_DIR)  # noqa: S606 — Windows; abre el Explorador
        return jsonify({"ok": True, "path": MEETINGS_DIR})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc), "path": MEETINGS_DIR}), 500


@app.route("/api/meetings/search")
def meetings_search_api():
    """Busca en el historial de reuniones por texto completo (FTS5 o LIKE fallback)."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    except (TypeError, ValueError):
        limit = 50
    results = _db.meetings_search(q, limit=limit)
    return jsonify({"results": results, "query": q})


@app.route("/api/meetings/export", methods=["POST"])
def meetings_export():
    """Backfill: exporta todas las reuniones de la DB a Markdown."""
    try:
        n = _meeting_export.export_all(_db, MEETINGS_DIR)
        return jsonify({"ok": True, "exported": n, "path": MEETINGS_DIR})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/meetings/chat", methods=["POST"])
def meetings_chat():
    """Chat multi-turno con el Asistente de reuniones sobre el historial de reuniones."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "mensaje vacío"}), 400
    history = data.get("history") or []
    _mid = data.get("meeting_id")
    try:
        meeting_id = int(_mid) if _mid not in (None, "") else None
    except (TypeError, ValueError):
        meeting_id = None
    try:
        max_tokens = min(max(int(data.get("max_tokens", 1024)), 256), 2048)
    except (TypeError, ValueError):
        max_tokens = 1024
    # Normalizar parámetro reasoning: True/False/\"auto\"
    _raw_reasoning = data.get("reasoning")
    if _raw_reasoning is None:
        reasoning = "auto"
    elif isinstance(_raw_reasoning, bool):
        reasoning = _raw_reasoning
    elif isinstance(_raw_reasoning, str):
        if _raw_reasoning.lower() == "true":
            reasoning = True
        elif _raw_reasoning.lower() == "false":
            reasoning = False
        else:
            reasoning = "auto"
    else:
        reasoning = "auto"
    # Ruteo vivo vs DB (unidad 2.3): el cliente puede forzar el modo con {"live": true/false};
    # por defecto, si hay reunión activa y no se pidió una reunión concreta (meeting_id),
    # el chat responde sobre la reunión en curso (snapshot en RAM). Si no, flujo DB intacto.
    if "live" in data:
        use_live = bool(data.get("live"))
    else:
        use_live = MEETING.is_active() and meeting_id is None
    if use_live:
        result = _assistant.answer_live(message, history=history,
                                        max_tokens=max_tokens, reasoning=reasoning)
    else:
        result = _assistant.answer(_db, message, history=history, meeting_id=meeting_id,
                                   max_tokens=max_tokens, reasoning=reasoning)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "error")}), 503
    return jsonify(result)


@app.route("/api/instagram-cookies/sync", methods=["POST"])
def instagram_cookies_sync():
    """Extrae las cookies de Instagram del navegador y las guarda cifradas (DPAPI).

    Fallback duradero: una vez guardadas, la transcripción de Instagram funciona
    aunque el navegador esté cerrado. Responde {ok, browser, count, error}.
    """
    from core.url_transcribe import sync_instagram_cookies  # noqa: PLC0415
    r = sync_instagram_cookies()
    status = 200 if r.get("ok") else 400
    return jsonify({"ok": r.get("ok", False), "browser": r.get("browser"),
                    "count": r.get("count", 0), "error": r.get("error")}), status


@app.route("/api/youtube-transcript", methods=["POST"])
def youtube_transcript():
    """Transcribe contenido de YouTube (subtítulos o audio) y lo devuelve como texto plano.

    Delega en core.url_transcribe.transcribe_url y persiste en DB si SAVE_HISTORY=true.

    Body JSON: {url: str}
    Responde: {ok, title, language, auto_generated, text}
    Errores: 400 URL inválida, 401 requiere auth, 404 sin contenido, 502 red, 500 otro.
    """
    import logging as _log
    from core.url_transcribe import transcribe_url  # noqa: PLC0415

    _logger = _log.getLogger(__name__)

    data = request.get_json()
    if not data or not data.get("url"):
        return jsonify({"error": "url field required"}), 400

    url = data["url"].strip()

    # Validación rápida: solo YouTube en este endpoint (compatibilidad con la UI existente)
    if not _YOUTUBE_RE.match(url):
        return jsonify({"error": "URL no reconocida como YouTube. Solo se admiten URLs de youtube.com o youtu.be"}), 400

    result = transcribe_url(url)

    if not result["ok"]:
        _kind_to_http = {
            "invalid_url": 400,
            "no_subtitles": 404,
            "needs_auth": 401,
            "network": 502,
            "empty": 404,
        }
        http_code = _kind_to_http.get(result.get("error_kind") or "", 500)
        return jsonify({"error": result.get("error", "Error desconocido")}), http_code

    # Persistir en DB si SAVE_HISTORY=true
    if os.getenv("SAVE_HISTORY", "true").lower() == "true":
        model_label = "youtube-subtitles" if result.get("method") == "subtitles" else "youtube-audio"
        _db.insert(
            text=result["text"],
            language=result.get("language"),
            duration_seconds=result.get("duration"),
            model=model_label,
            source=result.get("source", "youtube"),
        )

    return jsonify({
        "ok": True,
        "title": result.get("title", ""),
        "language": result.get("language", ""),
        "auto_generated": result.get("method") == "subtitles",
        "text": result["text"],
    })


def _find_free_port(start: int = 5678, attempts: int = 50) -> int:
    """Find an available port starting from `start`."""
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}-{start + attempts - 1}")


def start_web_server(port: int = None) -> int:
    """Start Flask in a daemon thread so it doesn't block the Qt event loop."""
    if port is None:
        port = _find_free_port()
    _start_url_queue_worker()
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return port
