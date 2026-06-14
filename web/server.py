import os
import re
import secrets
import socket
import threading
from datetime import datetime, timedelta
from urllib.parse import urlparse
from flask import Flask, jsonify, render_template_string, request, send_file
from dotenv import set_key
from db.database import TranscriptionDB
from config import APP_DATA_DIR
from core import dictionary as _dictionary
from core.meeting import MEETING

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


def _url_queue_worker() -> None:
    """Loop infinito que procesa la cola url_queue de forma serial (FIFO).

    - Toma el item 'pending' más antiguo.
    - Lo marca 'processing'.
    - Llama a transcribe_url con on_progress → actualiza stage en DB.
    - Si ok: inserta en transcriptions (respetando SAVE_HISTORY) y marca 'done'.
    - Si no ok: marca 'error'.
    - Pausa ~1.5s entre items (cortesía anti-baneo).
    - Items 'processing' huérfanos al arranque (crash anterior) se reencolan como 'pending'.
    """
    import time
    import logging as _log
    from core.url_transcribe import transcribe_url  # noqa: PLC0415

    _logger = _log.getLogger(__name__)

    # DB con su propia conexión (thread distinto)
    from db.database import TranscriptionDB  # noqa: PLC0415
    from config import DB_PATH  # noqa: PLC0415
    worker_db = TranscriptionDB(DB_PATH)

    # Reparar items 'processing' huérfanos de un crash anterior
    try:
        import sqlite3 as _sqlite3
        with _sqlite3.connect(DB_PATH) as _c:
            _c.execute("UPDATE url_queue SET status='pending', stage=NULL WHERE status='processing'")
    except Exception as _exc:
        _logger.warning("No se pudo reparar items processing huérfanos: %s", _exc)

    while True:
        try:
            item = worker_db.url_queue_next_pending()
            if item is None:
                time.sleep(1.0)
                continue

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
                    worker_db.insert(
                        text=result["text"],
                        language=result.get("language"),
                        duration_seconds=result.get("duration"),
                        model=model_label,
                        source=result.get("source") or "url",
                    )
                worker_db.url_queue_set_done(item_id, title)
                _logger.info("URL queue: id=%d completado — %s", item_id, title)
            else:
                error_msg = result.get("error") or "Error desconocido"
                worker_db.url_queue_set_error(item_id, error_msg)
                _logger.warning("URL queue: id=%d error — %s", item_id, error_msg)

        except Exception as exc:
            _logger.error("URL queue worker: excepción inesperada: %s", exc, exc_info=True)
            # Intentar marcar el item como error para no bloquear la cola
            try:
                if "item_id" in dir():
                    worker_db.url_queue_set_error(item_id, f"Error interno: {exc}")  # type: ignore[name-defined]
            except Exception:
                pass

        time.sleep(1.5)


def _start_url_queue_worker() -> None:
    """Arranca el worker de la cola de URLs una sola vez (guard contra doble arranque)."""
    global _url_worker_started
    with _url_worker_lock:
        if _url_worker_started:
            return
        _url_worker_started = True
    t = threading.Thread(target=_url_queue_worker, daemon=True, name="url-queue-worker")
    t.start()


app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["SECRET_KEY"] = secrets.token_hex(32)

