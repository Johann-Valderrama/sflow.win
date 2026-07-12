import os
import re
import secrets
import socket
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template, render_template_string, request, send_file
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
# FUENTE ÚNICA: este bloque se inyecta en LOS DOS documentos HTML que lo usan
# (dashboard → loadMeeting, y MEETING_PAGE → loadLive). EN TRANSICIÓN (unidad
# 4.1, paso 2/3): el dashboard ya vive en web/templates/dashboard.html y recibe
# este bloque como variable de contexto Jinja `{{ mt_js|safe }}` vía
# render_template(); MEETING_PAGE sigue inline y lo recibe por el
# .replace() de módulo de siempre (placeholder __MT_INCREMENTAL_JS__) hasta que
# el paso 3 lo extraiga también a web/templates/reunion.html. Son documentos
# SEPARADOS: definirlo solo en uno deja al otro con ReferenceError.
# OJO Jinja: mientras MEETING_PAGE siga pasando por render_template_string,
# este JS no debe contener nunca '{{', '{%' ni '{#' (llaves simples son seguras).
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


MEETING_PAGE = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vflow — Reunión</title>
<script src="/static/vendor/tailwind.js"></script>
<style>
  @font-face { font-family:'Inter'; font-style:normal; font-weight:300 600; font-display:swap; src:url('/static/vendor/inter-variable.woff2') format('woff2'); }
  body { background:#0a0a0f; color:#e5e7eb; font-family:Inter,system-ui,sans-serif; }
  .glass { background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.06); }
  .mt-fade { animation:mtFade .25s ease-out; }
  @keyframes mtFade { from{opacity:0;transform:translateY(3px);} to{opacity:1;transform:none;} }
  .btn { font-size:.8rem; padding:.4rem .8rem; border-radius:.5rem; cursor:pointer; }
  #asst-messages ul{list-style:disc;margin:.25rem 0;padding-left:1.2rem;}
  #asst-messages ol{list-style:decimal;margin:.25rem 0;padding-left:1.4rem;}
  #asst-messages li{margin:.1rem 0;}
  #asst-messages code{background:rgba(255,255,255,.1);padding:0 .25rem;border-radius:3px;font-family:monospace;font-size:.92em;}
  #asst-messages strong{font-weight:600;}
  #asst-messages .md-h{font-weight:600;margin:.3rem 0 .15rem;}
  #asst-messages .md-p{margin:.15rem 0;}
  #asst-messages .md-cite{color:#c4b5fd;text-decoration:underline;cursor:pointer;font-size:.92em;}
  svg.ic{width:1em;height:1em;display:inline-block;vertical-align:-0.125em;flex-shrink:0}
  @media (prefers-reduced-motion: reduce){*,*::before,*::after{animation-duration:.001ms!important;transition-duration:.001ms!important;}}
</style></head>
<body class="min-h-screen p-6">
<div class="max-w-5xl mx-auto">
  <main>
  <div class="flex items-center justify-between mb-5">
    <h1 class="text-2xl font-semibold" data-icon="mic">Reunión</h1>
    <div class="flex items-center gap-2">
      <select id="mt-template-select" onchange="setMeetingTemplate(this.value)" title="Plantilla de la reunión: moldea el acta y las sugerencias del chat en vivo"
        class="text-xs px-2 py-1.5 rounded-lg bg-white/[0.05] border border-white/10 text-white/60 hover:text-white/80 focus:outline-none focus:ring-2 focus:ring-violet-500/60">
        __MEETING_TEMPLATE_OPTIONS_HTML__
      </select>
      <button onclick="startMeeting()" id="mt-start" data-icon="play" class="btn bg-purple-600/30 text-purple-200 hover:bg-purple-600/50">Iniciar</button>
      <button onclick="stopMeeting()" id="mt-stop" data-icon="stop" class="btn bg-red-600/30 text-red-300 hover:bg-red-600/50 hidden">Terminar</button>
      <button onclick="openFolder()" data-icon="folder" class="btn text-white/55 hover:text-white/80 hover:bg-white/5" title="Abrir la carpeta de actas (.md)">Carpeta</button>
      <a href="/" class="btn text-white/45 hover:text-white/70 hover:bg-white/5">Dashboard</a>
    </div>
  </div>
  <div id="mt-status" class="text-xs text-white/55 mb-3" aria-live="polite"></div>

  <!-- Pestañas: En vivo | Preguntar (el chat existente vive dentro de "Preguntar") -->
  <div id="mt-tabs" class="flex items-center gap-1 mb-3 hidden">
    <button id="mt-tab-btn-live" onclick="mtSetTab('live')" class="text-xs px-3 py-1.5 rounded-t-lg border-b-2 transition-colors">En vivo</button>
    <button id="mt-tab-btn-ask" onclick="mtSetTab('ask')" class="text-xs px-3 py-1.5 rounded-t-lg border-b-2 transition-colors">Preguntar</button>
  </div>

  <!-- Panel: En vivo -->
  <div id="mt-tab-live">
    <!-- Header de reunión activa: timer + VU por canal + pausa -->
    <div id="mt-live-header" class="glass rounded-xl p-4 mb-3 hidden">
      <div class="flex items-center justify-between flex-wrap gap-3">
        <div class="flex items-center gap-3">
          <div id="mt-timer" class="text-3xl font-mono font-semibold tabular-nums text-white/90">00:00</div>
          <div id="mt-paused-badge" class="text-xs px-2 py-1 rounded-md bg-amber-500/20 text-amber-300 hidden">⏸ EN PAUSA</div>
        </div>
        <div class="flex items-center gap-4">
          <div class="flex items-center gap-1.5" title="Nivel de tu micrófono">
            <span class="text-[11px] text-purple-300/80 w-8">Yo</span>
            <div class="w-24 h-1.5 rounded-full bg-white/10 overflow-hidden"><div id="mt-vu-yo" class="h-full rounded-full" style="width:0%;background:var(--accent);transition:width .12s linear;"></div></div>
          </div>
          <div class="flex items-center gap-1.5" title="Nivel de audio del sistema (Ellos)">
            <span class="text-[11px] text-sky-300/80 w-10">Ellos</span>
            <div class="w-24 h-1.5 rounded-full bg-white/10 overflow-hidden"><div id="mt-vu-ellos" class="h-full rounded-full" style="width:0%;background:var(--cyan);transition:width .12s linear;"></div></div>
          </div>
        </div>
      </div>
    </div>

    <div class="grid gap-3 mb-3 grid-cols-1 md:grid-cols-[1.4fr_1fr]">
      <div>
        <div class="flex items-center justify-between mb-1.5">
          <span class="text-xs text-white/55">Transcripción en vivo</span>
          <button onclick="copyEl('mt-transcript', this)" class="text-[10px] text-white/30 hover:text-white/60 px-1.5 py-0.5 rounded hover:bg-white/5 transition-colors" title="Copiar transcript">Copiar</button>
        </div>
        <div id="mt-transcript" class="space-y-1.5 max-h-[28rem] overflow-y-auto glass rounded-xl p-3">
          <div class="text-xs text-white/45">Inicia una reunión para ver la transcripción (Yo / Ellos).</div>
        </div>
      </div>
      <div>
        <div class="text-xs text-white/55 mb-1.5">Pendientes</div>
        <div id="pending-cards" class="space-y-2 max-h-[28rem] overflow-y-auto glass rounded-xl p-3">
        </div>
        <div class="mt-3">
          <div class="text-xs text-white/55 mb-1.5">Nota rápida</div>
          <div class="flex gap-2">
            <input id="mt-note-input" type="text" placeholder="Escribe una nota…" aria-label="Nota rápida"
              class="focus:outline-none focus:ring-2 focus:ring-violet-500/60"
              style="flex:1;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:.5rem;color:#e5e7eb;padding:.35rem .7rem;font-size:.8rem;"
              onkeydown="if(event.key==='Enter')mtAddNote()">
            <button onclick="mtAddNote()" class="btn bg-white/[0.06] text-white/60 hover:bg-white/[0.1]">Añadir</button>
          </div>
          <div id="mt-notes-list" class="space-y-1 mt-2"></div>
        </div>
      </div>
    </div>

    <!-- Barra de 4 acciones -->
    <div id="mt-action-bar" class="flex items-center gap-2 mb-3 hidden">
      <button onclick="mtHighlight()" class="btn bg-white/[0.06] text-amber-300/90 hover:bg-amber-500/15" title="Marcar momento destacado">⭐ Highlight</button>
      <button onclick="mtFocusNote()" class="btn bg-white/[0.06] text-white/70 hover:bg-white/[0.1]" title="Añadir una nota rápida">📝 Nota</button>
      <button id="mt-pause-btn" onclick="mtTogglePause()" class="btn bg-white/[0.06] text-white/70 hover:bg-white/[0.1]" title="Pausar o reanudar la captura">⏸ Pausar</button>
      <button onclick="stopMeeting()" class="btn bg-red-600/30 text-red-300 hover:bg-red-600/50" title="Terminar la reunión">⏹ Terminar</button>
    </div>

    <div id="mt-acta" class="glass rounded-xl p-4 mb-8 hidden">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm font-medium text-emerald-300/80">Acta de la reunión</span>
        <button onclick="copyEl('mt-acta-body', this)" class="text-[10px] text-white/30 hover:text-white/60 px-1.5 py-0.5 rounded hover:bg-white/5 transition-colors" title="Copiar acta">Copiar</button>
      </div>
      <div id="mt-acta-body" class="space-y-2 text-sm text-white/80"></div>
    </div>

    <div class="flex items-center justify-between mb-2 mt-8">
      <div class="text-sm font-medium text-white/60">Historial de reuniones</div>
      <div class="flex gap-2">
        <button onclick="exportAll()" class="btn text-white/45 hover:text-white/70 hover:bg-white/5" title="Exportar todas a Markdown">Exportar todas (.md)</button>
        <button onclick="clearAll()" class="btn text-white/55 hover:text-red-300 hover:bg-white/5" title="Eliminar todas las reuniones">Limpiar todo</button>
      </div>
    </div>
    <input id="meetingSearch" type="search" placeholder="Buscar en reuniones…" aria-label="Buscar en reuniones"
      class="focus:outline-none focus:ring-2 focus:ring-violet-500/60"
      style="width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:.5rem;color:#e5e7eb;padding:.4rem .75rem;font-size:.8rem;margin-bottom:.5rem;"
      oninput="onSearchInput(this.value)">
    <div id="mt-history" class="space-y-1"></div>
    <div id="mt-viewer" class="glass rounded-xl p-4 mt-3 hidden"></div>
  </div>

  <!-- Panel: Preguntar (Asistente de reuniones — chat de memoria, sin cambios internos) -->
  <div id="mt-tab-ask" class="hidden">
    <div class="glass rounded-xl p-4 mt-6">
      <div class="flex items-center justify-between mb-2">
        <div class="text-sm font-medium text-violet-300/80" data-icon="chat">Asistente de reuniones &mdash; pregúntale a tus reuniones</div>
        <div class="flex items-center gap-1">
          <button id="asst-scope-global" class="text-[11px] px-2 py-0.5 rounded-full border transition-colors">Global</button>
          <button id="asst-scope-meeting" disabled class="text-[11px] px-2 py-0.5 rounded-full border transition-colors" title="Selecciona una reunión del historial para preguntar solo sobre ella">Esta reunión</button>
        </div>
      </div>
      <div id="asst-live-note" class="hidden text-[11px] text-emerald-300/70 mb-2">&#9679; Respondiendo sobre la reuni&oacute;n en curso</div>
      <div id="asst-messages" class="space-y-2 overflow-y-auto mb-2" aria-live="polite" style="min-height:60px;max-height:280px;"></div>
      <div id="asst-chips" class="flex flex-wrap gap-1.5 mb-2"></div>
      <div class="flex gap-2">
        <input id="asst-input" type="text" placeholder="Pregunta sobre tus reuniones…" aria-label="Pregunta sobre tus reuniones"
          class="focus:outline-none focus:ring-2 focus:ring-violet-500/60"
          style="flex:1;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:.5rem;color:#e5e7eb;padding:.35rem .7rem;font-size:.8rem;"
          onkeydown="if(event.key==='Enter')asstSend()">
        <button id="asst-send" onclick="asstSend()" class="btn bg-violet-600/30 text-violet-200 hover:bg-violet-600/50">Enviar</button>
      </div>
      <div class="flex items-center gap-1.5 mt-1.5">
        <input type="checkbox" id="asst-reason" style="accent-color:#7c3aed;cursor:pointer;">
        <label for="asst-reason" data-icon="spark" class="text-[11px] text-white/35 cursor-pointer select-none"
          title="Activa razonamiento para preguntas analíticas (más lento/caro)">Pensar más</label>
      </div>
    </div>
  </div>
  </main>
</div>
<script>
let _seen = new Set(), _segCount = 0, _actaShown = false, _poll = null;
function mtoast(msg,type){const c=document.body;const t=document.createElement('div');t.style.cssText='position:fixed;top:16px;right:16px;z-index:60;background:rgba(20,20,24,.96);border:1px solid '+(type==='err'?'rgba(239,68,68,.55)':'rgba(255,255,255,.12)')+';color:#eaeaea;padding:10px 14px;border-radius:10px;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.45)';t.textContent=msg;c.appendChild(t);setTimeout(()=>t.remove(),4000);}
let _meetingIds = new Set();
// --- Iconos SVG inline (sin dependencia de fuentes; idénticos en Win/Mac/Linux, sin tofu) ---
function _ic(p){return '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+p+'</svg>';}
const ICONS = {
  mic: _ic('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>'),
  play: _ic('<polygon points="6 4 20 12 6 20 6 4" fill="currentColor" stroke="none"/>'),
  stop: _ic('<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>'),
  folder: _ic('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),
  chat: _ic('<path d="M21 11.5a8.4 8.4 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5z"/>'),
  spark: _ic('<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" fill="currentColor"/>'),
  bulb: _ic('<path d="M9 18h6M10 22h4M15 14c.2-1 .7-1.7 1.4-2.5A4.6 4.6 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.8.8 1.2 1.5 1.4 2.5"/>'),
  calendar: _ic('<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>'),
  trash: _ic('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
  arrow: _ic('<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>'),
};
function _fillIcons(root){(root||document).querySelectorAll('[data-icon]').forEach(el=>{const i=ICONS[el.dataset.icon];if(i&&!el.querySelector('svg.ic')){const hasText=el.textContent.trim().length>0;el.insertAdjacentHTML('afterbegin', i+(hasText?' ':''));}});}
function esc(s){ const d=document.createElement('div'); d.textContent = (s==null?'':String(s)); return d.innerHTML; }
function copyEl(id, btn){ const el=document.getElementById(id); if(!el)return; navigator.clipboard.writeText(el.innerText.trim()).then(()=>{ const p=btn.textContent; btn.textContent='✓'; setTimeout(()=>{ btn.textContent=p; },1500); }); }
function mdInline(x){
  x=esc(x);
  x=x.replace(/`([^`]+)`/g,'<code>$1</code>');
  x=x.replace(/\\*\\*([^*]+?)\\*\\*/g,'<strong>$1</strong>');
  x=x.replace(/__([^_]+?)__/g,'<strong>$1</strong>');
  x=x.replace(/(^|[^*\\w])\\*([^*\\n]+?)\\*(?=[^*\\w]|$)/g,'$1<em>$2</em>');
  x=x.replace(/\\[(\\d+)\\]/g,function(_,n){ return _meetingIds.has(parseInt(n,10))?'<a href="#" class="md-cite" data-mid="'+n+'">['+n+']</a>':'['+n+']'; });
  return x;
}
function mdToHtml(t){
  if(!t)return '';
  const lines=t.split(/\\r?\\n/);
  let html='',list=null;
  const close=function(){ if(list){ html+='</'+list+'>'; list=null; } };
  for(const raw of lines){
    let m;
    if(m=raw.match(/^\\s*[-*\\u2022]\\s+(.*)$/)){
      if(list!=='ul'){ close(); html+='<ul>'; list='ul'; }
      html+='<li>'+mdInline(m[1])+'</li>';
    } else if(m=raw.match(/^\\s*\\d+[.)]\\s+(.*)$/)){
      if(list!=='ol'){ close(); html+='<ol>'; list='ol'; }
      html+='<li>'+mdInline(m[1])+'</li>';
    } else if(m=raw.match(/^\\s*#{1,6}\\s+(.*)$/)){
      close(); html+='<div class="md-h">'+mdInline(m[1])+'</div>';
    } else if(raw.trim()===''){
      close();
    } else {
      close(); html+='<div class="md-p">'+mdInline(raw)+'</div>';
    }
  }
  close();
  return html;
}
function pendMeta(p){ const a=[]; if(p&&p.responsable)a.push(esc(p.responsable));
  const fh=[p&&p.fecha,p&&p.hora].filter(Boolean).map(esc).join(' '); if(fh)a.push(ICONS.calendar+' '+fh);
  return a.length?' <span class="text-white/35">('+a.join(' \\u00b7 ')+')</span>':''; }

// --- Tarjetas genéricas de push ✓/✗ (unidad 2.2 -> generalizado en 5.3) ---
// cardKey: hash simple del texto NORMALIZADO (minúsculas, sin acentos, sin puntuación,
// espacios colapsados). NUNCA se usa el id del pendiente: la consolidación LLM puede
// renumerarlo, lo que crearía tarjetas fantasma (verificado en debate de diseño).
// Se mantiene el nombre pendKey como alias por compatibilidad con quien lo referencie.
function cardKey(texto){
  const norm=(texto||'').toString().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'')
    .toLowerCase().replace(/[^a-z0-9\\s]/g,' ').replace(/\\s+/g,' ').trim();
  let h=0; for(let i=0;i<norm.length;i++){ h=((h<<5)-h+norm.charCodeAt(i))|0; }
  return 'p'+h;
}
function pendKey(texto){ return cardKey(texto); }

// Estilo por tipo de tarjeta: clases Tailwind (borde/fondo) + etiqueta. Los tipos
// futuros (deteccion/coaching/cruzada — unidades 5.1/5.2) solo necesitan una
// entrada aquí; renderCards() ya sabe pintarlos.
const CARD_STYLES = {
  pendiente: {cls:'bg-amber-500/[0.06] border-amber-400/20', label:null},
  coaching:  {cls:'bg-cyan-500/[0.06] border-cyan-400/20',   label:'Coaching'},
  deteccion: {cls:'bg-violet-500/[0.06] border-violet-400/20', label:'Detección'},
  cruzada:   {cls:'bg-violet-500/[0.06] border-violet-400/20', label:'Memoria cruzada'},
};

const _pendCards = new Map(); // key -> {texto, tipo, responsable, firstSeen, resolved, timer}
const _PEND_TTL_MS = 180000;   // ~3 min de caducidad
const _PEND_MAX_VISIBLE = 3;

function _pendRemove(key){
  const c=_pendCards.get(key); if(!c) return;
  if(c.timer) clearTimeout(c.timer);
  const el=document.getElementById('pc-'+key);
  if(el){ el.style.transition='opacity .3s ease, transform .3s ease'; el.style.opacity='0'; el.style.transform='translateX(8px)';
    setTimeout(()=>{ if(el.parentNode) el.parentNode.removeChild(el); }, 300); }
}

function _pendScheduleExpiry(key){
  const c=_pendCards.get(key); if(!c) return;
  if(c.timer) clearTimeout(c.timer);
  const remaining=Math.max(0, _PEND_TTL_MS - (Date.now()-c.firstSeen));
  c.timer=setTimeout(()=>{ c.expired=true; _pendRemove(key); }, remaining);
}

function _pendRenderCard(key){
  const c=_pendCards.get(key); if(!c) return;
  const container=document.getElementById('pending-cards'); if(!container) return;
  const style=CARD_STYLES[c.tipo]||CARD_STYLES.pendiente;
  const div=document.createElement('div');
  div.id='pc-'+key;
  div.className='rounded-lg '+style.cls+' p-2.5 mt-fade';
  const labelHtml = style.label ? '<div class="text-[10px] uppercase tracking-wide text-white/40 mb-1">'+esc(style.label)+'</div>' : '';
  div.innerHTML =
    labelHtml+
    '<div class="text-xs text-white/80 leading-snug mb-1.5">'+esc(c.texto)+pendMeta(c)+'</div>'+
    '<div class="flex items-center gap-1.5" id="pc-actions-'+key+'">'+
      '<button class="text-[11px] px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-300/90 hover:bg-emerald-500/25" onclick="mtCardFeedback(\\''+key+'\\',\\''+c.tipo+'\\',1)" title="Confirmar">\\u2713</button>'+
      '<button class="text-[11px] px-2 py-0.5 rounded bg-white/[0.06] text-white/45 hover:bg-white/[0.1]" onclick="mtCardFeedback(\\''+key+'\\',\\''+c.tipo+'\\',-1)" title="Descartar">\\u2717</button>'+
    '</div>';
  container.appendChild(div);
}

function _pendEnforceMax(){
  // Máximo _PEND_MAX_VISIBLE tarjetas visibles: si llega una 4ª, cae la más vieja.
  const visible=[..._pendCards.entries()].filter(([,c])=>!c.resolved && !c.expired)
    .sort((a,b)=>a[1].firstSeen-b[1].firstSeen);
  while(visible.length>_PEND_MAX_VISIBLE){
    const [oldestKey]=visible.shift();
    _pendCards.get(oldestKey).expired=true;
    _pendRemove(oldestKey);
  }
}

// renderCards: renderer GENÉRICO (unidad 5.3, corrección O2). card={key,tipo,texto,detail?}.
// Gating por modo client-side: en modo 'silent' (status().proactive_mode) solo se
// muestran tarjetas tipo 'pendiente' (contrato inamovible desde 2.2); el resto de
// tipos se filtra aquí mismo, antes de tocar el DOM.
function renderCards(cards, proactiveMode){
  if(!Array.isArray(cards)) return;
  for(const p of cards){
    const texto=(p&&p.texto||'').trim(); if(!texto) continue;
    const tipo=(p&&p.tipo)||'pendiente';
    if(proactiveMode==='silent' && tipo!=='pendiente') continue;
    const key=(p&&p.key) || cardKey(texto);
    if(_pendCards.has(key)) continue; // ya vista (nueva, resuelta o caducada): nunca reaparece
    const card={texto, tipo, responsable:p.responsable, fecha:p.fecha, hora:p.hora, detail:p.detail,
      firstSeen: Date.now(), resolved:false, expired:false, timer:null};
    _pendCards.set(key, card);
    _pendRenderCard(key);
    _pendScheduleExpiry(key);
    _pendEnforceMax();
  }
}
// Alias de compatibilidad: los pendientes del insight stream siguen entrando por
// aquí (tipo 'pendiente' hardcodeado, forma histórica de esta función).
function renderPendingCards(pendientes){
  if(!Array.isArray(pendientes)) return;
  renderCards(pendientes.map(p=>({...p, tipo:'pendiente'})), null);
}

async function mtCardFeedback(key, tipo, value){
  const c=_pendCards.get(key); if(!c || c.resolved) return;
  c.resolved=true;
  const actions=document.getElementById('pc-actions-'+key);
  if(actions){ actions.innerHTML = value===1
    ? '<span class="text-[11px] text-emerald-300">\\u2713 Gracias</span>'
    : '<span class="text-[11px] text-white/35">\\u2717 Descartado</span>'; }
  try{
    await fetch('/api/meeting/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:key, tipo:(tipo||c.tipo||'pendiente'), texto:c.texto, value:value})});
  }catch(e){ /* best-effort: la tarjeta igual se retira */ }
  setTimeout(()=>{ _pendRemove(key); }, 2000);
}
// Alias de compatibilidad con el nombre anterior (unidad 2.2).
async function mtPendFeedback(key, value){ return mtCardFeedback(key, 'pendiente', value); }

function _pendResetAll(){
  const container=document.getElementById('pending-cards');
  if(container) container.innerHTML='';
  for(const c of _pendCards.values()){ if(c.timer) clearTimeout(c.timer); }
  _pendCards.clear();
}

__MT_INCREMENTAL_JS__

let _mtNotes = [], _mtPaused = false;
async function loadLive(){
  try{
    const d = await fetchMeetingIncremental('live'); const s = d.status||{};
    const startB=document.getElementById('mt-start'), stopB=document.getElementById('mt-stop');
    const liveHeader=document.getElementById('mt-live-header'), actionBar=document.getElementById('mt-action-bar');
    const tabs=document.getElementById('mt-tabs');
    // Plantilla activa (unidad 4.3): la verdad es el server (status().template), nunca
    // localStorage — así el dropdown refleja lo mismo que ve el hotkey AltGr+R.
    if(s.template && s.template!==_mtTemplate){ _mtTemplate=s.template; asstSyncLiveUi(); }
    const tplSel=document.getElementById('mt-template-select');
    if(tplSel && document.activeElement!==tplSel && tplSel.value!==_mtTemplate) tplSel.value=_mtTemplate;
    if(s.active){ startB.classList.add('hidden'); stopB.classList.remove('hidden');
      liveHeader.classList.remove('hidden'); actionBar.classList.remove('hidden'); tabs.classList.remove('hidden');
      let t=(s.paused?'En pausa \\u00b7 ':'Grabando ')+(s.elapsed_fmt||'00:00')+' \\u00b7 '+(s.segment_count||0)+' intervenciones';
      if(s.sys_available===false)t+=' \\u00b7 solo micr\\u00f3fono';
      if(s.insight_running)t+=' \\u00b7 analizando\\u2026'; if(s.error)t+=' \\u00b7 \\u26a0 '+s.error;
      document.getElementById('mt-status').textContent=t; _actaShown=false;
      document.getElementById('mt-timer').textContent=s.elapsed_fmt||'00:00';
      _mtPaused=!!s.paused;
      document.getElementById('mt-paused-badge').classList.toggle('hidden', !_mtPaused);
      const pauseBtn=document.getElementById('mt-pause-btn');
      if(pauseBtn) pauseBtn.textContent=_mtPaused?'\\u25b6 Reanudar':'\\u23f8 Pausar';
      const lv=(s.levels||{}), pctYo=Math.round(Math.min(lv.yo||0,1)*100), pctEllos=Math.round(Math.min(lv.ellos||0,1)*100);
      const vuYo=document.getElementById('mt-vu-yo'), vuEllos=document.getElementById('mt-vu-ellos');
      if(vuYo) vuYo.style.width=pctYo+'%'; if(vuEllos) vuEllos.style.width=pctEllos+'%';
      // Pendientes del insight stream (tipo 'pendiente' hardcodeado) + tarjetas
      // genéricas futuras si el backend ya expone status().cards (5.1/5.2 —
      // tolerante a su ausencia: hoy status() no trae "cards", no rompe nada).
      const pendCards=((d.insights||{}).pendientes||[]).map(p=>({...p, tipo:'pendiente'}));
      const extraCards=Array.isArray(s.cards) ? s.cards : [];
      renderCards(pendCards.concat(extraCards), s.proactive_mode);
    } else { startB.classList.remove('hidden'); stopB.classList.add('hidden'); document.getElementById('mt-status').textContent='';
      liveHeader.classList.add('hidden'); actionBar.classList.add('hidden'); tabs.classList.add('hidden');
      document.getElementById('mt-paused-badge').classList.add('hidden'); _mtPaused=false;
      if(_pendCards.size){ _pendResetAll(); }
      if(d.last_minutes && !_actaShown){ renderActa(d.last_minutes); _actaShown=true; loadHistory(); } }
    asstSetLive(!!s.active);  // chat "Preguntar": modo vivo sigue al estado de la reunión (unidad 2.3)
    renderTranscript(s, d.segments||[]);
  }catch(e){}
}

// --- Pestañas En vivo / Preguntar ---
function mtSetTab(tab){
  const live=document.getElementById('mt-tab-live'), ask=document.getElementById('mt-tab-ask');
  const btnLive=document.getElementById('mt-tab-btn-live'), btnAsk=document.getElementById('mt-tab-btn-ask');
  const activeCls='text-white/85 border-violet-400', inactiveCls='text-white/40 border-transparent hover:text-white/70';
  if(tab==='ask'){ live.classList.add('hidden'); ask.classList.remove('hidden');
    btnAsk.className='text-xs px-3 py-1.5 rounded-t-lg border-b-2 transition-colors '+activeCls;
    btnLive.className='text-xs px-3 py-1.5 rounded-t-lg border-b-2 transition-colors '+inactiveCls;
  } else { live.classList.remove('hidden'); ask.classList.add('hidden');
    btnLive.className='text-xs px-3 py-1.5 rounded-t-lg border-b-2 transition-colors '+activeCls;
    btnAsk.className='text-xs px-3 py-1.5 rounded-t-lg border-b-2 transition-colors '+inactiveCls;
  }
}
mtSetTab('live');

// --- Acciones en vivo: highlight, nota rápida, pausa ---
async function mtHighlight(){
  try{ const r=await fetch('/api/meeting/highlight',{method:'POST'}); const d=await r.json();
    if(d.ok && d.item){ mtoast('\\u2b50 Momento destacado ('+d.item.time+')','ok'); } else { mtoast('No hay reunión activa.','err'); }
  }catch(e){ mtoast('Error de red al marcar highlight.','err'); }
}
function mtFocusNote(){ const inp=document.getElementById('mt-note-input'); if(inp){ inp.focus(); } }
async function mtAddNote(){
  const inp=document.getElementById('mt-note-input'); const text=(inp.value||'').trim(); if(!text) return;
  try{ const r=await fetch('/api/meeting/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text})});
    const d=await r.json();
    if(d.ok && d.item){ inp.value=''; _mtNotes.push(d.item); renderMtNotes(); mtoast('Nota añadida.','ok'); }
    else{ mtoast('No hay reunión activa.','err'); }
  }catch(e){ mtoast('Error de red al añadir la nota.','err'); }
}
function renderMtNotes(){
  const el=document.getElementById('mt-notes-list'); if(!el) return;
  if(!_mtNotes.length){ el.innerHTML=''; return; }
  el.innerHTML=_mtNotes.map(n=>'<div class="text-xs text-white/60"><span class="text-white/30 font-mono mr-1">'+esc(n.time)+'</span>'+esc(n.text)+'</div>').join('');
}
async function mtTogglePause(){
  const url=_mtPaused?'/api/meeting/resume':'/api/meeting/pause';
  const btn=document.getElementById('mt-pause-btn'); if(btn) btn.disabled=true;
  try{ const r=await fetch(url,{method:'POST'}); const d=await r.json();
    if(d.ok!==false){ _mtPaused=!!d.paused; await loadLive(); }
  }catch(e){ mtoast('Error de red al pausar/reanudar.','err'); }
  finally{ if(btn) btn.disabled=false; }
}
function renderTranscript(s, segs){
  const c=document.getElementById('mt-transcript');
  if(!segs.length){ if(_segCount!==0){_segCount=0;c.innerHTML='';} if(!c.innerHTML)c.innerHTML='<div class="text-xs text-white/45">Esperando voz\\u2026</div>'; return; }
  if(segs.length<_segCount){ c.innerHTML=''; _segCount=0; }
  if(_segCount===0)c.innerHTML='';
  for(let i=_segCount;i<segs.length;i++){ const sg=segs[i]; const col=sg.speaker==='Yo'?'text-purple-300':'text-sky-300';
    const div=document.createElement('div'); div.className='text-sm text-white/80 leading-snug mt-fade';
    div.innerHTML='<span class="text-[10px] font-mono text-white/30 mr-1">'+esc(sg.time)+'</span><span class="text-xs font-medium '+col+' mr-1">'+esc(sg.speaker)+':</span>'+esc(sg.text);
    c.appendChild(div); }
  _segCount=segs.length; c.scrollTop=c.scrollHeight;
}
// Push→pull: el "Análisis en vivo" (temas/pendientes/propuestas) ya NO se muestra en
// vivo — va al acta. El único push permitido es la tarjeta de pendientes (unidad 2.2,
// contenedor #pending-cards). fcl/_seen se conservan para el fade-in de esas tarjetas.
function fcl(id){ if(id==null)return''; if(_seen.has(id))return''; _seen.add(id); return ' mt-fade'; }
// ---------------------------------------------------------------------------
// Métricas de conversación (unidad 3.2) — dona SVG inline + helpers de formato.
// Tolerante a metrics=null y a campos individuales null (reuniones viejas o sin voz).
// ---------------------------------------------------------------------------
function fmtMinSec(s){
  if(s==null||isNaN(s))return '\\u2014';
  s=Math.max(0,Math.round(s)); const m=Math.floor(s/60), r=s%60;
  return m+':'+(r<10?'0':'')+r;
}
function donutSvg(pctYo, pctEllos, size, stroke){
  size=size||28; stroke=stroke||(size>=80?10:5);
  const r=(size-stroke)/2, c=size/2, circ=2*Math.PI*r;
  const py=Math.max(0,Math.min(100,pctYo||0));
  const dashYo=circ*(py/100), dashEllos=circ-dashYo;
  // Arco "Yo" (violeta) empieza arriba (rotado -90deg); arco "Ellos" (cian) completa el resto.
  return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 '+size+' '+size+'" class="ic" style="width:'+size+'px;height:'+size+'px;">'
    +'<circle cx="'+c+'" cy="'+c+'" r="'+r+'" fill="none" stroke="var(--cyan)" stroke-width="'+stroke+'" opacity="0.85"/>'
    +'<circle cx="'+c+'" cy="'+c+'" r="'+r+'" fill="none" stroke="var(--accent)" stroke-width="'+stroke+'" '
    +'stroke-dasharray="'+dashYo+' '+dashEllos+'" stroke-dashoffset="0" transform="rotate(-90 '+c+' '+c+')" stroke-linecap="butt"/>'
    +'</svg>';
}
function metricsStrip(metrics){
  // Franja compacta para la tarjeta del historial. Devuelve '' si no hay metrics
  // (la tarjeta queda exactamente como antes).
  if(!metrics)return '';
  const py=metrics.pct_yo, pe=metrics.pct_ellos;
  const pyR=(py==null)?'\\u2014':Math.round(py), peR=(pe==null)?'\\u2014':Math.round(pe);
  const ttl=metrics.talk_to_listen;
  return '<div class="flex items-center gap-2 mt-1">'
    +donutSvg(py||0, pe||0, 22, 4)
    +'<span class="text-[11px] text-white/40">Yo '+pyR+'% \\u00b7 Ellos '+peR+'%</span>'
    +(ttl!=null?'<span class="text-[11px] text-white/25">\\u00b7 T:L '+ttl.toFixed(2)+'</span>':'')
    +'</div>';
}
function metricsPanel(metrics){
  // Sección "Estadísticas" del visor de detalle. Tolerante a metrics=null y a
  // campos individuales null.
  if(!metrics){
    return '<div class="text-xs text-white/40">Sin datos de conversación para esta reunión.</div>';
  }
  const py=metrics.pct_yo, pe=metrics.pct_ellos;
  const pyR=(py==null)?'\\u2014':Math.round(py), peR=(pe==null)?'\\u2014':Math.round(pe);
  const ttl=metrics.talk_to_listen;
  // El flag global marca que ALGÚN monólogo llegó a 90s; el badge va en el canal
  // cuyo valor lo cruza (pueden ser ambos), no en un canal fijo.
  const monoBadge=' <span class="text-[10px] px-1.5 py-0.5 rounded-md" style="background:rgba(245,158,11,0.18);color:#fcd34d;">largo</span>';
  const monoYoFlag=(metrics.longest_monologue_yo_s||0)>=90;
  const monoEllosFlag=(metrics.longest_monologue_ellos_s||0)>=90;
  const turnsStr=(metrics.turns_approx==null)?'\\u2014':(metrics.turns_approx+(metrics.approx?' (aprox.)':''));
  const wpmYo=metrics.wpm_yo, wpmEllos=metrics.wpm_ellos;
  function metric(label, val, note){
    return '<div class="glass rounded-lg px-3 py-2">'
      +'<div class="text-[10px] text-white/35 mb-0.5">'+esc(label)+'</div>'
      +'<div class="text-sm text-white/80">'+val+'</div>'
      +(note?'<div class="text-[10px] text-white/25 mt-0.5">'+note+'</div>':'')
      +'</div>';
  }
  let h='<div class="flex items-center gap-4 mb-3">'
    +donutSvg(py||0, pe||0, 84, 12)
    +'<div class="text-xs space-y-1">'
    +'<div class="text-purple-300/80">\\u25cf Yo &mdash; '+pyR+'% ('+fmtMinSec(metrics.talk_yo_s)+')</div>'
    +'<div class="text-sky-300/80">\\u25cf Ellos &mdash; '+peR+'% ('+fmtMinSec(metrics.talk_ellos_s)+')</div>'
    +'</div></div>';
  h+='<div class="grid grid-cols-2 md:grid-cols-3 gap-2">';
  h+=metric('Talk-to-listen', (ttl==null?'\\u2014':ttl.toFixed(2)), 'benchmark venta ~43/57');
  h+=metric('Mon\\u00f3logo m\\u00e1s largo \\u2014 Yo', fmtMinSec(metrics.longest_monologue_yo_s)+(monoYoFlag?monoBadge:''));
  h+=metric('Mon\\u00f3logo m\\u00e1s largo \\u2014 Ellos', fmtMinSec(metrics.longest_monologue_ellos_s)+(monoEllosFlag?monoBadge:''));
  h+=metric('Turnos', turnsStr);
  h+=metric('Preguntas \\u2014 Yo / Ellos', (metrics.questions_yo==null?'\\u2014':metrics.questions_yo)+' / '+(metrics.questions_ellos==null?'\\u2014':metrics.questions_ellos));
  h+=metric('WPM \\u2014 Yo / Ellos', (wpmYo==null?'\\u2014':Math.round(wpmYo))+' / '+(wpmEllos==null?'\\u2014':Math.round(wpmEllos)), 'sano: 140-160');
  h+='</div>';
  return h;
}
function fmtMmss(t){ const s=Math.round(t); return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); }
function traceChip(t){
  if(t==null||isNaN(t))return '';
  return ' <button type="button" class="mt-trace-chip text-[10px] text-violet-300/50 hover:text-violet-200 underline decoration-dotted px-1" data-t="'+t+'" title="Ir a este momento en la transcripci\\u00f3n">'+fmtMmss(t)+'</button>';
}
// Delegado único: los chips de decisiones/pendientes se generan como HTML string
// (actaHtml) sin listeners individuales; un solo listener en document cubre tanto
// el acta en vivo (#mt-acta-body) como el visor de reuniones (#viewer-acta-body).
document.addEventListener('click', function(ev){
  const chip = ev.target.closest && ev.target.closest('.mt-trace-chip');
  if(!chip) return;
  const t = parseFloat(chip.dataset.t || 'NaN');
  if(!isNaN(t)) jumpToMoment(t);
});
function bantHtml(b){
  if(!b||typeof b!=='object')return '';
  const fields=[['budget','Presupuesto'],['authority','Autoridad'],['need','Necesidad'],['timeline','Plazo']];
  const cells=fields.filter(f=>b[f[0]]).map(f=>'<div><div class="text-[10px] text-white/35 uppercase tracking-wide">'+f[1]+'</div><div class="text-xs text-white/75">'+esc(b[f[0]])+'</div></div>');
  if(!cells.length)return '';
  return '<div><div class="text-xs text-fuchsia-300/50 mt-2 mb-1">BANT</div><div class="grid grid-cols-2 gap-2">'+cells.join('')+'</div></div>';
}
function actaHtml(m){
  m=m||{}; const dec=m.decisiones||[],tem=m.temas||[],pen=m.pendientes||[],pro=m.propuestas||[],cit=m.citas||[],mom=m.momentos_destacados||[],nus=m.notas_usuario||[]; let h='';
  if(m.resumen)h+='<p class="text-white/80">'+esc(m.resumen)+'</p>';
  h+=bantHtml(m.bant);
  if(nus.length)h+='<div><div class="text-xs text-violet-300/50 mt-2 mb-1">📝 Notas del usuario</div>'+nus.map(x=>'<div class="text-xs text-white/85 mb-0.5"><span class="text-white/50">'+esc(x.time||'')+'</span> '+esc(x.nota||'')+(x.contexto?'<div class="text-[11px] text-white/40 ml-6">IA: '+esc(x.contexto)+'</div>':'')+'</div>').join('')+'</div>';
  if(mom.length)h+='<div><div class="text-xs text-yellow-300/50 mt-2 mb-1">\\u2b50 Momentos destacados</div>'+mom.map(x=>'<div class="text-xs text-white/75">'+ICONS.spark+' <span class="text-white/50">'+esc(x.time||'')+'</span> '+esc(x.texto||'')+'</div>').join('')+'</div>';
  if(dec.length)h+='<div><div class="text-xs text-white/40 mt-2 mb-1">Decisiones</div>'+dec.map(d=>'<div class="text-xs text-white/75">\\u2022 '+esc(typeof d==='string'?d:(d.texto||''))+(typeof d==='string'?'':traceChip(d.t))+'</div>').join('')+'</div>';
  if(pen.length)h+='<div><div class="text-xs text-amber-300/50 mt-2 mb-1">Pendientes</div>'+pen.map(p=>'<div class="text-xs text-white/75">'+ICONS.arrow+' '+esc(p.texto||p)+pendMeta(p)+(typeof p==='object'?traceChip(p.t):'')+'</div>').join('')+'</div>';
  if(pro.length)h+='<div><div class="text-xs text-sky-300/50 mt-2 mb-1">Propuestas</div>'+pro.map(p=>'<div class="text-xs text-white/75">'+ICONS.bulb+' '+esc(p.texto!=null?p.texto:p)+'</div>').join('')+'</div>';
  if(cit.length)h+='<div><div class="text-xs text-emerald-300/50 mt-2 mb-1">Próximas reuniones</div>'+cit.map(c=>'<div class="text-xs text-white/75">'+ICONS.calendar+' '+esc(c.texto||c)+pendMeta({fecha:c.fecha,hora:c.hora})+'</div>').join('')+'</div>';
  if(tem.length)h+='<div><div class="text-xs text-white/40 mt-2 mb-1">Temas tratados</div>'+tem.map(t=>'<div class="text-xs text-white/75">\\u2022 '+esc(t)+'</div>').join('')+'</div>';
  return h||'<div class="text-xs text-white/30">Acta vacía.</div>';
}
function renderActa(m){ document.getElementById('mt-acta-body').innerHTML=actaHtml(m); document.getElementById('mt-acta').classList.remove('hidden'); }

async function setMeetingTemplate(name){
  const prev=_mtTemplate; _mtTemplate=name;  // optimista; se corrige en el próximo loadLive() si falla
  try{
    const r=await fetch('/api/meeting/template',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({template:name})});
    const d=await r.json();
    if(!d.ok){ _mtTemplate=prev; const sel=document.getElementById('mt-template-select'); if(sel)sel.value=prev; mtoast('Plantilla inválida.','err'); return; }
    if(_asstLive) asstSyncLiveUi();  // refresca los chips en vivo si el chat está en modo vivo
  }catch(e){ _mtTemplate=prev; const sel=document.getElementById('mt-template-select'); if(sel)sel.value=prev; mtoast('Error de red al cambiar la plantilla.','err'); }
}
async function startMeeting(){ _seen=new Set(); _segCount=0; _actaShown=false; _mtNotes=[]; renderMtNotes();
  document.getElementById('mt-acta').classList.add('hidden'); document.getElementById('mt-transcript').innerHTML='';
  const btn=document.getElementById('mt-start'); btn.disabled=true;
  try{ const r=await fetch('/api/meeting/start',{method:'POST'}); const d=await r.json();
    if(!d.ok){ document.getElementById('mt-status').textContent=d.error||'No se pudo iniciar.'; mtoast(d.error||'No se pudo iniciar.','err'); return; }
    await loadLive(); startPoll(); }catch(e){ mtoast('Error de red al iniciar.','err'); }finally{ btn.disabled=false; } }
async function stopMeeting(){ document.getElementById('mt-status').textContent='Terminando y generando el acta\\u2026';
  const btn=document.getElementById('mt-stop'); btn.disabled=true;
  try{ const r=await fetch('/api/meeting/stop',{method:'POST'}); const d=await r.json(); await loadLive();
    renderActa(d.minutes); _actaShown=true; loadHistory(); }catch(e){ mtoast('Error de red al terminar.','err'); }finally{ btn.disabled=false; } }
function startPoll(){ if(_poll)return; _poll=setInterval(loadLive,1500); }

async function loadHistory(){
  try{ const r=await fetch('/api/meetings'); const d=await r.json(); const el=document.getElementById('mt-history');
    const ms=d.meetings||[]; _meetingIds=new Set(ms.map(m=>m.id)); if(!ms.length){ el.innerHTML='<div class="text-xs text-white/45">Aún no hay reuniones guardadas.</div>'; return; }
    el.innerHTML=ms.map(m=>{ const dur=Math.round((m.duration_seconds||0)/60);
      return '<div class="glass rounded-lg px-3 py-2 hover:bg-white/[0.04]">'
        +'<div class="flex items-center gap-2">'
        +'<span class="text-xs text-white/70 flex-1 cursor-pointer" onclick="openMeeting('+m.id+')">'+esc(m.started_at||m.created_at||'')+'</span>'
        +'<span class="text-[11px] text-white/30">'+dur+' min</span>'
        +'<button onclick="delMeeting('+m.id+')" class="text-white/25 hover:text-red-300 text-sm px-1" title="Eliminar esta reunión">'+ICONS.trash+'</button>'
        +'</div>'
        +metricsStrip(m.metrics)
        +'</div>'; }).join('');
  }catch(e){} }
function jumpToMoment(t, segs){
  // Busca el segmento cuyo data-t sea el más cercano a t y hace scroll + highlight
  const body=document.getElementById('mt-transcript-body');
  if(!body) return;
  const divs=body.querySelectorAll('[data-t]');
  if(!divs.length) return;
  let best=null, bestDiff=Infinity;
  divs.forEach(function(d){ const dt=parseFloat(d.dataset.t||'0'); const diff=Math.abs(dt-t); if(diff<bestDiff){bestDiff=diff;best=d;} });
  if(!best) return;
  best.scrollIntoView({behavior:'smooth',block:'center'});
  best.classList.add('mt-seg-flash');
  setTimeout(function(){ best.classList.remove('mt-seg-flash'); },1200);
}
function _renderTimeline(chapters, meetingId, v){
  const tl=document.getElementById('mt-timeline');
  if(!tl) return;
  if(Array.isArray(chapters) && chapters.length){
    let h='<div class="flex flex-wrap gap-1.5">';
    chapters.forEach(function(ch,i){
      h+='<button data-t="'+ch.t+'" data-idx="'+i+'" class="mt-chapter-btn text-[11px] px-2 py-1 rounded-md bg-white/[0.06] hover:bg-violet-500/20 text-white/60 hover:text-violet-300 transition-colors" title="'+esc(ch.resumen||ch.titulo)+'">'
        +'<span class="text-white/30">'+esc(ch.inicio)+'</span> <span class="text-white/70">'+esc(ch.titulo)+'</span>'
        +'</button>';
    });
    h+='</div>';
    tl.innerHTML=h;
    tl.querySelectorAll('.mt-chapter-btn').forEach(function(btn){
      btn.addEventListener('click',function(){ jumpToMoment(parseFloat(btn.dataset.t||'0')); });
    });
  } else {
    tl.innerHTML='<button id="mt-gen-chapters-btn" class="btn text-[11px] bg-white/[0.05] text-white/40 hover:text-violet-300 hover:bg-violet-500/15">Generar l\\u00ednea de tiempo</button>';
    const gbtn=document.getElementById('mt-gen-chapters-btn');
    if(gbtn) gbtn.addEventListener('click',async function(){
      gbtn.textContent='Generando\\u2026'; gbtn.disabled=true;
      try{
        const r=await fetch('/api/meetings/'+meetingId+'/chapters',{method:'POST'});
        const d=await r.json();
        _renderTimeline(d.chapters||[], meetingId, v);
      }catch(e){ gbtn.textContent='Error al generar'; gbtn.disabled=false; }
    });
  }
}
async function openMeeting(id){
  const v=document.getElementById('mt-viewer'); v.classList.remove('hidden'); v.innerHTML='<div class="text-xs text-white/30">Cargando\\u2026</div>';
  try{ const r=await fetch('/api/meetings/'+id); const m=await r.json();
    const dateStr=esc(m.started_at||'');
    // Transcript: por segmentos si existen, si no, <pre> plano (compat reuniones viejas)
    let transcriptHtml;
    const segs=Array.isArray(m.segments)&&m.segments.length?m.segments:null;
    if(segs){
      transcriptHtml='<div id="mt-transcript-body" class="text-xs max-h-80 overflow-y-auto space-y-0.5">';
      segs.forEach(function(s,i){
        const isYo=(s.speaker||'').toLowerCase().indexOf('yo')!==-1||(s.speaker||'')==='Yo';
        const clr=isYo?'text-violet-300/80':'text-sky-300/80';
        transcriptHtml+='<div id="mtseg-'+i+'" data-t="'+parseFloat(s.t||0)+'" class="px-1 rounded hover:bg-white/[0.04]">'
          +'<span class="text-white/25 select-none">['+esc(s.time||'')+'</span>'
          +' <span class="'+clr+' font-medium select-none">'+esc(s.speaker||'')+'</span>'
          +'<span class="text-white/25 select-none">]</span>'
          +' <span class="text-white/65">'+esc(s.text||'')+'</span>'
          +'</div>';
      });
      transcriptHtml+='</div>';
    } else {
      transcriptHtml='<pre class="text-xs text-white/60 whitespace-pre-wrap max-h-80 overflow-y-auto">'+esc(m.transcript||'')+'</pre>';
    }
    v.innerHTML='<style>.mt-seg-flash{background:rgba(139,92,246,0.18)!important;transition:background 0.1s;}</style>'
      +'<div class="flex items-center justify-between mb-2"><div class="text-sm font-medium text-white/60">Reuni\\u00f3n '+dateStr+'</div>'
      +'<div class="flex gap-2">'
      +'<button id="mt-close-btn" class="btn text-white/30 hover:text-white/60">Cerrar</button>'
      +'</div></div>'
      +'<div class="text-xs font-medium text-violet-300/50 mb-1">L\\u00ednea de tiempo</div><div id="mt-timeline" class="mb-3"></div>'
      +'<div class="flex items-center justify-between mb-1"><span class="text-xs font-medium text-white/40">Estad\\u00edsticas</span></div><div id="viewer-metrics-body" class="mb-3">'+metricsPanel(m.metrics)+'</div>'
      +'<div class="flex items-center justify-between mb-1"><span class="text-xs font-medium text-emerald-300/70">Acta</span><button onclick="copyEl(&apos;viewer-acta-body&apos;, this)" class="text-[10px] text-white/30 hover:text-white/60 px-1.5 py-0.5 rounded hover:bg-white/5">Copiar</button></div><div id="viewer-acta-body" class="space-y-2 mb-3">'+actaHtml(m.minutes)+'</div>'
      +'<div class="flex items-center justify-between mb-1"><span class="text-xs font-medium text-white/40">Transcripci\\u00f3n</span><button onclick="copyEl(&apos;viewer-transcript-body&apos;, this)" class="text-[10px] text-white/30 hover:text-white/60 px-1.5 py-0.5 rounded hover:bg-white/5">Copiar</button></div><div id="viewer-transcript-body">'+transcriptHtml+'</div>';
    const _cb=document.getElementById('mt-close-btn');
    if(_cb) _cb.addEventListener('click',function(){
      document.getElementById('mt-viewer').classList.add('hidden');
      currentViewerMeetingId=null;
      const smb=document.getElementById('asst-scope-meeting');
      smb.disabled=true; smb.textContent='Esta reuni\\u00f3n';
      asstSetScope('global');
    });
    // Actualiza estado de ámbito al abrir reunión
    currentViewerMeetingId=id;
    currentViewerMeetingDate=m.started_at||'';
    const smb=document.getElementById('asst-scope-meeting');
    smb.disabled=false;
    smb.textContent='Esta reuni\\u00f3n ('+(currentViewerMeetingDate.slice(0,10)||'#'+id)+')';
    asstSetScope('meeting');
    // Renderizar timeline (capítulos)
    _renderTimeline(m.chapters||[], id, v);
  }catch(e){ v.innerHTML='<div class="text-xs text-red-300">No se pudo cargar.</div>'; } }
async function openFolder(){ try{ const r=await fetch('/api/meetings/open-folder',{method:'POST'}); const d=await r.json();
  if(!d.ok) mtoast('No se pudo abrir la carpeta: '+(d.error||d.path||''),'err'); }catch(e){ mtoast('No se pudo abrir la carpeta.','err'); } }
async function exportAll(){ try{ const r=await fetch('/api/meetings/export',{method:'POST'}); const d=await r.json();
  mtoast(d.ok?('Exportadas '+d.exported+' reuniones a: '+d.path):'Error al exportar', d.ok?'ok':'err'); }catch(e){ mtoast('Error al exportar.','err'); } }
async function delMeeting(id){ if(!confirm('¿Eliminar esta reunión? No se puede deshacer.'))return;
  try{ await fetch('/api/meetings/'+id+'/delete',{method:'POST'});
    document.getElementById('mt-viewer').classList.add('hidden'); loadHistory(); }catch(e){} }
async function clearAll(){ if(!confirm('¿Eliminar TODAS las reuniones del historial? No se puede deshacer.'))return;
  try{ const r=await fetch('/api/meetings/clear',{method:'POST'}); const d=await r.json();
    document.getElementById('mt-viewer').classList.add('hidden'); loadHistory(); mtoast('Eliminadas '+(d.deleted||0)+' reuniones.','ok'); }catch(e){ mtoast('Error al limpiar el historial.','err'); } }

// Búsqueda FTS
let _searchTimer=null;
function onSearchInput(val){
  clearTimeout(_searchTimer);
  _searchTimer=setTimeout(()=>{ const q=val.trim(); if(!q){loadHistory();}else{runSearch(q);} },250);
}
function hl(s){
  const d=document.createElement('div'); d.textContent=s||''; let h=d.innerHTML;
  return h.split('\x02').join('<b>').split('\x03').join('</b>');
}
async function runSearch(q){
  const el=document.getElementById('mt-history');
  try{
    const r=await fetch('/api/meetings/search?q='+encodeURIComponent(q));
    const d=await r.json(); const res=d.results||[];
    if(!res.length){ el.innerHTML='<div class="text-xs text-white/45">Sin resultados para \\u201c'+esc(q)+'\\u201d.</div>'; return; }
    el.innerHTML=res.map(m=>{
      const dateStr=document.createElement('div'); dateStr.textContent=m.started_at||'';
      return '<div class="glass rounded-lg px-3 py-2 hover:bg-white/[0.04] cursor-pointer" onclick="openMeeting('+m.id+')">'
        +'<div class="flex items-center gap-2">'
        +'<span class="text-xs text-white/70 flex-1">'+esc(m.title||m.started_at||'')+'</span>'
        +'<span class="text-[11px] text-white/30">'+esc(m.started_at||'')+'</span>'
        +'</div>'
        +(m.snippet?'<div class="text-[11px] text-white/40 mt-0.5 truncate">'+hl(m.snippet)+'</div>':'')
        +'</div>';
    }).join('');
  }catch(e){ el.innerHTML='<div class="text-xs text-red-300">Error al buscar.</div>'; }
}

// ---------------------------------------------------------------------------
// Asistente de reuniones — chat de memoria sobre reuniones
// ---------------------------------------------------------------------------
let asstMeetingId = null;
let asstHistory = [];
let currentViewerMeetingId = null;
let currentViewerMeetingDate = '';

const ASST_CHIPS_GLOBAL = [
  '\\u00bfCu\\u00e1les son mis pendientes?',
  'Resume mis \\u00faltimas reuniones',
  '\\u00bfQu\\u00e9 decisiones tomamos?',
  'Redacta un email de seguimiento',
  'Genera un informe de pendientes'
];
const ASST_CHIPS_MEETING = [
  'Resume esta reuni\\u00f3n',
  'Lista los pendientes',
  '\\u00bfQu\\u00e9 se decidi\\u00f3?',
  'Redacta email de seguimiento'
];
// Chips del chat EN VIVO por plantilla de reunión (unidad 4.3): generados desde
// core.meeting_templates.TEMPLATES (Python) para no duplicar los textos a mano —
// ver __MEETING_TEMPLATES_CHIPS_JSON__ sustituido en meeting_page() antes de servir.
const MEETING_TEMPLATE_CHIPS = __MEETING_TEMPLATES_CHIPS_JSON__;
function currentLiveChips(){
  return MEETING_TEMPLATE_CHIPS[_mtTemplate] || MEETING_TEMPLATE_CHIPS['general'];
}
let _asstLive = false;
let _mtTemplate = 'general';  // plantilla activa (unidad 4.3) — la verdad vive en el server, se refleja aquí desde loadLive()
// Modo vivo de la pestaña Preguntar: chips fijos + nota "reunión en curso".
// Lo alimenta loadLive() con el estado del polling de /api/meeting.
function asstSetLive(active){
  active = !!active;
  if(active === _asstLive) return;
  _asstLive = active;
  if(active){
    asstSetScope('global');  // la reunión viva manda: se pregunta sin meeting_id
  } else {
    asstSetScope(asstMeetingId && currentViewerMeetingId ? 'meeting' : 'global');
  }
}
// Refleja el modo vivo en la UI (nota + chips). La llama asstSetScope al final,
// así el vivo sobrevive a los toggles de ámbito y se apaga si se elige una reunión guardada.
function asstSyncLiveUi(){
  const liveNow = _asstLive && !asstMeetingId;
  const note = document.getElementById('asst-live-note');
  if(note) note.classList.toggle('hidden', !liveNow);
  if(liveNow) asstRenderChips(currentLiveChips());
}

function asstRenderChips(chips){
  const el=document.getElementById('asst-chips');
  el.innerHTML='';
  chips.forEach(function(c){
    const b=document.createElement('button');
    b.textContent=c;
    b.className='hover:bg-violet-600/25';
    b.style.cssText='font-size:.73rem;padding:.25rem .6rem;border-radius:.4rem;cursor:pointer;background:rgba(139,92,246,0.12);border:1px solid rgba(139,92,246,0.22);color:rgba(196,181,253,0.85);';
    b.addEventListener('click',function(){ asstSend(c); });
    el.appendChild(b);
  });
}

function asstSetScope(mode){
  const gb=document.getElementById('asst-scope-global');
  const mb=document.getElementById('asst-scope-meeting');
  if(mode==='meeting' && currentViewerMeetingId){
    asstMeetingId=currentViewerMeetingId;
    // Activo: Esta reunión
    gb.className='text-[11px] px-2 py-0.5 rounded-full border transition-colors text-white/40 border-white/10 hover:text-white/70';
    mb.className='text-[11px] px-2 py-0.5 rounded-full border transition-colors bg-violet-600/30 text-violet-200 border-violet-500/40';
    asstRenderChips(ASST_CHIPS_MEETING);
  } else {
    asstMeetingId=null;
    // Activo: Global
    gb.className='text-[11px] px-2 py-0.5 rounded-full border transition-colors bg-violet-600/30 text-violet-200 border-violet-500/40';
    mb.className='text-[11px] px-2 py-0.5 rounded-full border transition-colors text-white/40 border-white/10'+(mb.disabled?' opacity-40 cursor-not-allowed':' hover:text-white/70');
    asstRenderChips(ASST_CHIPS_GLOBAL);
  }
  asstSyncLiveUi();  // el modo vivo (unidad 2.3) pisa chips/nota si aplica
}

function asstAddMsg(role, html){
  const el=document.getElementById('asst-messages');
  const isUser=(role==='user');
  const div=document.createElement('div');
  div.className='text-xs leading-relaxed '+(isUser?'text-white/70':'text-violet-100/90');
  div.style.cssText='padding:.35rem .6rem;border-radius:.4rem;'+(isUser?'background:rgba(255,255,255,0.04);text-align:right;':'background:rgba(139,92,246,0.10);');
  div.innerHTML=(isUser?'<span class="text-white/30 mr-1">T\\u00fa:</span>':'<span class="text-violet-300/60 mr-1">Asistente:</span>')+html;
  el.appendChild(div);
  el.scrollTop=el.scrollHeight;
  return div;
}

async function asstSend(text){
  const inp=document.getElementById('asst-input');
  const msg=(text!==undefined?text:inp.value).trim();
  if(!msg)return;
  inp.value='';
  asstAddMsg('user', esc(msg).replace(/\\n/g,'<br>'));
  asstHistory.push({role:'user',content:msg});
  const reasonChk=document.getElementById('asst-reason');
  const reasoningVal=reasonChk&&reasonChk.checked?true:'auto';
  const placeholder=asstAddMsg('assistant','<span class="text-white/45 italic">El asistente est\\u00e1 pensando\\u2026</span>');
  const sendBtn=document.getElementById('asst-send'); sendBtn.disabled=true;
  try{
    const res=await fetch('/api/meetings/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg,history:asstHistory.slice(-6),meeting_id:asstMeetingId,reasoning:reasoningVal,
        live:(_asstLive&&!asstMeetingId)?true:undefined})
    });
    const d=await res.json();
    if(!res.ok||d.error){
      placeholder.innerHTML='<span class="text-white/30 mr-1">Asistente:</span><span style="color:#f87171;">'
        +esc(d.error||'Error al consultar el asistente.')+'</span>';
    } else {
      const ans=d.answer||'';
      const badge=d.reasoned?'<span title="Respuesta con razonamiento extendido" style="font-size:.9em;opacity:.7;margin-right:.25rem;">'+ICONS.spark+'</span>':'';
      placeholder.innerHTML='<span class="text-violet-300/60 mr-1">Asistente:</span>'
        +badge+mdToHtml(ans);
      asstHistory.push({role:'assistant',content:ans});
    }
  }catch(e){
    placeholder.innerHTML='<span class="text-white/30 mr-1">Asistente:</span>'
      +'<span style="color:#f87171;">Error de red.</span>';
  }finally{
    document.getElementById('asst-send').disabled=false;
  }
}

// Inicializar estado de ámbito: Global activo, "Esta reunión" deshabilitado
(function(){
  document.getElementById('asst-scope-global').addEventListener('click',function(){ asstSetScope('global'); });
  document.getElementById('asst-scope-meeting').addEventListener('click',function(){
    if(currentViewerMeetingId) asstSetScope('meeting');
  });
  asstSetScope('global');
})();

// Listener delegado para citas clickables [N]
(function(){
  const box=document.getElementById('asst-messages');
  if(box) box.addEventListener('click',function(e){
    const a=e.target.closest('.md-cite');
    if(!a) return;
    e.preventDefault();
    const mid=parseInt(a.dataset.mid,10);
    if(!isNaN(mid) && _meetingIds.has(mid)){
      openMeeting(mid);
      const v=document.getElementById('mt-viewer');
      if(v) v.scrollIntoView({behavior:'smooth',block:'start'});
    }
  });
})();

_fillIcons();  // iconos SVG estáticos (data-icon)
loadLive(); startPoll(); loadHistory();
</script></body></html>"""

# Inyección única (a nivel de módulo, no por request) del helper JS compartido
# del polling incremental en AMBOS documentos. Si el placeholder faltara en
# alguno, el helper quedaría sin definir en ese documento y loadMeeting/loadLive
# morirían con ReferenceError — hay un test de integración que lo vigila
# (tests/test_meeting_incremental.py::TestHelperPresentInBothDocuments).
MEETING_PAGE = MEETING_PAGE.replace("__MT_INCREMENTAL_JS__", _MT_INCREMENTAL_JS)


@app.route("/")
def index():
    """Sirve la página principal del dashboard de transcripciones."""
    return render_template("dashboard.html", mt_js=_MT_INCREMENTAL_JS)


def _meeting_page_html() -> str:
    """Sustituye los placeholders de plantillas (unidad 4.3) por el HTML/JSON generado
    desde ``core.meeting_templates.TEMPLATES`` — una sola fuente de verdad para los
    textos de las 4 plantillas, sin duplicarlos a mano en el JS."""
    import json as _json  # noqa: PLC0415 — import local (mismo patrón que el resto del módulo)
    options_html = "\n        ".join(
        f'<option value="{name}">{tpl["label"]}</option>'
        for name, tpl in _meeting_templates.TEMPLATES.items()
    )
    chips_json = _json.dumps(_meeting_templates.chips_map(), ensure_ascii=False)
    html = MEETING_PAGE.replace("__MEETING_TEMPLATE_OPTIONS_HTML__", options_html)
    html = html.replace("__MEETING_TEMPLATES_CHIPS_JSON__", chips_json)
    return html


@app.route("/reunion")
def reunion():
    """Ventana dedicada al modo reunión: en vivo (transcript + análisis + acta) + historial."""
    return render_template_string(_meeting_page_html())


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