# Single DB instance (avoids re-running DDL on every request)
_db = TranscriptionDB()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Vflow - Transcripciones</title>
    <!-- NOTE: Tailwind loaded from CDN. Accepted risk: app is local-only, dashboard on localhost. -->
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap');
        body { font-family: 'Inter', system-ui, sans-serif; background: #0a0a0a; color: #e5e5e5; }
        .glass { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06); }
        /* Entrada suave de nuevos segmentos/insights del modo reunión (anti-parpadeo) */
        .mt-fade { animation: mtFade 0.25s ease-out; }
        @keyframes mtFade { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }
        .row-hover:hover { background: rgba(255,255,255,0.05); }
        .text-preview { max-height: 2.6em; overflow: hidden; transition: max-height 0.3s ease; }
        .text-preview.expanded { max-height: 500px; }
        .copied { animation: flash 0.5s ease; }
        @keyframes flash { 0%,100% { background: transparent; } 50% { background: rgba(80,220,120,0.1); } }
        .deleted { animation: fadeout 0.4s ease forwards; }
        @keyframes fadeout { to { opacity: 0; transform: translateX(20px); } }
        .brand-logo { width: 28px; height: 28px; border-radius: 6px; }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 3px; }
        .dropdown { position: relative; display: inline-block; }
        .dropdown-menu { display: none; position: absolute; right: 0; top: 100%; margin-top: 4px;
            background: #1a1a1a; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;
            min-width: 200px; z-index: 50; overflow: hidden; }
        .dropdown.open .dropdown-menu { display: block; }
        .dropdown-item { padding: 8px 14px; font-size: 13px; color: rgba(255,255,255,0.6);
            cursor: pointer; transition: background 0.15s; }
        .dropdown-item:hover { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.9); }
        .dropdown-item.danger { color: #ef4444; }
        .dropdown-item.danger:hover { background: rgba(239,68,68,0.15); }
        .edit-area { width: 100%; background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.15);
            border-radius: 6px; color: #e5e5e5; padding: 6px 8px; font-size: 13px; font-family: inherit;
            resize: vertical; min-height: 60px; }
        .edit-area:focus { outline: none; border-color: rgba(140,80,220,0.5); }
        .cfg-select { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
            border-radius: 6px; color: #e5e5e5; padding: 6px 8px; font-size: 13px; width: 100%; cursor: pointer; }
        .cfg-select:focus { outline: none; border-color: rgba(140,80,220,0.5); }
        .cfg-select option { background: #1a1a1a; }
        .toggle-switch { position: relative; display: inline-block; width: 36px; height: 20px; flex-shrink: 0; }
        .toggle-switch input { opacity: 0; width: 0; height: 0; }
        .toggle-slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(255,255,255,0.1); border-radius: 20px; transition: 0.2s; }
        .toggle-slider:before { position: absolute; content: ''; height: 14px; width: 14px;
            left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: 0.2s; }
        .toggle-switch input:checked + .toggle-slider { background: rgba(140,80,220,0.6); }
        .toggle-switch input:checked + .toggle-slider:before { transform: translateX(16px); }
        .selection-bar { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
            background: #1a1a1a; border: 1px solid rgba(140,80,220,0.4); border-radius: 12px;
            padding: 10px 20px; display: none; align-items: center; gap: 14px; z-index: 100;
            box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
        .selection-bar.visible { display: flex; }
        .selection-bar .count { color: rgba(255,255,255,0.7); font-size: 13px; }
        .selection-bar button { font-size: 13px; padding: 5px 14px; border-radius: 6px; cursor: pointer; border: none; }
        .selection-bar .del-btn { background: rgba(239,68,68,0.2); color: #f87171; }
        .selection-bar .del-btn:hover { background: rgba(239,68,68,0.35); }
        .selection-bar .cancel-btn { background: rgba(255,255,255,0.08); color: rgba(255,255,255,0.5); }
        .selection-bar .cancel-btn:hover { background: rgba(255,255,255,0.15); color: rgba(255,255,255,0.8); }
        tr.selected-row { background: rgba(140,80,220,0.08); }
        tr.row-hover { user-select: text; }
        tr.row-hover td:not(.text-cell) { user-select: none; -webkit-user-select: none; }
        .dict-entry-dimmed { opacity: 0.4; }
        .dict-pin-btn { background: none; border: none; cursor: pointer; padding: 2px 4px; border-radius: 4px; font-size: 14px; line-height: 1; color: rgba(255,255,255,0.25); transition: color 0.15s; }
        .dict-pin-btn:hover { color: rgba(255,200,0,0.8); }
        .dict-pin-btn.pinned { color: rgba(255,200,0,0.9); }
        .dict-budget-bar { height: 4px; border-radius: 2px; background: rgba(255,255,255,0.08); margin-top: 4px; }
        .dict-budget-bar-fill { height: 100%; border-radius: 2px; background: rgba(140,80,220,0.6); transition: width 0.3s; }
        .dict-add-from-history { position: fixed; background: #1a1a1a; border: 1px solid rgba(140,80,220,0.5); border-radius: 8px; padding: 6px 12px; font-size: 12px; color: #c4b5fd; cursor: pointer; z-index: 200; box-shadow: 0 4px 16px rgba(0,0,0,0.4); display: none; }
        .dict-add-from-history:hover { background: rgba(140,80,220,0.2); }
    </style>
</head>
<body class="min-h-screen p-6">
    <div class="max-w-4xl mx-auto">
        <!-- Header -->
        <div class="flex items-center justify-between mb-8">
            <div class="flex items-center gap-3">
                <img src="/logo" class="brand-logo" alt="Vflow">
                <div class="text-2xl font-semibold text-white">Vflow</div>
                <span class="text-xs text-white/30 bg-white/5 px-2 py-1 rounded-full" id="count-badge">-</span>
            </div>
            <div class="flex items-center gap-3">
                <input type="text" id="search" placeholder="Buscar..."
                    class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                    placeholder-white/30 focus:outline-none focus:border-white/20 w-48">
                <div class="dropdown" id="cleanup-dropdown">
                    <button onclick="document.getElementById('cleanup-dropdown').classList.toggle('open')"
                        class="text-white/40 hover:text-white/70 text-sm px-2 py-1 rounded hover:bg-white/5">
                        Limpiar &#9662;
                    </button>
                    <div class="dropdown-menu">
                        <div class="dropdown-item" onclick="bulkDelete('day','hoy')">Eliminar de hoy</div>
                        <div class="dropdown-item" onclick="bulkDelete('week')">Eliminar ultima semana</div>
                        <div class="dropdown-item" onclick="bulkDelete('month')">Eliminar ultimo mes</div>
                        <div class="dropdown-item" onclick="deleteByDate()">Elegir fecha especifica...</div>
                        <div class="dropdown-item danger" onclick="bulkDelete('all')">Eliminar todo</div>
                    </div>
                </div>
                <button onclick="loadData()" class="text-white/40 hover:text-white/70 text-sm">Actualizar</button>
                <button onclick="toggleSettings()" class="text-white/40 hover:text-white/70 text-sm px-2 py-1 rounded hover:bg-white/5" title="Configuración">&#9881;</button>
                <button onclick="toggleDictionary()" class="text-white/40 hover:text-white/70 text-sm px-2 py-1 rounded hover:bg-white/5" title="Diccionario">&#128218;</button>
                <button onclick="toggleShortcuts()" class="text-white/40 hover:text-white/70 text-sm px-2 py-1 rounded hover:bg-white/5" title="Atajos de teclado">&#9000;</button>
                <button onclick="toggleUrlQueue()" class="text-white/40 hover:text-white/70 text-sm px-2 py-1 rounded hover:bg-white/5" title="Transcribir desde URL">&#9654;</button>
                <button onclick="toggleMeeting()" class="text-white/40 hover:text-white/70 text-sm px-2 py-1 rounded hover:bg-white/5" title="Reunión en vivo (mic + sistema)">&#127908;</button>
            </div>
        </div>

        <!-- Shortcuts panel -->
        <div id="shortcuts-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-4">Atajos de teclado</div>
            <div class="grid gap-3" style="grid-template-columns: repeat(auto-fit, minmax(280px, 1fr))">
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">Ctrl</kbd>
                        <span class="text-white/30 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">Alt</kbd>
                        <span class="text-white/30 text-xs ml-1">— mantener</span>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Transcribir (mantenido)</p>
                    <p class="text-xs text-white/35 mt-0.5">Mantén la combinación un instante para que empiece a grabar; suelta para pegar el texto transcrito.</p>
                </div>
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">Shift</kbd>
                        <span class="text-white/30 text-xs">×3 rápido</span>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Transcribir — manos libres</p>
                    <p class="text-xs text-white/35 mt-0.5">Pulsa Shift tres veces en ~400&nbsp;ms para iniciar. Pulsa Shift una vez más para parar y pegar.</p>
                </div>
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">Ctrl</kbd>
                        <span class="text-white/30 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">Shift</kbd>
                        <span class="text-white/30 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">Alt</kbd>
                        <span class="text-white/30 text-xs ml-1">— mantener</span>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Traducir (mantenido)</p>
                    <p class="text-xs text-white/25 mt-0.5">(con backend local: solo →inglés)</p>
                    <p class="text-xs text-white/35 mt-0.5">Presiona Shift antes de Alt, mantén la combinación un instante para grabar; suelta para pegar la traducción al idioma destino.</p>
                </div>
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">AltGr</kbd>
                        <span class="text-white/30 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">T</kbd>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Traducir — manos libres (toggle)</p>
                    <p class="text-xs text-white/35 mt-0.5">Primera pulsación inicia la grabación; segunda pulsación la detiene y pega la traducción.</p>
                    <p class="text-xs text-white/25 mt-0.5">(con backend local: solo →inglés)</p>
                </div>
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">AltGr</kbd>
                        <span class="text-white/30 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/20 bg-white/[0.07] text-white/80">R</kbd>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Reunión — captura dual (toggle)</p>
                    <p class="text-xs text-white/35 mt-0.5">Inicia/termina una reunión capturando tu micrófono («Yo») y el audio del sistema («Ellos») a la vez. El transcript en vivo aparece en el panel 🎙.</p>
                </div>
            </div>
            <p class="text-xs text-white/25 mt-4">El idioma de transcripción y el idioma de destino (traducción) se configuran en el panel de Configuración.</p>
        </div>

        <!-- URL Queue panel -->
        <div id="url-queue-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-1">Transcribir desde URL</div>
            <p class="text-xs text-white/30 mb-3">Pega una o varias URLs de YouTube, TikTok u otras plataformas (una por línea) para transcribirlas en cola.</p>

            <!-- URL única -->
            <div class="flex gap-2 mb-2">
                <input type="text" id="uq-url" placeholder="https://www.youtube.com/watch?v=..."
                    class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                    placeholder-white/30 focus:outline-none focus:border-white/20 flex-1">
                <button onclick="enqueueUrls()"
                    class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 whitespace-nowrap" id="uq-btn">
                    Añadir a la cola
                </button>
            </div>

            <!-- Bulk textarea -->
            <textarea id="uq-bulk" rows="3" placeholder="Una URL por línea para añadir varias a la vez…"
                class="bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white/80
                placeholder-white/30 focus:outline-none focus:border-white/20 w-full resize-y mb-2"></textarea>

            <!-- Opciones -->
            <div class="flex items-center gap-4 mb-2 flex-wrap">
                <label class="flex items-center gap-2 text-xs text-white/40 cursor-pointer select-none">
                    <input type="checkbox" id="uq-instagram" class="accent-purple-500">
                    Instagram (experimental, requiere sesión en el navegador)
                </label>
                <button onclick="syncInstagramCookies()" id="uq-ig-sync"
                    class="text-xs px-2 py-1 rounded bg-purple-600/20 text-purple-300 hover:bg-purple-600/40"
                    title="Guarda tus cookies de Instagram cifradas (DPAPI) para usarlas aunque cierres el navegador">
                    Sincronizar cookies de Instagram</button>
            </div>
            <p class="text-xs text-white/20 mb-3">La cola usa el backend de transcripción actual. Para que sea gratis, usa el backend local (Configuración). Las cookies de Instagram se guardan cifradas con DPAPI; inicia sesión en Instagram (Opera recomendado) antes de sincronizar.</p>

            <div id="uq-ig-feedback" class="text-xs mb-2 hidden"></div>
            <div id="uq-feedback" class="text-xs mb-3 hidden"></div>

            <!-- Lista de progreso -->
            <div class="flex items-center justify-between mb-2">
                <span class="text-xs text-white/40">Cola de transcripción</span>
                <div class="flex gap-2">
                    <button onclick="cancelPendingUrls()" class="text-xs px-2 py-1 rounded text-white/30 hover:text-white/60 hover:bg-white/5">Cancelar pendientes</button>
                    <button onclick="clearQueueFinished()" class="text-xs px-2 py-1 rounded text-white/30 hover:text-white/60 hover:bg-white/5">Limpiar terminadas</button>
                </div>
            </div>
            <div id="uq-list" class="space-y-1 max-h-64 overflow-y-auto">
                <div class="text-xs text-white/20">Sin items en la cola.</div>
            </div>
        </div>

        <!-- Meeting panel (reunión en vivo) -->
        <div id="meeting-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-1">Reunión en vivo</div>
            <p class="text-xs text-white/30 mb-3">Captura tu micrófono («Yo») y el audio del sistema («Ellos») a la vez y transcribe en vivo. Inicia/termina también con <kbd class="px-1.5 py-0.5 text-[10px] font-mono rounded border border-white/20 bg-white/[0.07]">AltGr</kbd>+<kbd class="px-1.5 py-0.5 text-[10px] font-mono rounded border border-white/20 bg-white/[0.07]">R</kbd> o desde la bandeja. Para mejor diarización usa auriculares.</p>

            <div class="flex items-center gap-3 mb-3">
                <button onclick="startMeeting()" id="mt-start"
                    class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 whitespace-nowrap">
                    &#9654; Iniciar reunión
                </button>
                <button onclick="stopMeeting()" id="mt-stop"
                    class="text-xs px-3 py-1.5 rounded bg-red-600/30 text-red-300 hover:bg-red-600/50 whitespace-nowrap hidden">
                    &#9632; Terminar reunión
                </button>
                <span id="mt-status" class="text-xs text-white/40"></span>
            </div>

            <div id="mt-feedback" class="text-xs mb-2 hidden"></div>

            <div class="grid gap-3" style="grid-template-columns: 1.4fr 1fr;">
                <!-- Transcript en vivo -->
                <div>
                    <div class="text-xs text-white/40 mb-1.5">Transcript en vivo</div>
                    <div id="mt-transcript" class="space-y-1.5 max-h-96 overflow-y-auto rounded-lg bg-white/[0.02] border border-white/[0.06] p-3">
                        <div class="text-xs text-white/20">El transcript en vivo aparecerá aquí cuando inicies una reunión.</div>
                    </div>
                </div>
                <!-- Insight Stream (temas / pendientes / propuestas) -->
                <div>
                    <div class="flex items-center justify-between mb-1.5">
                        <span class="text-xs text-white/40">Análisis en vivo</span>
                        <div class="flex gap-1 text-[10px]">
                            <button id="mt-view-foco" onclick="setMeetingView('foco')" class="px-2 py-0.5 rounded text-white/40 hover:text-white/70" title="Solo lo accionable (menos distracción en reunión)">Foco</button>
                            <button id="mt-view-revision" onclick="setMeetingView('revision')" class="px-2 py-0.5 rounded bg-purple-600/40 text-purple-200" title="Panel completo: temas, pendientes y propuestas">Revisión</button>
                        </div>
                    </div>
                    <div id="mt-insights" class="space-y-3 max-h-96 overflow-y-auto rounded-lg bg-white/[0.02] border border-white/[0.06] p-3">
                        <div class="text-xs text-white/20">Temas, pendientes y propuestas aparecerán aquí a medida que avance la reunión.</div>
                    </div>
                </div>
            </div>

            <!-- Acta post-reunión (se rellena al terminar) -->
            <div id="mt-minutes" class="mt-3 rounded-lg bg-white/[0.02] border border-white/[0.06] p-3 hidden">
                <div class="text-xs font-medium text-emerald-300/80 mb-2">Acta de la reunión</div>
                <div id="mt-minutes-body" class="space-y-2 text-sm text-white/80"></div>
            </div>
        </div>

        <!-- Settings panel -->
        <div id="settings-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-4">Configuración</div>
            <div class="grid gap-4" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr))">
                <div>
                    <label class="text-xs text-white/40 block mb-1">Idioma de transcripción</label>
                    <select id="cfg-language" class="cfg-select">
                        <option value="es">Español</option>
                        <option value="en">English</option>
                        <option value="fr">Français</option>
                        <option value="de">Deutsch</option>
                        <option value="it">Italiano</option>
                        <option value="pt">Português</option>
                        <option value="ja">日本語</option>
                        <option value="zh">中文</option>
                        <option value="auto">Auto-detectar (mayor costo)</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Micrófono</label>
                    <select id="cfg-microphone" class="cfg-select">
                        <option value="">Sistema por defecto</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Idioma de salida (traducción)</label>
                    <select id="cfg-translate-target" class="cfg-select" onchange="updateLocalTranslationNote()">
                        <option value="en">English</option>
                        <option value="es">Español</option>
                        <option value="fr">Français</option>
                        <option value="de">Deutsch</option>
                        <option value="it">Italiano</option>
                        <option value="pt">Português</option>
                        <option value="ja">日本語</option>
                        <option value="zh">中文</option>
                        <option value="ko">한국어</option>
                        <option value="ru">Русский</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Sonidos</label>
                    <div class="flex items-center gap-2" style="height:32px">
                        <label class="toggle-switch">
                            <input type="checkbox" id="cfg-sounds">
                            <span class="toggle-slider"></span>
                        </label>
                        <span class="text-xs text-white/40">Beep al iniciar/terminar</span>
                    </div>
                    <div class="flex items-center gap-2 mt-2">
                        <input type="range" id="cfg-beep-volume" min="1" max="10" step="1"
                               class="accent-purple-500" style="width:90px"
                               oninput="document.getElementById('cfg-beep-volume-label').textContent=this.value">
                        <span class="text-xs text-white/40">Volumen: <span id="cfg-beep-volume-label">2</span></span>
                    </div>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Guardar historial</label>
                    <div class="flex items-center gap-2" style="height:32px">
                        <label class="toggle-switch">
                            <input type="checkbox" id="cfg-save-history">
                            <span class="toggle-slider"></span>
                        </label>
                        <span class="text-xs text-white/40">Guardar transcripciones</span>
                    </div>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Eliminar transcripciones después de (días)</label>
                    <select id="cfg-retention-days" class="cfg-select">
                        <option value="0">Nunca</option>
                        <option value="7">7 días</option>
                        <option value="30">30 días</option>
                        <option value="90">90 días</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Fuente de audio</label>
                    <select id="cfg-audio-source" class="cfg-select">
                        <option value="mic">Micrófono</option>
                        <option value="system">Audio del sistema (loopback)</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Backend de transcripción</label>
                    <select id="cfg-backend" class="cfg-select" onchange="onBackendChange()">
                        <option value="groq">Groq API (nube)</option>
                        <option value="local">Local sin internet</option>
                    </select>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Modelo local</label>
                    <select id="cfg-local-model" class="cfg-select">
                        <option value="small">small — rápido (~466 MB)</option>
                        <option value="medium">medium — más preciso (~1.5 GB)</option>
                    </select>
                </div>
            </div>
            <!-- Sección modelo local -->
            <div id="local-model-section" class="mt-4 p-3 rounded-lg bg-white/[0.02] border border-white/[0.06] hidden">
                <div class="flex items-center justify-between mb-2">
                    <span class="text-xs text-white/50" id="local-model-status-text">Verificando...</span>
                    <button onclick="downloadModel()" id="btn-download-model"
                        class="text-xs px-3 py-1 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 disabled:opacity-40 disabled:cursor-not-allowed">
                        Descargar modelo
                    </button>
                </div>
                <div id="local-download-progress-wrap" class="hidden">
                    <div class="w-full bg-white/10 rounded-full h-1.5 mt-1">
                        <div id="local-download-bar" class="bg-purple-500 h-1.5 rounded-full transition-all" style="width:0%"></div>
                    </div>
                    <span class="text-xs text-white/30 mt-1 block" id="local-download-pct">0%</span>
                </div>
                <!-- Aviso limitación de traducción (dinámico) -->
                <div id="local-translation-note" class="mt-2 rounded-lg px-3 py-2 text-xs flex items-start gap-2 hidden">
                    <span class="flex-shrink-0 mt-px">ℹ️</span>
                    <span id="local-translation-note-text"></span>
                </div>
                <!-- Groq Fallback (solo visible cuando backend=local) -->
                <div class="flex items-start gap-2 mt-3 pt-3 border-t border-white/[0.06]">
                    <label class="toggle-switch mt-0.5">
                        <input type="checkbox" id="cfg-groq-fallback" onchange="updateLocalTranslationNote()">
                        <span class="toggle-slider"></span>
                    </label>
                    <div>
                        <span class="text-xs text-white/50">Permitir Groq como respaldo si el modo local falla</span>
                        <p class="text-xs text-white/25 mt-0.5">Si se activa, el audio se enviará a Groq cuando el modo local falle.</p>
                    </div>
                </div>
            </div>

            <!-- Backend de análisis de reuniones (insights + acta) -->
            <div class="mt-4 pt-4 border-t border-white/[0.06]">
                <label class="text-xs text-white/40 block mb-1">Análisis de reuniones (insights en vivo + acta)</label>
                <select id="cfg-insights-backend" class="cfg-select" onchange="onInsightsBackendChange()">
                    <option value="groq">Groq API (nube) — instantáneo, requiere internet</option>
                    <option value="endpoint">Local (LM Studio) — privado, sin internet</option>
                </select>
                <div id="cfg-insights-endpoint-wrap" class="mt-2 p-3 rounded-lg bg-white/[0.02] border border-white/[0.06] hidden">
                    <label class="text-xs text-white/40 block mb-1">Modelo local (id en LM Studio)</label>
                    <input type="text" id="cfg-insights-model" placeholder="qwen/qwen2.5-vl-7b"
                        class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80 placeholder-white/30 focus:outline-none focus:border-white/20 w-full">
                    <p class="text-xs text-white/25 mt-1">Requiere LM Studio abierto con el servidor local activo (localhost:1234). Probados: <span class="text-white/40">qwen/qwen2.5-vl-7b</span> (calidad, ~40s) · <span class="text-white/40">llama-3.2-3b-instruct</span> (rápido, ~15s). Si LM Studio está cerrado, la reunión sigue transcribiendo pero sin análisis.</p>
                </div>
            </div>

            <div class="flex justify-end items-center mt-4 gap-3">
                <span id="cfg-saved" class="text-xs text-green-400" style="opacity:0;transition:opacity 0.3s">Guardado ✓</span>
                <button onclick="saveSettings()" class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50">Guardar</button>
            </div>
        </div>

        <!-- Dictionary panel -->
        <div id="dictionary-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="flex items-center justify-between mb-1">
                <div class="text-sm font-medium text-white/60">Diccionario personal</div>
                <div class="flex items-center gap-2">
                    <a href="/api/dictionary/export" class="text-xs px-2 py-1 rounded bg-white/5 text-white/40 hover:text-white/70 hover:bg-white/10" title="Exportar CSV">&#11015; CSV</a>
                    <label class="text-xs px-2 py-1 rounded bg-white/5 text-white/40 hover:text-white/70 hover:bg-white/10 cursor-pointer" title="Importar CSV">&#11014; CSV
                        <input type="file" accept=".csv,text/csv" class="hidden" onchange="importDictCSV(this)">
                    </label>
                </div>
            </div>
            <p class="text-xs text-white/30 mb-2">Las palabras se usan para que el modelo las reconozca; los pares corrigen la transcripción (ej. Johan → Johann).</p>
            <!-- Budget bar -->
            <div class="mb-3">
                <div class="text-xs text-white/30" id="dict-budget-label">Vocabulario en prompt: — de —</div>
                <div class="dict-budget-bar mt-1"><div class="dict-budget-bar-fill" id="dict-budget-fill" style="width:0%"></div></div>
            </div>
            <!-- Search -->
            <input type="text" id="dict-search" placeholder="Buscar en el diccionario…"
                class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                placeholder-white/30 focus:outline-none focus:border-white/20 w-full mb-3"
                oninput="filterDictList()">
            <form onsubmit="addDictEntry(event)" class="flex flex-wrap gap-2 mb-4 items-end">
                <div>
                    <label class="text-xs text-white/40 block mb-1">Palabra / forma canónica <span class="text-red-400">*</span></label>
                    <input type="text" id="dict-replace-to" placeholder="Johann"
                        class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                        placeholder-white/30 focus:outline-none focus:border-white/20 w-40" required>
                </div>
                <div>
                    <label class="text-xs text-white/40 block mb-1">Cuando escuche… (opcional)</label>
                    <input type="text" id="dict-replace-from" placeholder="Johan"
                        class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                        placeholder-white/30 focus:outline-none focus:border-white/20 w-40">
                </div>
                <button type="submit"
                    class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50">
                    Añadir
                </button>
            </form>
            <div id="dict-import-result" class="text-xs mb-2 hidden"></div>
            <div id="dict-list" class="space-y-1">
                <div class="text-xs text-white/20">Cargando...</div>
            </div>
        </div>

        <!-- Table -->
        <div class="glass rounded-xl overflow-hidden">
            <table class="w-full">
                <thead>
                    <tr class="text-white/40 text-xs uppercase tracking-wider border-b border-white/5">
                        <th class="py-3 px-2 text-center w-10">
                            <input type="checkbox" id="select-all" onclick="toggleSelectAll(this)" class="accent-purple-500 cursor-pointer">
                        </th>
                        <th class="py-3 px-4 text-left w-36">Hora</th>
                        <th class="py-3 px-4 text-left">Transcripcion</th>
                        <th class="py-3 px-4 text-right w-20">Dur.</th>
                        <th class="py-3 px-4 text-center w-32"></th>
                    </tr>
                </thead>
                <tbody id="tbody"></tbody>
            </table>
            <div id="empty" class="hidden text-center py-12 text-white/20 text-sm">
                No hay transcripciones aun
            </div>
        </div>

        <!-- Footer -->
        <div class="mt-4 text-center text-white/15 text-xs">
            Vflow &middot; Ctrl+Shift para grabar &middot; Groq Whisper
        </div>
    </div>

    <!-- Add to dictionary floating button (appears on text selection in transcription table) -->
    <button class="dict-add-from-history" id="dict-from-history-btn" onclick="addSelectedTextToDict()">
        📖 Añadir al diccionario
    </button>

    <!-- Selection bar -->
    <div class="selection-bar" id="selection-bar">
        <span class="count" id="sel-count">0 seleccionados</span>
        <button class="del-btn" onclick="deleteSelected()">Eliminar seleccionados</button>
        <button class="cancel-btn" onclick="clearSelection()">Cancelar</button>
    </div>

    <script>
        let allData = [];
        let renderedData = [];
        let editingId = null;
        let selectedIds = new Set();
        let expandedIds = new Set();
        let anchorIndex = null;

        async function loadData() {
            const res = await fetch('/api/transcriptions');
            allData = await res.json();
            // Remove selected IDs that no longer exist
            const existingIds = new Set(allData.map(t => t.id));
            selectedIds = new Set([...selectedIds].filter(id => existingIds.has(id)));
            renderTable(allData);
        }

        function renderTable(data) {
            renderedData = data;
            const tbody = document.getElementById('tbody');
            const empty = document.getElementById('empty');
            const badge = document.getElementById('count-badge');
            badge.textContent = data.length + ' total';

            if (data.length === 0) {
                tbody.innerHTML = '';
                empty.classList.remove('hidden');
                updateSelectionBar();
                return;
            }
            empty.classList.add('hidden');

            tbody.innerHTML = data.map((t, i) => {
                const date = new Date(t.created_at + 'Z');
                const time = date.toLocaleString('es-MX', {
                    month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit', second: '2-digit'
                });
                const dur = t.duration_seconds ? t.duration_seconds.toFixed(1) + 's' : '-';
                const srcBadge = t.source === 'youtube'
                    ? '<span class="text-white/25 text-xs ml-1" title="YouTube">▶</span>'
                    : t.source === 'system'
                        ? '<span class="text-white/25 text-xs ml-1" title="Audio del sistema">🔊</span>'
                        : '';
                const isEditing = editingId === t.id;
                const checked = selectedIds.has(t.id) ? 'checked' : '';
                const rowClass = selectedIds.has(t.id) ? 'selected-row' : '';
                const textCell = isEditing
                    ? `<textarea class="edit-area" id="edit-${t.id}">${escapeHtml(t.text)}</textarea>
                       <div class="flex gap-2 mt-1">
                           <button onclick="event.stopPropagation(); saveEdit(${t.id})"
                               class="text-xs px-2 py-1 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50">Guardar</button>
                           <button onclick="event.stopPropagation(); cancelEdit()"
                               class="text-xs px-2 py-1 rounded text-white/40 hover:text-white/60 hover:bg-white/5">Cancelar</button>
                       </div>`
                    : `<div class="text-preview" id="text-${i}">${escapeHtml(t.text)}</div>`;
                return `
                <tr class="row-hover border-b border-white/[0.03] cursor-pointer ${rowClass}" data-id="${t.id}" onclick="handleRowClick(event, ${i})">
                    <td class="py-3 px-2 text-center align-top">
                        <input type="checkbox" class="row-check accent-purple-500 cursor-pointer" data-id="${t.id}"
                            ${checked} onclick="event.stopPropagation(); handleRowSelect(event, ${i})">
                    </td>
                    <td class="py-3 px-4 text-white/30 text-xs whitespace-nowrap align-top">${time}${srcBadge}</td>
                    <td class="py-3 px-4 text-white/80 text-sm align-top text-cell">${textCell}</td>
                    <td class="py-3 px-4 text-white/20 text-xs text-right align-top">${dur}</td>
                    <td class="py-3 px-4 text-center align-top whitespace-nowrap">
                        <button onclick="event.stopPropagation(); copyText(${i}, this)"
                            class="text-white/20 hover:text-white/60 text-xs px-1.5 py-1 rounded hover:bg-white/5"
                            title="Copiar">Copiar</button>
                        <button onclick="event.stopPropagation(); startEdit(${t.id})"
                            class="text-white/20 hover:text-white/60 text-xs px-1.5 py-1 rounded hover:bg-white/5 ml-0.5"
                            title="Editar">&#9998;</button>
                        <button onclick="event.stopPropagation(); deleteSingle(${t.id}, this)"
                            class="text-white/20 hover:text-red-400 text-xs px-1.5 py-1 rounded hover:bg-red-500/10 ml-0.5"
                            title="Eliminar">&#10005;</button>
                    </td>
                </tr>`;
            }).join('');

            syncCheckboxUI();

            // Restore expanded state after re-render
            expandedIds.forEach(id => {
                const row = tbody.querySelector(`tr[data-id="${id}"]`);
                if (row) {
                    const preview = row.querySelector('.text-preview');
                    if (preview) preview.classList.add('expanded');
                }
            });
        }

        function toggleExpand(row) {
            const preview = row.querySelector('.text-preview');
            const id = parseInt(row.dataset.id);
            if (preview) {
                preview.classList.toggle('expanded');
                if (preview.classList.contains('expanded')) {
                    expandedIds.add(id);
                } else {
                    expandedIds.delete(id);
                }
            }
        }

        function copyText(index, btn) {
            navigator.clipboard.writeText(renderedData[index].text);
            const row = btn.closest('tr');
            row.classList.add('copied');
            btn.textContent = 'OK';
            setTimeout(() => { btn.textContent = 'Copiar'; row.classList.remove('copied'); }, 1000);
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // --- Edit ---
        function startEdit(id) {
            editingId = id;
            renderTable(allData);
            setTimeout(() => {
                const ta = document.getElementById('edit-' + id);
                if (ta) { ta.focus(); ta.selectionStart = ta.value.length; }
            }, 50);
        }

        function cancelEdit() {
            editingId = null;
            renderTable(allData);
        }

        async function saveEdit(id) {
            const ta = document.getElementById('edit-' + id);
            if (!ta) return;
            const newText = ta.value.trim();
            if (!newText) return;
            await fetch('/api/transcriptions/' + id, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text: newText})
            });
            editingId = null;
            loadData();
        }

        // --- Delete single ---
        async function deleteSingle(id, btn) {
            if (!confirm('¿Eliminar esta transcripcion?')) return;
            if (!confirm('¿Estas seguro? Esta accion no se puede deshacer.')) return;
            const row = btn.closest('tr');
            row.classList.add('deleted');
            setTimeout(async () => {
                await fetch('/api/transcriptions/' + id, {method: 'DELETE'});
                loadData();
            }, 350);
        }

        // --- Bulk delete ---
        async function bulkDelete(range, label) {
            document.getElementById('cleanup-dropdown').classList.remove('open');
            const labels = {day: 'las transcripciones de hoy', week: 'las transcripciones de la ultima semana',
                month: 'las transcripciones del ultimo mes', all: 'TODAS las transcripciones'};
            const desc = labels[range] || range;
            if (!confirm('¿Eliminar ' + desc + '?')) return;
            if (!confirm('¿Estas seguro? Esta accion no se puede deshacer.')) return;
            let url = '/api/transcriptions?range=' + range;
            if (range === 'day' && label === 'hoy') {
                url += '&date=' + new Date().toISOString().slice(0,10);
            }
            await fetch(url, {method: 'DELETE'});
            loadData();
        }

        async function deleteByDate() {
            document.getElementById('cleanup-dropdown').classList.remove('open');
            const dateStr = prompt('Ingresa la fecha (YYYY-MM-DD):');
            if (!dateStr || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(dateStr)) return;
            if (!confirm('¿Eliminar las transcripciones del ' + dateStr + '?')) return;
            if (!confirm('¿Estas seguro? Esta accion no se puede deshacer.')) return;
            await fetch('/api/transcriptions?range=day&date=' + dateStr, {method: 'DELETE'});
            loadData();
        }

        // --- Selection logic ---
        function handleRowClick(e, index) {
            if (e.target.closest('button, textarea, .edit-area')) return;
            if (e.target.classList.contains('row-check')) return;
            // Don't toggle expand if user made a text selection
            const sel = window.getSelection();
            if (sel && !sel.isCollapsed && sel.toString().trim().length > 0) return;
            if (e.ctrlKey || e.shiftKey || e.metaKey) {
                e.preventDefault();
                handleRowSelect(e, index);
            } else {
                toggleExpand(e.currentTarget);
            }
        }

        function handleRowSelect(e, index) {
            const id = renderedData[index].id;

            if (e.ctrlKey && e.shiftKey) {
                if (anchorIndex !== null) {
                    const [start, end] = [Math.min(anchorIndex, index), Math.max(anchorIndex, index)];
                    for (let i = start; i <= end; i++) selectedIds.add(renderedData[i].id);
                } else {
                    selectedIds.add(id);
                    anchorIndex = index;
                }
            } else if (e.shiftKey) {
                selectedIds.clear();
                if (anchorIndex !== null) {
                    const [start, end] = [Math.min(anchorIndex, index), Math.max(anchorIndex, index)];
                    for (let i = start; i <= end; i++) selectedIds.add(renderedData[i].id);
                } else {
                    selectedIds.add(id);
                    anchorIndex = index;
                }
            } else if (e.ctrlKey || e.metaKey) {
                if (selectedIds.has(id)) selectedIds.delete(id);
                else selectedIds.add(id);
                anchorIndex = index;
            } else {
                selectedIds.clear();
                selectedIds.add(id);
                anchorIndex = index;
            }

            syncCheckboxUI();
        }

        function syncCheckboxUI() {
            const visibleChecks = document.querySelectorAll('.row-check');
            visibleChecks.forEach(c => {
                const id = parseInt(c.dataset.id);
                c.checked = selectedIds.has(id);
                c.closest('tr').classList.toggle('selected-row', c.checked);
            });
            const selectAll = document.getElementById('select-all');
            const allChecked = visibleChecks.length > 0 && [...visibleChecks].every(c => c.checked);
            const someChecked = [...visibleChecks].some(c => c.checked);
            selectAll.checked = allChecked;
            selectAll.indeterminate = !allChecked && someChecked;
            updateSelectionBar();
        }

        function toggleSelectAll(cb) {
            renderedData.forEach(t => {
                if (cb.checked) selectedIds.add(t.id);
                else selectedIds.delete(t.id);
            });
            anchorIndex = null;
            syncCheckboxUI();
        }

        function updateSelectionBar() {
            const bar = document.getElementById('selection-bar');
            const count = document.getElementById('sel-count');
            if (selectedIds.size > 0) {
                bar.classList.add('visible');
                count.textContent = selectedIds.size + ' seleccionado' + (selectedIds.size > 1 ? 's' : '');
            } else {
                bar.classList.remove('visible');
            }
        }

        function clearSelection() {
            selectedIds.clear();
            anchorIndex = null;
            syncCheckboxUI();
        }

        async function deleteSelected() {
            const n = selectedIds.size;
            if (!confirm('¿Eliminar ' + n + ' transcripcion' + (n > 1 ? 'es' : '') + ' seleccionada' + (n > 1 ? 's' : '') + '?')) return;
            if (!confirm('¿Estas seguro? Esta accion no se puede deshacer.')) return;
            await fetch('/api/transcriptions/delete-batch', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ids: [...selectedIds]})
            });
            selectedIds.clear();
            loadData();
        }

        // Close dropdown on outside click
        document.addEventListener('click', (e) => {
            const dd = document.getElementById('cleanup-dropdown');
            if (dd && !dd.contains(e.target)) dd.classList.remove('open');
        });

        // Search
        document.getElementById('search').addEventListener('input', (e) => {
            const q = e.target.value.toLowerCase();
            if (!q) { renderTable(allData); return; }
            renderTable(allData.filter(t => t.text.toLowerCase().includes(q)));
        });

        // Collapse all expanded rows on Escape
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                expandedIds.clear();
                document.querySelectorAll('.text-preview.expanded').forEach(el => {
                    el.classList.remove('expanded');
                });
            }
        });

        // Auto-refresh every 5 seconds
        loadData();
        setInterval(loadData, 5000);

        // --- Settings panel ---
        async function toggleSettings() {
            const panel = document.getElementById('settings-panel');
            if (panel.classList.contains('hidden')) {
                panel.classList.remove('hidden');
                await loadSettings();
            } else {
                panel.classList.add('hidden');
            }
        }

        async function loadSettings() {
            const [settings, mics] = await Promise.all([
                fetch('/api/settings').then(r => r.json()),
                fetch('/api/microphones').then(r => r.json()),
            ]);
            document.getElementById('cfg-language').value = settings.language || 'es';
            document.getElementById('cfg-translate-target').value = settings.translate_target || 'en';
            document.getElementById('cfg-sounds').checked = settings.sounds_enabled !== false;
            const vol = settings.beep_volume || 2;
            document.getElementById('cfg-beep-volume').value = vol;
            document.getElementById('cfg-beep-volume-label').textContent = vol;
            document.getElementById('cfg-save-history').checked = settings.save_history !== false;
            const retDays = settings.retention_days || 0;
            const retSelect = document.getElementById('cfg-retention-days');
            const retOpt = retSelect.querySelector('option[value="' + retDays + '"]');
            retSelect.value = retOpt ? String(retDays) : '0';
            const micSelect = document.getElementById('cfg-microphone');
            micSelect.innerHTML = '<option value="">Sistema por defecto</option>';
            mics.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m.name;
                opt.textContent = m.name;
                if (m.name === settings.device_name) opt.selected = true;
                micSelect.appendChild(opt);
            });
            // Fuente de audio
            document.getElementById('cfg-audio-source').value = settings.audio_source || 'mic';
            // Backend local
            document.getElementById('cfg-backend').value = settings.transcription_backend || 'groq';
            document.getElementById('cfg-local-model').value = settings.local_whisper_model || 'small';
            document.getElementById('cfg-groq-fallback').checked = settings.groq_fallback === true;
            // Backend de insights (análisis de reuniones)
            document.getElementById('cfg-insights-backend').value = settings.insights_backend || 'groq';
            document.getElementById('cfg-insights-model').value = settings.insights_endpoint_model || 'qwen/qwen2.5-vl-7b';
            onInsightsBackendChange();
            updateLocalModelSection();
            refreshLocalModelStatus();
            updateLocalTranslationNote();
        }

        async function saveSettings() {
            const data = {
                language: document.getElementById('cfg-language').value,
                translate_target: document.getElementById('cfg-translate-target').value,
                device_name: document.getElementById('cfg-microphone').value,
                sounds_enabled: document.getElementById('cfg-sounds').checked ? 'true' : 'false',
                beep_volume: document.getElementById('cfg-beep-volume').value,
                save_history: document.getElementById('cfg-save-history').checked ? 'true' : 'false',
                retention_days: document.getElementById('cfg-retention-days').value,
                audio_source: document.getElementById('cfg-audio-source').value,
                transcription_backend: document.getElementById('cfg-backend').value,
                local_whisper_model: document.getElementById('cfg-local-model').value,
                groq_fallback: document.getElementById('cfg-groq-fallback').checked ? 'true' : 'false',
                insights_backend: document.getElementById('cfg-insights-backend').value,
                insights_endpoint_model: document.getElementById('cfg-insights-model').value.trim(),
            };
            await fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data),
            });
            const saved = document.getElementById('cfg-saved');
            saved.style.opacity = '1';
            setTimeout(() => { saved.style.opacity = '0'; }, 2000);
        }

        // --- Backend local ---
        function onBackendChange() {
            updateLocalModelSection();
        }

        // --- Backend de insights (análisis de reuniones) ---
        function onInsightsBackendChange() {
            const backend = document.getElementById('cfg-insights-backend').value;
            const wrap = document.getElementById('cfg-insights-endpoint-wrap');
            wrap.classList.toggle('hidden', backend !== 'endpoint');
        }

        function updateLocalModelSection() {
            const backend = document.getElementById('cfg-backend').value;
            const section = document.getElementById('local-model-section');
            if (backend === 'local') {
                section.classList.remove('hidden');
                refreshLocalModelStatus();
            } else {
                section.classList.add('hidden');
            }
            updateLocalTranslationNote();
        }

        // Idiomas legibles para el aviso
        const _langNames = {en:'inglés',es:'español',fr:'francés',de:'alemán',it:'italiano',pt:'portugués',nl:'neerlandés',ru:'ruso',zh:'chino',ja:'japonés',ko:'coreano',ar:'árabe'};
        function _langLabel(code) { return _langNames[code] || code; }

        function updateLocalTranslationNote() {
            const note = document.getElementById('local-translation-note');
            const noteText = document.getElementById('local-translation-note-text');
            if (!note) return;
            const backend = document.getElementById('cfg-backend').value;
            if (backend !== 'local') { note.classList.add('hidden'); return; }
            const target = (document.getElementById('cfg-translate-target').value || 'en').toLowerCase();
            const fallback = document.getElementById('cfg-groq-fallback').checked;
            note.classList.remove('hidden');
            if (target === 'en') {
                // Informativo discreto: azul tenue
                note.className = 'mt-2 rounded-lg px-3 py-2 text-xs flex items-start gap-2 bg-blue-900/20 border border-blue-500/20 text-blue-300/70';
                noteText.textContent = 'El modo local solo traduce a inglés. Para traducir a otros idiomas, activa “Permitir Groq como respaldo” o cambia al backend Groq.';
            } else if (fallback) {
                // Amber suave — se usará Groq
                note.className = 'mt-2 rounded-lg px-3 py-2 text-xs flex items-start gap-2 bg-amber-900/30 border border-amber-500/30 text-amber-300/90';
                noteText.textContent = 'Traducción a ' + _langLabel(target) + ': se usará Groq cuando el local falle (el audio saldrá a internet).';
            } else {
                // Amber más visible — advertencia real
                note.className = 'mt-2 rounded-lg px-3 py-2 text-xs flex items-start gap-2 bg-amber-900/40 border border-amber-500/50 text-amber-200';
                noteText.textContent = 'Atención: la traducción a ' + _langLabel(target) + ' devolverá el texto sin traducir con el backend local. Activa el respaldo Groq o usa el backend Groq.';
            }
        }

        let _statusPollInterval = null;

        async function refreshLocalModelStatus() {
            try {
                const status = await fetch('/api/local-model/status').then(r => r.json());
                const statusText = document.getElementById('local-model-status-text');
                const btnDownload = document.getElementById('btn-download-model');
                const progressWrap = document.getElementById('local-download-progress-wrap');
                const progressBar = document.getElementById('local-download-bar');
                const progressPct = document.getElementById('local-download-pct');

                if (status.downloading) {
                    statusText.textContent = 'Descargando modelo ' + status.model + '...';
                    btnDownload.disabled = true;
                    progressWrap.classList.remove('hidden');
                    if (status.progress !== null) {
                        const pct = Math.round(status.progress * 100);
                        progressBar.style.width = pct + '%';
                        progressPct.textContent = pct + '%';
                    }
                    if (!_statusPollInterval) {
                        _statusPollInterval = setInterval(refreshLocalModelStatus, 1500);
                    }
                } else {
                    if (_statusPollInterval) { clearInterval(_statusPollInterval); _statusPollInterval = null; }
                    progressWrap.classList.add('hidden');
                    btnDownload.disabled = false;
                    if (status.error) {
                        statusText.textContent = 'Error: ' + status.error;
                    } else if (status.downloaded) {
                        statusText.textContent = 'Modelo ' + status.model + ' descargado ✓';
                        btnDownload.textContent = 'Re-descargar';
                    } else {
                        statusText.textContent = 'Modelo ' + status.model + ' no descargado';
                        btnDownload.textContent = 'Descargar modelo';
                    }
                }
            } catch(e) {
                console.error('Error al consultar estado del modelo:', e);
            }
        }

        // --- Dictionary panel ---
        let _dictEntries = [];
        let _dictBudget = {included: 0, total: 0, included_ids: []};

        async function toggleDictionary() {
            const panel = document.getElementById('dictionary-panel');
            if (panel.classList.contains('hidden')) {
                panel.classList.remove('hidden');
                await loadDictionary();
            } else {
                panel.classList.add('hidden');
            }
        }

        function toggleShortcuts() {
            const panel = document.getElementById('shortcuts-panel');
            panel.classList.toggle('hidden');
        }

        // ---- Modo reunión (captura dual mic + sistema) ----
        let _mtPollInterval = null;
        // Firmas del último render: evitan repintar el DOM si el contenido no cambió
        // (esto mata el "parpadeo" de reemplazar todo el bloque cada 2s).
        let _mtSegSig = null;
        let _mtInsSig = null;
        let _mtStatusSig = null;
        let _mtSeenInsightIds = new Set();  // ids ya mostrados → solo los nuevos animan (fade)
        let _mtViewMode = 'revision';       // 'foco' (mínimo, en reunión) | 'revision' (todo)
        let _mtMinutesShown = false;        // acta ya mostrada para la reunión terminada actual

        function setMeetingView(mode) {
            _mtViewMode = mode;
            const f = document.getElementById('mt-view-foco'), r = document.getElementById('mt-view-revision');
            f.className = 'px-2 py-0.5 rounded ' + (mode === 'foco' ? 'bg-purple-600/40 text-purple-200' : 'text-white/40 hover:text-white/70');
            r.className = 'px-2 py-0.5 rounded ' + (mode === 'revision' ? 'bg-purple-600/40 text-purple-200' : 'text-white/40 hover:text-white/70');
            _mtInsSig = null; _mtSeenInsightIds = new Set();  // forzar re-render en el nuevo modo
            loadMeeting();
        }

        function toggleMeeting() {
            const panel = document.getElementById('meeting-panel');
            const isHidden = panel.classList.contains('hidden');
            panel.classList.toggle('hidden');
            if (isHidden) {
                loadMeeting().then(st => { if (st && st.active) _startMtPoll(); });
            } else {
                _stopMtPoll();
            }
        }

        function _stopMtPoll() {
            if (_mtPollInterval) { clearInterval(_mtPollInterval); _mtPollInterval = null; }
        }
        function _startMtPoll() {
            if (_mtPollInterval) return;
            _mtPollInterval = setInterval(loadMeeting, 2000);
        }

        async function loadMeeting() {
            try {
                const res = await fetch('/api/meeting');
                const data = await res.json();
                const st = data.status || {};
                renderMeeting(st, data.segments || []);
                renderInsights(data.insights || {});
                // Mostrar el acta cuando la reunión haya terminado, sin importar cómo
                // se terminó (botón, AltGr+R o bandeja). Mientras está activa, se rearma.
                if (st.active) {
                    _mtMinutesShown = false;
                } else if (data.last_minutes && !_mtMinutesShown) {
                    renderMinutes(data.last_minutes);
                    _mtMinutesShown = true;
                }
                return st;
            } catch(e) { return {}; }
        }

        // Sufijo de un pendiente: responsable + fecha/hora si existen → "(María · 📅 viernes 15:00)"
        function pendMeta(p) {
            const parts = [];
            if (p && p.responsable) parts.push(escapeHtml(String(p.responsable)));
            const fh = [p && p.fecha, p && p.hora].filter(Boolean).map(x => escapeHtml(String(x))).join(' ');
            if (fh) parts.push('📅 ' + fh);
            return parts.length ? ' <span class="text-white/35">(' + parts.join(' · ') + ')</span>' : '';
        }

        function renderInsights(ins) {
            const el = document.getElementById('mt-insights');
            if (!el) return;
            // Solo repintar si el análisis cambió de verdad (anti-parpadeo); la firma
            // incluye el modo para que cambiar Foco↔Revisión repinte.
            const insSig = _mtViewMode + '|' + JSON.stringify(ins || {});
            if (insSig === _mtInsSig) return;
            _mtInsSig = insSig;
            const temas = ins.temas || [];
            const pend = ins.pendientes || [];
            const prop = (ins.propuestas || []).filter(p => (p.confianza || 'alta') === 'alta');
            if (!temas.length && !pend.length && !prop.length) {
                el.innerHTML = '<div class="text-xs text-white/20">Temas, pendientes y propuestas aparecerán aquí a medida que avance la reunión.</div>';
                return;
            }
            // fade solo para ids no vistos antes (los ya mostrados no re-animan)
            const fadeCls = (id) => {
                if (id == null) return '';
                if (_mtSeenInsightIds.has(id)) return '';
                _mtSeenInsightIds.add(id); return ' mt-fade';
            };
            // Modo FOCO: mínima distracción — solo el resumen de conteos + pendientes (lo accionable)
            if (_mtViewMode === 'foco') {
                let fhtml = '<div class="text-xs text-white/50 mb-2">' + temas.length + ' temas · '
                    + pend.length + ' pendientes · ' + prop.length + ' propuestas</div>';
                if (pend.length) {
                    fhtml += pend.map(p => {
                        return '<div class="text-xs text-white/75 mb-0.5' + fadeCls(p.id) + '">☐ ' + escapeHtml(String(p.texto || '')) + pendMeta(p) + '</div>';
                    }).join('');
                } else {
                    fhtml += '<div class="text-[11px] text-white/25">Sin pendientes detectados aún.</div>';
                }
                fhtml += '<div class="text-[10px] text-white/20 mt-2">Modo Foco: solo lo accionable. Cambia a Revisión para ver todo.</div>';
                el.innerHTML = fhtml;
                return;
            }
            let html = '';
            if (temas.length) {
                html += '<div><div class="text-[11px] uppercase tracking-wide text-white/30 mb-1">Temas</div>'
                    + temas.map(t => '<div class="text-xs text-white/75 mb-0.5' + fadeCls(t.id) + '">• ' + escapeHtml(String(t.text != null ? t.text : t)) + '</div>').join('') + '</div>';
            }
            if (pend.length) {
                html += '<div><div class="text-[11px] uppercase tracking-wide text-amber-300/50 mb-1">Pendientes</div>'
                    + pend.map(p => {
                        return '<div class="text-xs text-white/75 mb-0.5' + fadeCls(p.id) + '">☐ ' + escapeHtml(String(p.texto || '')) + pendMeta(p) + '</div>';
                    }).join('') + '</div>';
            }
            if (prop.length) {
                html += '<div><div class="text-[11px] uppercase tracking-wide text-sky-300/50 mb-1">Propuestas</div>'
                    + prop.map(p => '<div class="text-xs text-white/75 mb-0.5' + fadeCls(p.id) + '">💡 ' + escapeHtml(String(p.texto || '')) + '</div>').join('') + '</div>';
            }
            const citas = ins.citas || [];
            if (citas.length) {
                html += '<div><div class="text-[11px] uppercase tracking-wide text-emerald-300/50 mb-1">Próximas reuniones</div>'
                    + citas.map(c => '<div class="text-xs text-white/75 mb-0.5' + fadeCls(c.id) + '">📅 ' + escapeHtml(String(c.texto || '')) + pendMeta({fecha: c.fecha, hora: c.hora}) + '</div>').join('') + '</div>';
            }
            el.innerHTML = html;
        }

        function renderMinutes(m) {
            const wrap = document.getElementById('mt-minutes');
            const body = document.getElementById('mt-minutes-body');
            if (!wrap || !body) return;
            m = m || {};
            const dec = m.decisiones || [], tem = m.temas || [], pen = m.pendientes || [], prop = m.propuestas || [], cit = m.citas || [];
            if (!m.resumen && !dec.length && !tem.length && !pen.length && !prop.length && !cit.length) {
                wrap.classList.add('hidden');
                return;
            }
            let html = '';
            if (m.resumen) html += '<p class="text-white/80">' + escapeHtml(String(m.resumen)) + '</p>';
            if (dec.length) html += '<div><div class="text-xs text-white/40 mt-2 mb-1">Decisiones</div>'
                + dec.map(d => '<div class="text-xs text-white/75">• ' + escapeHtml(String(d)) + '</div>').join('') + '</div>';
            if (pen.length) html += '<div><div class="text-xs text-amber-300/50 mt-2 mb-1">Pendientes</div>'
                + pen.map(p => '<div class="text-xs text-white/75">☐ ' + escapeHtml(String(p.texto || p)) + pendMeta(p) + '</div>').join('') + '</div>';
            if (prop.length) html += '<div><div class="text-xs text-sky-300/50 mt-2 mb-1">Propuestas</div>'
                + prop.map(p => '<div class="text-xs text-white/75">💡 ' + escapeHtml(String(p.texto != null ? p.texto : p)) + '</div>').join('') + '</div>';
            if (cit.length) html += '<div><div class="text-xs text-emerald-300/50 mt-2 mb-1">Próximas reuniones</div>'
                + cit.map(c => '<div class="text-xs text-white/75">📅 ' + escapeHtml(String(c.texto || c)) + pendMeta({fecha: c.fecha, hora: c.hora}) + '</div>').join('') + '</div>';
            if (tem.length) html += '<div><div class="text-xs text-white/40 mt-2 mb-1">Temas tratados</div>'
                + tem.map(t => '<div class="text-xs text-white/75">• ' + escapeHtml(String(t)) + '</div>').join('') + '</div>';
            body.innerHTML = html;
            wrap.classList.remove('hidden');
        }

        function renderMeeting(status, segments) {
            const startBtn = document.getElementById('mt-start');
            const stopBtn = document.getElementById('mt-stop');
            const statusEl = document.getElementById('mt-status');
            const container = document.getElementById('mt-transcript');

            // Estado/botones: solo tocar el DOM si cambió (anti-parpadeo)
            const thinking = status.active && status.insight_running ? ' · analizando…' : '';
            const statusSig = JSON.stringify([status.active, status.elapsed_fmt, status.segment_count, status.sys_available, !!status.insight_running, status.error || '']);
            if (statusSig !== _mtStatusSig) {
                _mtStatusSig = statusSig;
                if (status.active) {
                    startBtn.classList.add('hidden');
                    stopBtn.classList.remove('hidden');
                    let txt = 'Grabando ' + (status.elapsed_fmt || '00:00') + ' · ' + (status.segment_count || 0) + ' intervenciones';
                    if (status.sys_available === false) txt += ' · solo micrófono';
                    if (status.error) txt += ' · ⚠ ' + status.error;
                    statusEl.textContent = txt + thinking;
                    statusEl.className = status.error ? 'text-xs text-amber-300' : 'text-xs text-red-300';
                } else {
                    startBtn.classList.remove('hidden');
                    stopBtn.classList.add('hidden');
                    statusEl.textContent = '';
                }
            }

            // Transcript: render incremental. Solo añade los segmentos nuevos al final
            // (append) en vez de reconstruir todo el bloque → sin parpadeo ni saltos de scroll.
            const segSig = segments.length + ':' + (segments.length ? segments[segments.length - 1].text.slice(0, 24) : '');
            if (segSig === _mtSegSig) return;  // nada nuevo
            const prevCount = (_mtSegSig && container.dataset.count) ? parseInt(container.dataset.count, 10) : 0;
            _mtSegSig = segSig;

            if (!segments.length) {
                container.innerHTML = status.active
                    ? '<div class="text-xs text-white/20">Escuchando… el texto aparecerá cada ~20s.</div>'
                    : '<div class="text-xs text-white/20">El transcript en vivo aparecerá aquí cuando inicies una reunión.</div>';
                container.dataset.count = '0';
                return;
            }
            const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;
            if (prevCount === 0 || prevCount > segments.length) {
                container.innerHTML = '';  // primer render o reset
            }
            const start = (prevCount > 0 && prevCount <= segments.length) ? prevCount : 0;
            if (start === 0) container.innerHTML = '';
            for (let i = start; i < segments.length; i++) {
                const s = segments[i];
                const color = (s.speaker === 'Yo') ? 'text-purple-300' : 'text-sky-300';
                const div = document.createElement('div');
                div.className = 'text-sm text-white/80 leading-snug mt-fade';
                div.innerHTML = '<span class="text-[10px] font-mono text-white/30 mr-1">' + s.time + '</span>'
                    + '<span class="text-xs font-medium ' + color + ' mr-1">' + s.speaker + ':</span>'
                    + escapeHtml(s.text);
                container.appendChild(div);
            }
            container.dataset.count = String(segments.length);
            if (nearBottom) container.scrollTop = container.scrollHeight;  // solo auto-scroll si ya estabas abajo
        }

        async function startMeeting() {
            const fb = document.getElementById('mt-feedback');
            fb.classList.add('hidden');
            document.getElementById('mt-minutes').classList.add('hidden');  // limpiar acta previa
            _mtSegSig = _mtInsSig = _mtStatusSig = null;  // forzar render limpio de la nueva reunión
            _mtSeenInsightIds = new Set();
            _mtMinutesShown = false;
            document.getElementById('mt-transcript').dataset.count = '0';
            try {
                const res = await fetch('/api/meeting/start', {method:'POST'});
                const data = await res.json();
                if (!data.ok) {
                    fb.textContent = data.error || 'No se pudo iniciar la reunión.';
                    fb.className = 'text-xs mb-2 text-red-300';
                    fb.classList.remove('hidden');
                    return;
                }
                if (data.sys_available === false) {
                    fb.textContent = 'Aviso: no se detectó audio del sistema; grabando solo el micrófono.';
                    fb.className = 'text-xs mb-2 text-amber-300';
                    fb.classList.remove('hidden');
                }
                await loadMeeting();
                _startMtPoll();
            } catch(e) {
                fb.textContent = 'Error de red al iniciar la reunión.';
                fb.className = 'text-xs mb-2 text-red-300';
                fb.classList.remove('hidden');
            }
        }

        async function stopMeeting() {
            const statusEl = document.getElementById('mt-status');
            statusEl.textContent = 'Terminando, transcribiendo lo último y generando el acta…';
            statusEl.className = 'text-xs text-white/40';
            try {
                const res = await fetch('/api/meeting/stop', {method:'POST'});
                const data = await res.json();
                _stopMtPoll();
                await loadMeeting();
                renderMinutes(data.minutes);
                _mtMinutesShown = true;  // ya mostrada por esta vía (evita doble render del poller)
                const fb = document.getElementById('mt-feedback');
                const dur = data.duration_seconds || 0;
                const mins = Math.floor(dur/60), secs = Math.floor(dur%60);
                const cnt = (data.segments||[]).length;
                if (!cnt) {
                    fb.textContent = 'Reunión terminada (sin voz detectada).';
                } else if (data.saved) {
                    fb.textContent = 'Reunión guardada: ' + cnt + ' intervenciones, ' + mins + 'm ' + secs + 's.';
                } else {
                    fb.textContent = 'Reunión terminada: ' + cnt + ' intervenciones (historial desactivado).';
                }
                fb.className = 'text-xs mb-2 text-emerald-300';
                fb.classList.remove('hidden');
            } catch(e) {
                _stopMtPoll();
            }
        }

        async function loadDictionary() {
            const res = await fetch('/api/dictionary');
            const data = await res.json();
            _dictEntries = data.entries || [];
            _dictBudget = data.budget || {included: 0, total: 0, included_ids: []};
            renderDictBudget();
            renderDictList(_dictEntries);
        }

        function renderDictBudget() {
            const lbl = document.getElementById('dict-budget-label');
            const fill = document.getElementById('dict-budget-fill');
            if (!lbl || !fill) return;
            const {included, total, used_chars, max_chars} = _dictBudget;
            const pct = max_chars > 0 ? Math.round((used_chars || 0) / max_chars * 100) : 0;
            if (included < total) {
                lbl.textContent = `Vocabulario en prompt: ${included} de ${total} términos (espacio lleno)`;
            } else {
                lbl.textContent = `Vocabulario en prompt: ${included} término${included === 1 ? '' : 's'} — ${pct}% del espacio usado`;
            }
            fill.style.width = pct + '%';
        }

        function filterDictList() {
            const q = (document.getElementById('dict-search').value || '').toLowerCase();
            if (!q) { renderDictList(_dictEntries); return; }
            renderDictList(_dictEntries.filter(e =>
                (e.replace_from || '').toLowerCase().includes(q) ||
                (e.replace_to || '').toLowerCase().includes(q)
            ));
        }

        function renderDictList(entries) {
            const container = document.getElementById('dict-list');
            if (!entries.length) {
                container.innerHTML = '<div class="text-xs text-white/20">No hay entradas aún. Añade palabras o pares de corrección.</div>';
                return;
            }
            const includedSet = new Set(_dictBudget.included_ids || []);
            container.innerHTML = entries.map(e => {
                const label = e.replace_from
                    ? `<span class="text-white/50">${escapeHtml(e.replace_from)}</span> <span class="text-white/25 mx-1">→</span> <span class="text-white/80">${escapeHtml(e.replace_to)}</span>`
                    : `<span class="text-white/80">${escapeHtml(e.replace_to)}</span>`;
                const checked = e.enabled ? 'checked' : '';
                const pinned = e.pinned ? 'pinned' : '';
                const pinTitle = e.pinned ? 'Desfijado del prompt' : 'Fijar en el prompt (prioridad)';
                const pinIcon = e.pinned ? '★' : '☆';
                const inBudget = includedSet.has(e.id);
                const dimmed = !inBudget ? 'dict-entry-dimmed' : '';
                const outOfPromptTitle = !inBudget ? ' title="Fuera del prompt por límite; el reemplazo sigue activo"' : '';
                const hitBadge = (e.hit_count > 0)
                    ? `<span class="text-white/20 text-xs ml-1" title="Correcciones aplicadas">×${e.hit_count}</span>`
                    : '';
                return `
                <div class="flex items-center gap-3 py-1.5 border-b border-white/[0.04] ${dimmed}" data-dict-id="${e.id}"${outOfPromptTitle}>
                    <button class="dict-pin-btn ${pinned}" title="${pinTitle}" onclick="pinDictEntry(${e.id}, ${e.pinned ? 0 : 1})">${pinIcon}</button>
                    <label class="toggle-switch flex-shrink-0">
                        <input type="checkbox" ${checked} onchange="toggleDictEntry(${e.id}, this.checked)">
                        <span class="toggle-slider"></span>
                    </label>
                    <span class="text-sm flex-1">${label}${hitBadge}</span>
                    <button onclick="deleteDictEntry(${e.id})"
                        class="text-white/20 hover:text-red-400 text-xs px-1.5 py-1 rounded hover:bg-red-500/10">&#10005;</button>
                </div>`;
            }).join('');
        }

        function _showDictError(msg) {
            // Reutiliza el patrón de notificación del dashboard (cfg-saved) o alert como fallback
            const saved = document.getElementById('cfg-saved');
            if (saved) {
                saved.textContent = '✗ ' + msg;
                saved.style.color = '#f87171';
                saved.style.opacity = '1';
                setTimeout(() => {
                    saved.style.opacity = '0';
                    saved.style.color = '';
                    saved.textContent = 'Guardado ✓';
                }, 4000);
            } else {
                alert(msg);
            }
        }

        async function addDictEntry(e) {
            e.preventDefault();
            const replaceTo = document.getElementById('dict-replace-to').value.trim();
            const replaceFrom = document.getElementById('dict-replace-from').value.trim();
            if (!replaceTo) return;
            try {
                const res = await fetch('/api/dictionary', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({replace_to: replaceTo, replace_from: replaceFrom || undefined}),
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    _showDictError(err.error || ('Error ' + res.status));
                    return;
                }
            } catch(ex) {
                _showDictError('Error de red: ' + ex);
                return;
            }
            document.getElementById('dict-replace-to').value = '';
            document.getElementById('dict-replace-from').value = '';
            await loadDictionary();
        }

        async function deleteDictEntry(id) {
            if (!confirm('¿Eliminar esta entrada del diccionario?')) return;
            try {
                const res = await fetch('/api/dictionary/' + id, {method: 'DELETE'});
                if (!res.ok && res.status !== 204) {
                    const err = await res.json().catch(() => ({}));
                    _showDictError(err.error || ('Error ' + res.status));
                    return;
                }
            } catch(ex) {
                _showDictError('Error de red: ' + ex);
                return;
            }
            await loadDictionary();
        }

        async function toggleDictEntry(id, enabled) {
            await fetch('/api/dictionary/' + id, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: enabled}),
            });
            await loadDictionary();
        }

        async function pinDictEntry(id, pinned) {
            await fetch('/api/dictionary/' + id, {
                method: 'PATCH',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({pinned: !!pinned}),
            });
            await loadDictionary();
        }

        async function importDictCSV(input) {
            const file = input.files[0];
            if (!file) return;
            const fd = new FormData();
            fd.append('file', file);
            const resultEl = document.getElementById('dict-import-result');
            resultEl.classList.remove('hidden');
            resultEl.textContent = 'Importando…';
            resultEl.style.color = 'rgba(255,255,255,0.4)';
            try {
                const res = await fetch('/api/dictionary/import', {method: 'POST', body: fd});
                const data = await res.json();
                if (res.ok) {
                    resultEl.textContent = `Importadas: ${data.imported}, omitidas: ${data.skipped}`;
                    resultEl.style.color = '#4ade80';
                } else {
                    resultEl.textContent = data.error || 'Error al importar';
                    resultEl.style.color = '#f87171';
                }
            } catch(ex) {
                resultEl.textContent = 'Error de red: ' + ex;
                resultEl.style.color = '#f87171';
            }
            input.value = '';
            setTimeout(() => resultEl.classList.add('hidden'), 5000);
            await loadDictionary();
        }

        // --- Añadir al diccionario desde historial (selección de texto) ---
        let _dictFromHistoryTimer = null;

        document.addEventListener('mouseup', (e) => {
            const tbody = document.getElementById('tbody');
            if (!tbody) return;
            const sel = window.getSelection();
            if (!sel || sel.isCollapsed) {
                hideDictFromHistoryBtn();
                return;
            }
            const selectedText = sel.toString().trim();
            if (!selectedText || selectedText.length > 100) {
                hideDictFromHistoryBtn();
                return;
            }
            // Solo si la selección está dentro de la tabla de transcripciones
            if (!tbody.contains(sel.anchorNode) && !tbody.contains(sel.focusNode)) {
                hideDictFromHistoryBtn();
                return;
            }
            const btn = document.getElementById('dict-from-history-btn');
            btn._selectedText = selectedText;
            btn.style.left = (e.pageX + 8) + 'px';
            btn.style.top = (e.pageY - 36) + 'px';
            btn.style.display = 'block';
        });

        document.addEventListener('selectionchange', () => {
            const sel = window.getSelection();
            if (!sel || sel.isCollapsed) {
                // Pequeño delay para no ocultar antes del click
                if (_dictFromHistoryTimer) clearTimeout(_dictFromHistoryTimer);
                _dictFromHistoryTimer = setTimeout(hideDictFromHistoryBtn, 300);
            }
        });

        function hideDictFromHistoryBtn() {
            const btn = document.getElementById('dict-from-history-btn');
            if (btn) btn.style.display = 'none';
        }

        function addSelectedTextToDict() {
            const btn = document.getElementById('dict-from-history-btn');
            const text = (btn && btn._selectedText) || '';
            hideDictFromHistoryBtn();
            window.getSelection().removeAllRanges();
            // Abrir panel diccionario si está cerrado
            const panel = document.getElementById('dictionary-panel');
            if (panel.classList.contains('hidden')) {
                panel.classList.remove('hidden');
                loadDictionary();
            }
            // Scroll al panel
            panel.scrollIntoView({behavior: 'smooth', block: 'start'});
            // Prefill "Cuando escuche..."
            const fromInput = document.getElementById('dict-replace-from');
            const toInput = document.getElementById('dict-replace-to');
            if (fromInput) { fromInput.value = text; }
            // Foco en "Palabra"
            setTimeout(() => { if (toInput) toInput.focus(); }, 200);
        }

        async function downloadModel() {
            const model = document.getElementById('cfg-local-model').value;
            const btn = document.getElementById('btn-download-model');
            btn.disabled = true;
            try {
                await fetch('/api/local-model/download', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({model: model}),
                });
                // Iniciar polling
                if (_statusPollInterval) clearInterval(_statusPollInterval);
                _statusPollInterval = setInterval(refreshLocalModelStatus, 1500);
                refreshLocalModelStatus();
            } catch(e) {
                btn.disabled = false;
                alert('Error al iniciar descarga: ' + e);
            }
        }

        // --- URL Queue panel ---
        let _uqPollInterval = null;

        function toggleUrlQueue() {
            const panel = document.getElementById('url-queue-panel');
            const isHidden = panel.classList.contains('hidden');
            panel.classList.toggle('hidden');
            if (isHidden) {
                loadUrlQueue().then(summary => {
                    if (summary && (summary.pending > 0 || summary.processing > 0)) _startUqPoll();
                });
            } else {
                _stopUqPoll();
            }
        }

        function _stopUqPoll() {
            if (_uqPollInterval) { clearInterval(_uqPollInterval); _uqPollInterval = null; }
        }

        function _startUqPoll() {
            if (_uqPollInterval) return;
            _uqPollInterval = setInterval(async () => {
                const summary = await loadUrlQueue();
                if (summary && summary.pending === 0 && summary.processing === 0) {
                    _stopUqPoll();
                    loadData();
                }
            }, 2000);
        }

        async function loadUrlQueue() {
            try {
                const res = await fetch('/api/url-queue');
                const data = await res.json();
                renderUqList(data.items || []);
                return data.summary || {};
            } catch(e) { return {}; }
        }

        const _platformIcons = { youtube: '▶', tiktok: '♪', instagram: '◎', other: '🔗' };
        const _statusLabels = {
            pending: '<span class="text-white/30">Pendiente</span>',
            processing: '<span class="text-yellow-400/80">Procesando…</span>',
            done: '<span class="text-green-400/80">Listo ✓</span>',
            error: '<span class="text-red-400/80">Error</span>',
        };

        function _shortUrl(url) {
            try {
                const u = new URL(url);
                const path = u.pathname.slice(0, 30) + (u.pathname.length > 30 ? '…' : '');
                return u.hostname + path;
            } catch(e) { return url.slice(0, 40) + (url.length > 40 ? '…' : ''); }
        }

        function renderUqList(items) {
            const container = document.getElementById('uq-list');
            if (!items.length) {
                container.innerHTML = '<div class="text-xs text-white/20 py-2">Sin items en la cola.</div>';
                return;
            }
            container.innerHTML = items.map(item => {
                const icon = _platformIcons[item.platform] || '🔗';
                const statusHtml = _statusLabels[item.status] || item.status;
                const stageHtml = item.stage && item.status === 'processing'
                    ? '<span class="text-white/25 ml-1">(' + escapeHtml(item.stage) + ')</span>' : '';
                const titleHtml = item.title
                    ? '<span class="text-white/40 ml-1 truncate" style="max-width:200px" title="' + escapeHtml(item.title) + '">' + escapeHtml(item.title) + '</span>'
                    : '';
                const errorHtml = item.error && item.status === 'error'
                    ? '<div class="text-xs text-red-400/60 mt-0.5 truncate" title="' + escapeHtml(item.error) + '">' + escapeHtml(item.error) + '</div>'
                    : '';
                return '<div class="flex items-start gap-2 py-1.5 border-b border-white/[0.04]">' +
                    '<span class="text-white/30 text-xs mt-0.5 flex-shrink-0">' + icon + '</span>' +
                    '<div class="flex-1 min-w-0">' +
                    '<div class="flex items-center gap-2 flex-wrap">' +
                    '<span class="text-xs text-white/50 truncate" title="' + escapeHtml(item.url) + '">' + escapeHtml(_shortUrl(item.url)) + '</span>' +
                    '<span class="text-xs">' + statusHtml + stageHtml + '</span>' +
                    titleHtml +
                    '</div>' + errorHtml + '</div></div>';
            }).join('');
        }

        async function enqueueUrls() {
            const singleUrl = (document.getElementById('uq-url').value || '').trim();
            const bulkText = (document.getElementById('uq-bulk').value || '').trim();
            const allowInstagram = document.getElementById('uq-instagram').checked;
            const feedback = document.getElementById('uq-feedback');

            const combined = [singleUrl, bulkText].filter(Boolean).join('\\n');
            if (!combined.trim()) return;

            try {
                const res = await fetch('/api/url-queue', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({text: combined, allow_instagram: allowInstagram}),
                });
                const data = await res.json();
                feedback.classList.remove('hidden');
                if (data.enqueued > 0) {
                    feedback.style.color = '#4ade80';
                    let msg = data.enqueued + ' URL' + (data.enqueued > 1 ? 's' : '') + ' a\xf1adida' + (data.enqueued > 1 ? 's' : '') + ' a la cola.';
                    if (data.rejected && data.rejected.length > 0) {
                        msg += ' Rechazadas (' + data.rejected.length + '): ' + data.rejected.slice(0,3).join(', ');
                    }
                    feedback.textContent = msg;
                } else {
                    feedback.style.color = '#f87171';
                    feedback.textContent = 'No se a\xf1adi\xf3 ninguna URL. ' +
                        (data.rejected && data.rejected.length ? 'L\xedneas rechazadas: ' + data.rejected.slice(0,3).join(', ') : 'Verifica las URLs.');
                }
                document.getElementById('uq-url').value = '';
                document.getElementById('uq-bulk').value = '';
                setTimeout(() => feedback.classList.add('hidden'), 6000);
                await loadUrlQueue();
                _startUqPoll();
            } catch(ex) {
                feedback.classList.remove('hidden');
                feedback.style.color = '#f87171';
                feedback.textContent = 'Error de red: ' + ex;
            }
        }

        async function syncInstagramCookies() {
            const btn = document.getElementById('uq-ig-sync');
            const fb = document.getElementById('uq-ig-feedback');
            btn.disabled = true; const old = btn.textContent; btn.textContent = 'Sincronizando…';
            fb.classList.add('hidden');
            try {
                const res = await fetch('/api/instagram-cookies/sync', {method: 'POST'});
                const data = await res.json();
                fb.classList.remove('hidden');
                if (data.ok) {
                    fb.style.color = '#4ade80';
                    fb.textContent = '✓ Cookies de Instagram guardadas cifradas (' + data.count + ' desde ' + data.browser + ').';
                } else {
                    fb.style.color = '#f87171';
                    fb.textContent = data.error || 'No se pudieron sincronizar las cookies.';
                }
            } catch(ex) {
                fb.classList.remove('hidden');
                fb.style.color = '#f87171';
                fb.textContent = 'Error de red: ' + ex;
            } finally {
                btn.disabled = false; btn.textContent = old;
                setTimeout(() => fb.classList.add('hidden'), 8000);
            }
        }

        async function clearQueueFinished() {
            await fetch('/api/url-queue/clear', {method: 'POST'});
            await loadUrlQueue();
        }

        async function cancelPendingUrls() {
            await fetch('/api/url-queue/cancel-pending', {method: 'POST'});
            await loadUrlQueue();
            _stopUqPoll();
        }

        // Enter en campo URL único encola
        document.addEventListener('DOMContentLoaded', () => {
            const uqInput = document.getElementById('uq-url');
            if (uqInput) uqInput.addEventListener('keydown', e => { if (e.key === 'Enter') enqueueUrls(); });
        });

        // Watcher global: refrescar historial cuando se completan items de la cola
        let _lastQueueDone = 0;
        setInterval(async () => {
            const panel = document.getElementById('url-queue-panel');
            if (!panel || panel.classList.contains('hidden')) return;
            try {
                const res = await fetch('/api/url-queue');
                const data = await res.json();
                const nowDone = data.summary ? data.summary.done : 0;
                if (nowDone > _lastQueueDone) { _lastQueueDone = nowDone; loadData(); }
                renderUqList(data.items || []);
                if (data.summary && (data.summary.pending > 0 || data.summary.processing > 0)) _startUqPoll();
            } catch(e) {}
        }, 3000);
    </script>
</body>
</html>
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
    return render_template_string(HTML_TEMPLATE)


@app.route("/logo")
def logo():
    """Sirve el logo de la app para el dashboard."""
    from config import LOGO_PATH
    return send_file(LOGO_PATH, mimetype="image/png")


@app.route("/api/transcriptions")
def get_transcriptions():
    """Retorna las últimas 200 transcripciones en formato JSON."""
    return jsonify(_db.get_recent(limit=200))


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
    """Actualiza el texto de una transcripción existente."""
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "text field required"}), 400
    updated = _db.update_text(tid, data["text"])
    if updated == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


def _set_env_key(key: str, value: str):
    """Write key=value to .env and update the running process environment."""
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    set_key(_ENV_PATH, key, value)
    os.environ[key] = value


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
        "insights_backend": os.getenv("INSIGHTS_BACKEND", "groq"),
        "insights_endpoint_model": os.getenv("INSIGHTS_ENDPOINT_MODEL", "qwen/qwen2.5-vl-7b"),
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
        "insights_backend": "INSIGHTS_BACKEND",
        "insights_endpoint_model": "INSIGHTS_ENDPOINT_MODEL",
        "audio_source": "AUDIO_SOURCE",
    }
    for field, env_key in allowed.items():
        if field in data:
            _set_env_key(env_key, str(data[field]).strip())

    # Si se activó el backend local y el modelo está descargado, disparar warmup
    if data.get("transcription_backend") == "local":
        _trigger_local_warmup_if_ready()

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
    """Devuelve el estado, el transcript en vivo y el Insight Stream (para polling)."""
    return jsonify({
        "status": MEETING.status(),
        "segments": MEETING.transcript_segments(),
        "insights": MEETING.get_insights(),
        "last_minutes": MEETING.get_last_minutes(),  # acta de la última reunión terminada
    })


@app.route("/api/meeting/start", methods=["POST"])
def meeting_start():
    """Inicia una reunión (captura dual mic + audio del sistema)."""
    res = MEETING.start()
    code = 200 if res.get("ok") else 500
    return jsonify(res), code


@app.route("/api/meeting/stop", methods=["POST"])
def meeting_stop():
    """Detiene la reunión, persiste el acta y devuelve el transcript final."""
    res = MEETING.stop()
    return jsonify(res)


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
