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
from config import APP_DATA_DIR, MEETINGS_DIR, WEB_STATIC_DIR
from core import dictionary as _dictionary
from core.meeting import MEETING
from core import meeting_export as _meeting_export
from core import insights as _insights
from core import assistant as _assistant

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


app = Flask(__name__, static_folder=WEB_STATIC_DIR, static_url_path="/static")
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
    <!-- Tailwind Play y fuente Inter auto-hospedados (web/static/vendor/) para funcionar sin internet. -->
    <script src="/static/vendor/tailwind.js"></script>
    <style>
        @font-face {
            font-family: 'Inter';
            font-style: normal;
            font-weight: 300 600;
            font-display: swap;
            src: url('/static/vendor/inter-variable.woff2') format('woff2');
        }
        /* ==== Design tokens (U1 rediseño) ==================================== */
        :root {
            --bg: #0a0a0c;
            --panel: rgba(255,255,255,0.04);
            --panel-border: rgba(255,255,255,0.08);
            --panel-hover: rgba(255,255,255,0.06);
            --txt: rgba(255,255,255,0.87);      /* contenido */
            --txt-2: rgba(255,255,255,0.60);    /* secundario */
            --txt-3: rgba(255,255,255,0.42);    /* metadatos */
            --accent: #8b5cf6;                  /* violeta Vflow / canal Yo / fuente mic */
            --accent-soft: rgba(139,92,246,0.16);
            --accent-border: rgba(139,92,246,0.45);
            --cyan: #22d3ee;                    /* canal Ellos / fuente system */
            --cyan-soft: rgba(34,211,238,0.13);
            --amber: #f59e0b;                   /* fuente youtube/url */
            --amber-soft: rgba(245,158,11,0.13);
        }
        body { font-family: 'Inter', system-ui, sans-serif; background: var(--bg); color: var(--txt); font-size: 14px; }
        .glass { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06); }
        /* ==== Sistema de componentes (U1 rediseño) ============================ */
        .btn { display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 14px;
            border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; border: 1px solid transparent;
            transition: background .15s, border-color .15s, color .15s; white-space: nowrap; }
        .btn-primary { background: var(--accent); color: #fff; }
        .btn-primary:hover { background: #7c4fe0; }
        .btn-secondary { background: rgba(255,255,255,0.04); border-color: rgba(255,255,255,0.14); color: var(--txt); }
        .btn-secondary:hover { background: rgba(255,255,255,0.09); border-color: rgba(255,255,255,0.22); }
        .btn-ghost { background: transparent; color: var(--txt-2); }
        .btn-ghost:hover { background: rgba(255,255,255,0.06); color: var(--txt); }
        .btn-danger-ghost { background: transparent; color: rgba(248,113,113,0.8); }
        .btn-danger-ghost:hover { background: rgba(239,68,68,0.12); color: #f87171; }
        .icon-btn { display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px;
            border-radius: 8px; color: var(--txt-2); cursor: pointer; border: none; background: transparent;
            transition: background .15s, color .15s; font-size: 15px; }
        .icon-btn:hover { background: rgba(255,255,255,0.07); color: var(--txt); }
        .card { background: var(--panel); border: 1px solid var(--panel-border); border-radius: 12px; }
        .card-hover:hover { background: var(--panel-hover); }
        .badge { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; font-weight: 500;
            padding: 2px 8px; border-radius: 9999px; white-space: nowrap; }
        .badge-mic { background: var(--accent-soft); color: #c4b5fd; }
        .badge-system { background: var(--cyan-soft); color: #67e8f9; }
        .badge-youtube { background: var(--amber-soft); color: #fcd34d; }
        .metric-card { background: var(--panel); border: 1px solid var(--panel-border); border-radius: 12px;
            padding: 14px 16px; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
        .metric-label { font-size: 12px; color: var(--txt-3); }
        .metric-value { font-size: 22px; font-weight: 600; color: var(--txt); line-height: 1.2; }
        .metric-sub { font-size: 11px; color: var(--txt-3); }
        /* ==== Shell (U2): sidebar + área de contenido ========================= */
        .shell { display: flex; min-height: 100vh; align-items: stretch; }
        #sidebar { width: 232px; flex-shrink: 0; position: sticky; top: 0; height: 100vh;
            display: flex; flex-direction: column; padding: 18px 12px 12px;
            border-right: 1px solid rgba(255,255,255,0.07); background: rgba(255,255,255,0.015); }
        .nav-item { display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: 8px;
            font-size: 13.5px; color: var(--txt-2); cursor: pointer; text-decoration: none;
            transition: background .15s, color .15s; }
        .nav-item:hover { background: rgba(255,255,255,0.05); color: var(--txt); }
        .nav-item.active { background: var(--accent-soft); color: #d6c8fd; }
        #sidebar-status { margin-top: auto; padding: 10px 12px 2px; border-top: 1px solid rgba(255,255,255,0.06);
            font-size: 11.5px; color: var(--txt-3); display: flex; flex-direction: column; gap: 4px; }
        #content { flex: 1; min-width: 0; padding: 22px 28px; }
        .content-inner { max-width: 1400px; margin: 0 auto; }
        @media (max-width: 1100px) {
            #sidebar { width: 62px; padding: 18px 8px 10px; }
            #sidebar .nav-label, #sidebar-status, #sidebar .brand-name { display: none; }
            .nav-item { justify-content: center; padding: 9px; }
            #content { padding: 18px 16px; }
        }
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
            max-width: calc(100vw - 32px);
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
        /* [A11Y-1] Foco global accesible */
        *:focus-visible { outline: 2px solid rgba(140,80,220,.75); outline-offset: 2px; }
        /* [MOTION-1] Respeta prefers-reduced-motion */
        @media (prefers-reduced-motion: reduce){ *,*::before,*::after{ animation-duration:.001ms!important; transition-duration:.001ms!important; } }
        /* Utilidad accesible solo-lector */
        .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
        /* [NET-1/TOAST] Avisos y banner offline */
        #toast-container { position: fixed; top: 16px; right: 16px; display: flex; flex-direction: column; gap: 8px; z-index: 60; }
        .toast { background: rgba(20,20,24,.96); border: 1px solid rgba(255,255,255,.12); color: #eaeaea; padding: 10px 14px; border-radius: 10px; font-size: 13px; box-shadow: 0 8px 24px rgba(0,0,0,.45); animation: toastIn .18s ease; }
        .toast.err { border-color: rgba(239,68,68,.55); }
        .toast.ok { border-color: rgba(34,197,94,.5); }
        @keyframes toastIn { from { opacity: 0; transform: translateX(12px); } to { opacity: 1; transform: none; } }
        #offline-banner { position: fixed; top: 0; left: 0; right: 0; z-index: 70; background: rgba(239,68,68,.95); color: #fff; text-align: center; font-size: 13px; padding: 6px; display: none; }
        #offline-banner.show { display: block; }
        /* [LOAD-1] Skeleton de carga */
        .skel { background: linear-gradient(90deg, rgba(255,255,255,.04), rgba(255,255,255,.1), rgba(255,255,255,.04)); background-size: 200% 100%; animation: skel 1.2s infinite; border-radius: 6px; }
        @keyframes skel { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
        tr.selected-row { background: rgba(140,80,220,0.08); }
        /* Acciones de fila discretas hasta el hover (U3) */
        td.actions-cell { opacity: 0.3; transition: opacity 0.15s; }
        tr:hover td.actions-cell, tr.selected-row td.actions-cell { opacity: 1; }
        /* Command palette Ctrl+K (U4) */
        #palette-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.55); z-index: 300;
            display: flex; align-items: flex-start; justify-content: center; padding-top: 14vh; }
        #palette-box { width: min(620px, calc(100vw - 32px)); background: #16161a;
            border: 1px solid rgba(255,255,255,0.12); border-radius: 12px; overflow: hidden;
            box-shadow: 0 16px 48px rgba(0,0,0,0.6); }
        #palette-input { width: 100%; background: transparent; border: none; outline: none;
            color: var(--txt); font-size: 15px; padding: 14px 16px;
            border-bottom: 1px solid rgba(255,255,255,0.08); }
        #palette-list { max-height: 46vh; overflow-y: auto; padding: 6px; }
        .palette-item { display: flex; align-items: center; gap: 10px; padding: 9px 12px;
            border-radius: 8px; font-size: 13.5px; color: var(--txt-2); cursor: pointer; }
        .palette-item .pal-sub { font-size: 11.5px; color: var(--txt-3); margin-left: auto; white-space: nowrap; }
        .palette-item.sel, .palette-item:hover { background: var(--accent-soft); color: var(--txt); }
        .palette-hint { padding: 8px 14px; font-size: 11px; color: var(--txt-3);
            border-top: 1px solid rgba(255,255,255,0.06); display: flex; gap: 14px; }
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
        /* Iconos SVG inline: tamaño/alineación uniforme, heredan color del texto */
        svg.ic { width: 1em; height: 1em; display: inline-block; vertical-align: -0.125em; flex-shrink: 0; }
        /* Acordeón del panel de Configuración */
        .set-nav { display: flex; align-items: center; flex-wrap: wrap; gap: .4rem; margin-bottom: 1rem; }
        .set-chip { display: inline-flex; align-items: center; gap: .35rem; font-size: 11px; padding: .25rem .65rem; border-radius: 9999px; background: rgba(255,255,255,.05); border: 1px solid rgba(255,255,255,.1); color: rgba(255,255,255,.7); cursor: pointer; }
        .set-chip:hover { background: rgba(255,255,255,.1); color: rgba(255,255,255,.92); }
        .set-chip-x { margin-left: auto; font-size: 11px; padding: .25rem .65rem; border-radius: 9999px; background: transparent; border: 1px solid rgba(255,255,255,.1); color: rgba(255,255,255,.5); cursor: pointer; }
        .set-chip-x:hover { color: rgba(255,255,255,.85); background: rgba(255,255,255,.05); }
        .set-dot { width: .5rem; height: .5rem; border-radius: 9999px; display: inline-block; flex-shrink: 0; }
        .set-sec { border: 1px solid rgba(255,255,255,.07); border-left: 3px solid var(--sec-accent, rgba(140,80,220,.55)); border-radius: .6rem; background: rgba(255,255,255,.02); margin-bottom: .6rem; }
        .set-sec-head { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: .7rem .9rem; background: none; border: none; cursor: pointer; text-align: left; border-radius: .6rem; }
        .set-sec-head:hover { background: rgba(255,255,255,.03); }
        .set-sec-title { display: flex; align-items: center; gap: .5rem; font-size: 13px; font-weight: 500; color: rgba(255,255,255,.82); }
        .set-sec-toggle { font-size: 11px; color: rgba(255,255,255,.45); }
        .set-sec-chev { transition: transform .2s; opacity: .55; }
        .set-sec-body { padding: 0 .9rem .9rem; }
        .set-sec.collapsed .set-sec-body { display: none; }
        .set-sec.collapsed .set-sec-chev { transform: rotate(-90deg); }
    </style>
</head>
<body class="min-h-screen">
    <div class="shell">
    <aside id="sidebar" aria-label="Navegación principal">
        <div class="flex items-center gap-2.5 px-2 mb-6">
            <img src="/logo" class="brand-logo" alt="Vflow">
            <span class="brand-name text-lg font-semibold text-white">Vflow</span>
        </div>
        <nav class="flex flex-col gap-1" id="sidebar-nav" aria-label="Secciones">
            <a href="#/dictados" class="nav-item" data-view="dictados" data-icon="mic"><span class="nav-label">Dictados</span></a>
            <a href="#/reunion" class="nav-item" data-view="reunion" data-icon="chat"><span class="nav-label">Reuniones</span></a>
            <a href="#/diccionario" class="nav-item" data-view="diccionario" data-icon="book"><span class="nav-label">Diccionario</span></a>
            <a href="#/url" class="nav-item" data-view="url" data-icon="play"><span class="nav-label">Desde URL</span></a>
            <a href="#/atajos" class="nav-item" data-view="atajos" data-icon="keyboard"><span class="nav-label">Atajos</span></a>
            <a href="#/ajustes" class="nav-item" data-view="ajustes" data-icon="settings"><span class="nav-label">Ajustes</span></a>
        </nav>
        <div id="sidebar-status" title="Estado del sistema">
            <span id="sb-backend">Backend: —</span>
            <span id="sb-source">Fuente: —</span>
        </div>
    </aside>
    <div id="content">
    <div class="content-inner">
        <!-- Topbar -->
        <header role="banner" class="flex items-center justify-between mb-6 flex-wrap gap-3">
            <div class="flex items-center gap-3">
                <h1 class="text-xl font-semibold text-white" id="view-title">Dictados</h1>
                <span class="text-xs text-white/40 bg-white/5 px-2 py-1 rounded-full dictados-only" id="count-badge">-</span>
            </div>
            <div class="flex items-center gap-2 flex-wrap">
                <input type="text" id="search" placeholder="Buscar en recientes…" title="Filtra solo las transcripciones cargadas"
                    class="dictados-only bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                    placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/20 w-48">
                <div class="dropdown dictados-only" id="cleanup-dropdown">
                    <button onclick="document.getElementById('cleanup-dropdown').classList.toggle('open')"
                        class="btn btn-ghost">
                        Limpiar <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="dropdown-menu">
                        <div class="dropdown-item" onclick="bulkDelete('day','hoy')">Eliminar de hoy</div>
                        <div class="dropdown-item" onclick="bulkDelete('week')">Eliminar última semana</div>
                        <div class="dropdown-item" onclick="bulkDelete('month')">Eliminar último mes</div>
                        <div class="dropdown-item" onclick="event.stopPropagation()" style="display:flex;align-items:center;gap:8px;justify-content:space-between;">
                            <span>Eliminar por fecha</span>
                            <input type="date" id="cleanup-date" onchange="deleteByDate(this.value)" style="color-scheme:dark"
                                class="bg-white/5 border border-white/10 rounded px-2 py-0.5 text-xs text-white/80 focus:outline-none focus:ring-2 focus:ring-purple-500/60">
                        </div>
                        <div class="dropdown-item danger" onclick="bulkDelete('all')">Eliminar todo</div>
                    </div>
                </div>
                <button onclick="loadData()" class="btn btn-ghost dictados-only">Actualizar</button>
                <button onclick="window.open('/reunion','_blank')" class="btn btn-secondary" data-icon="mic" title="Ventana de reunión completa (en vivo + historial)"><span>Ventana reunión</span></button>
            </div>
        </header>

        <main>
        <!-- Shortcuts panel -->
        <div id="shortcuts-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-4">Atajos de teclado</div>
            <div class="grid gap-3" style="grid-template-columns: repeat(auto-fit, minmax(280px, 1fr))">
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">Ctrl</kbd>
                        <span class="text-white/45 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">Alt</kbd>
                        <span class="text-white/45 text-xs ml-1">— mantener</span>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Transcribir (mantenido)</p>
                    <p class="text-xs text-white/55 mt-0.5">Mantén la combinación un instante para que empiece a grabar; suelta para pegar el texto transcrito.</p>
                </div>
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">Shift</kbd>
                        <span class="text-white/45 text-xs">×3 rápido</span>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Transcribir — manos libres</p>
                    <p class="text-xs text-white/55 mt-0.5">Pulsa Shift tres veces en ~400&nbsp;ms para iniciar. Pulsa Shift una vez más para parar y pegar.</p>
                    <p class="text-xs text-white/45 mt-0.5">Shift como parte de un acorde (Shift+A, Shift+Enter) no inicia ni detiene: puedes escribir en otra app mientras dictas.</p>
                </div>
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">Ctrl</kbd>
                        <span class="text-white/45 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">Shift</kbd>
                        <span class="text-white/45 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">Alt</kbd>
                        <span class="text-white/45 text-xs ml-1">— mantener</span>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Traducir (mantenido)</p>
                    <p class="text-xs text-white/45 mt-0.5">(con backend local: solo →inglés)</p>
                    <p class="text-xs text-white/55 mt-0.5">Presiona Shift antes de Alt, mantén la combinación un instante para grabar; suelta para pegar la traducción al idioma destino.</p>
                </div>
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">AltGr</kbd>
                        <span class="text-white/45 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">T</kbd>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Traducir — manos libres (toggle)</p>
                    <p class="text-xs text-white/55 mt-0.5">Primera pulsación inicia la grabación; segunda pulsación la detiene y pega la traducción.</p>
                    <p class="text-xs text-white/45 mt-0.5">(con backend local: solo →inglés)</p>
                </div>
                <div class="p-3 rounded-lg bg-white/[0.03] border border-white/[0.06]">
                    <div class="flex items-center gap-2 mb-1.5">
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">AltGr</kbd>
                        <span class="text-white/45 text-xs">+</span>
                        <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/45 bg-white/[0.07] text-white/80">R</kbd>
                    </div>
                    <p class="text-xs text-white/70 font-medium">Reunión — captura dual (toggle)</p>
                    <p class="text-xs text-white/55 mt-0.5">Inicia/termina una reunión capturando tu micrófono («Yo») y el audio del sistema («Ellos») a la vez. El transcript en vivo aparece en el panel 🎙.</p>
                </div>
            </div>
            <p class="text-xs text-white/45 mt-4">El idioma de transcripción y el idioma de destino (traducción) se configuran en el panel de Configuración.</p>
        </div>

        <!-- URL Queue panel -->
        <div id="url-queue-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-1">Transcribir desde URL</div>
            <p class="text-xs text-white/45 mb-3">Pega una o varias URLs de YouTube, TikTok u otras plataformas (una por línea) para transcribirlas en cola.</p>

            <!-- URL única -->
            <div class="flex gap-2 mb-2">
                <input type="url" id="uq-url" placeholder="https://www.youtube.com/watch?v=..."
                    class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                    placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/20 flex-1">
                <button onclick="enqueueUrls()"
                    class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 whitespace-nowrap" id="uq-btn">
                    Añadir a la cola
                </button>
            </div>

            <!-- Bulk textarea -->
            <textarea id="uq-bulk" rows="3" placeholder="Una URL por línea para añadir varias a la vez…"
                class="bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white/80
                placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/20 w-full resize-y mb-2"></textarea>

            <!-- Opciones -->
            <div class="flex items-center gap-4 mb-2 flex-wrap">
                <label class="flex items-center gap-2 text-xs text-white/45 cursor-pointer select-none">
                    <input type="checkbox" id="uq-instagram" class="accent-purple-500">
                    Instagram (experimental, requiere sesión en el navegador)
                </label>
                <button onclick="syncInstagramCookies()" id="uq-ig-sync"
                    class="text-xs px-2 py-1 rounded bg-purple-600/20 text-purple-300 hover:bg-purple-600/40"
                    title="Guarda tus cookies de Instagram cifradas (DPAPI) para usarlas aunque cierres el navegador">
                    Sincronizar cookies de Instagram</button>
            </div>
            <p class="text-xs text-white/45 mb-3">La cola usa el backend de transcripción actual. Para que sea gratis, usa el backend local (Configuración). Las cookies de Instagram se guardan cifradas con DPAPI; inicia sesión en Instagram (Opera recomendado) antes de sincronizar.</p>

            <div id="uq-ig-feedback" class="text-xs mb-2 hidden" aria-live="polite"></div>
            <div id="uq-feedback" class="text-xs mb-3 hidden" aria-live="assertive"></div>

            <!-- Lista de progreso -->
            <div class="flex items-center justify-between mb-2">
                <span class="text-xs text-white/45">Cola de transcripción</span>
                <div class="flex gap-2">
                    <button onclick="cancelPendingUrls()" class="text-xs px-2 py-1 rounded text-white/55 hover:text-white/60 hover:bg-white/5">Cancelar pendientes</button>
                    <button onclick="clearQueueFinished()" class="text-xs px-2 py-1 rounded text-white/55 hover:text-white/60 hover:bg-white/5">Limpiar terminadas</button>
                </div>
            </div>
            <div id="uq-list" class="space-y-1 max-h-64 overflow-y-auto">
                <div class="text-xs text-white/45">Sin items en la cola.</div>
            </div>
        </div>

        <!-- Meeting panel (reunión en vivo) -->
        <div id="meeting-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-1">Reunión en vivo</div>
            <p class="text-xs text-white/45 mb-3">Captura tu micrófono («Yo») y el audio del sistema («Ellos») a la vez y transcribe en vivo. Inicia/termina también con <kbd class="px-1.5 py-0.5 text-[10px] font-mono rounded border border-white/45 bg-white/[0.07]">AltGr</kbd>+<kbd class="px-1.5 py-0.5 text-[10px] font-mono rounded border border-white/45 bg-white/[0.07]">R</kbd> o desde la bandeja. Para mejor diarización usa auriculares.</p>

            <div class="flex items-center gap-3 mb-3">
                <button onclick="startMeeting()" id="mt-start" data-icon="play"
                    class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 whitespace-nowrap">
                    Iniciar reunión
                </button>
                <button onclick="stopMeeting()" id="mt-stop" data-icon="stop"
                    class="text-xs px-3 py-1.5 rounded bg-red-600/30 text-red-300 hover:bg-red-600/50 whitespace-nowrap hidden">
                    Terminar reunión
                </button>
                <span id="mt-status" class="text-xs text-white/45"></span>
            </div>

            <div id="mt-feedback" class="text-xs mb-2 hidden" aria-live="polite"></div>

            <div class="grid grid-cols-1 md:grid-cols-[1.4fr_1fr] gap-3">
                <!-- Transcript en vivo -->
                <div>
                    <div class="flex items-center justify-between mb-1.5">
                        <span class="text-xs text-white/45">Transcript en vivo</span>
                        <button onclick="copyEl('mt-transcript', this)" class="text-[10px] text-white/30 hover:text-white/60 px-1.5 py-0.5 rounded hover:bg-white/5 transition-colors" title="Copiar transcript">Copiar</button>
                    </div>
                    <div id="mt-transcript" class="space-y-1.5 max-h-96 overflow-y-auto rounded-lg bg-white/[0.02] border border-white/[0.06] p-3">
                        <div class="text-xs text-white/45">El transcript en vivo aparecerá aquí cuando inicies una reunión.</div>
                    </div>
                </div>
                <!-- Insight Stream (temas / pendientes / propuestas) -->
                <div>
                    <div class="flex items-center justify-between mb-1.5">
                        <span class="text-xs text-white/45">Análisis en vivo</span>
                        <div class="flex gap-1 text-[10px]">
                            <button id="mt-view-foco" onclick="setMeetingView('foco')" class="px-2 py-0.5 rounded text-white/55 hover:text-white/70" title="Solo lo accionable (menos distracción en reunión)">Foco</button>
                            <button id="mt-view-revision" onclick="setMeetingView('revision')" class="px-2 py-0.5 rounded bg-purple-600/40 text-purple-200" title="Panel completo: temas, pendientes y propuestas">Revisión</button>
                        </div>
                    </div>
                    <div id="mt-insights" class="space-y-3 max-h-96 overflow-y-auto rounded-lg bg-white/[0.02] border border-white/[0.06] p-3">
                        <div class="text-xs text-white/45">Temas, pendientes y propuestas aparecerán aquí a medida que avance la reunión.</div>
                    </div>
                </div>
            </div>

            <!-- Acta post-reunión (se rellena al terminar) -->
            <div id="mt-minutes" class="mt-3 rounded-lg bg-white/[0.02] border border-white/[0.06] p-3 hidden">
                <div class="flex items-center justify-between mb-2">
                    <span class="text-xs font-medium text-emerald-300/80">Acta de la reunión</span>
                    <button onclick="copyEl('mt-minutes-body', this)" class="text-[10px] text-white/30 hover:text-white/60 px-1.5 py-0.5 rounded hover:bg-white/5 transition-colors" title="Copiar acta">Copiar</button>
                </div>
                <div id="mt-minutes-body" class="space-y-2 text-sm text-white/80"></div>
            </div>
        </div>

        <!-- Settings panel -->
        <div id="settings-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="text-sm font-medium text-white/60 mb-4">Configuración</div>

            <!-- Navegación de secciones -->
            <div class="set-nav">
                <span class="text-xs text-white/45 mr-1">Ir a:</span>
                <button type="button" class="set-chip" onclick="setNavGo('transcripcion')"><span class="set-dot" style="background:rgba(140,80,220,.85)"></span>Transcripción</button>
                <button type="button" class="set-chip" onclick="setNavGo('sonidos')"><span class="set-dot" style="background:rgba(56,189,248,.85)"></span>Sonidos</button>
                <button type="button" class="set-chip" onclick="setNavGo('historial')"><span class="set-dot" style="background:rgba(52,211,153,.85)"></span>Historial</button>
                <button type="button" class="set-chip" onclick="setNavGo('reuniones')"><span class="set-dot" style="background:rgba(167,139,250,.85)"></span>Reuniones</button>
                <button type="button" class="set-chip" onclick="setNavGo('apikeys')"><span class="set-dot" style="background:rgba(251,191,36,.85)"></span>API Keys</button>
                <button type="button" id="set-collapse-all" class="set-chip-x" onclick="setCollapseAll()">Comprimir todo</button>
            </div>

            <!-- Sección: Transcripción -->
            <div class="set-sec" id="sec-transcripcion" style="--sec-accent:rgba(140,80,220,.6)">
                <button type="button" class="set-sec-head" onclick="toggleSetSec('transcripcion')" aria-expanded="true" aria-controls="sec-transcripcion-body">
                    <span class="set-sec-title"><svg class="set-sec-chev ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span class="set-dot" style="background:rgba(140,80,220,.85)"></span>Transcripción</span>
                    <span class="set-sec-toggle"><span class="set-sec-toggle-label">Ocultar</span></span>
                </button>
                <div id="sec-transcripcion-body" class="set-sec-body">
                    <div class="grid gap-4" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr))">
                        <div>
                            <label for="cfg-language" class="text-xs text-white/55 block mb-1">Idioma de transcripción</label>
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
                            <label for="cfg-microphone" class="text-xs text-white/55 block mb-1">Micrófono</label>
                            <select id="cfg-microphone" class="cfg-select">
                                <option value="">Sistema por defecto</option>
                            </select>
                        </div>
                        <div>
                            <label for="cfg-translate-target" class="text-xs text-white/55 block mb-1">Idioma de salida (traducción)</label>
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
                            <label for="cfg-audio-source" class="text-xs text-white/55 block mb-1">Fuente de audio</label>
                            <select id="cfg-audio-source" class="cfg-select">
                                <option value="mic">Micrófono</option>
                                <option value="system">Audio del sistema (loopback)</option>
                            </select>
                        </div>
                        <div>
                            <label for="cfg-backend" class="text-xs text-white/55 block mb-1">Backend de transcripción</label>
                            <select id="cfg-backend" class="cfg-select" onchange="onBackendChange()">
                                <option value="groq">Groq API (nube)</option>
                                <option value="local">Local sin internet</option>
                            </select>
                        </div>
                        <div>
                            <label for="cfg-local-model" class="text-xs text-white/55 block mb-1">Modelo local</label>
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
                                class="text-xs px-3 p-2.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 disabled:opacity-40 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-purple-500/60">
                                Descargar modelo
                            </button>
                        </div>
                        <div id="local-download-progress-wrap" class="hidden">
                            <div class="w-full bg-white/10 rounded-full h-1.5 mt-1">
                                <div id="local-download-bar" class="bg-purple-500 h-1.5 rounded-full transition-all" style="width:0%"></div>
                            </div>
                            <span class="text-xs text-white/30 mt-1 block" id="local-download-pct">0%</span>
                        </div>
                        <div id="local-translation-note" class="mt-2 rounded-lg px-3 py-2 text-xs flex items-start gap-2 hidden">
                            <span class="flex-shrink-0 mt-px">ℹ️</span>
                            <span id="local-translation-note-text"></span>
                        </div>
                        <div class="flex items-start gap-2 mt-3 pt-3 border-t border-white/[0.06]">
                            <label class="toggle-switch mt-0.5">
                                <input type="checkbox" id="cfg-groq-fallback" onchange="updateLocalTranslationNote()">
                                <span class="toggle-slider"></span>
                            </label>
                            <div>
                                <span class="text-xs text-white/50">Permitir Groq como respaldo si el modo local falla</span>
                                <p class="text-xs text-white/55 mt-0.5">Si se activa, el audio se enviará a Groq cuando el modo local falle.</p>
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Sección: Sonidos -->
            <div class="set-sec collapsed" id="sec-sonidos" style="--sec-accent:rgba(56,189,248,.6)">
                <button type="button" class="set-sec-head" onclick="toggleSetSec('sonidos')" aria-expanded="false" aria-controls="sec-sonidos-body">
                    <span class="set-sec-title"><svg class="set-sec-chev ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span class="set-dot" style="background:rgba(56,189,248,.85)"></span>Sonidos</span>
                    <span class="set-sec-toggle"><span class="set-sec-toggle-label">Mostrar</span></span>
                </button>
                <div id="sec-sonidos-body" class="set-sec-body">
                    <div class="flex items-center gap-2">
                        <label class="toggle-switch">
                            <input type="checkbox" id="cfg-sounds">
                            <span class="toggle-slider"></span>
                        </label>
                        <span class="text-xs text-white/55">Beep al iniciar/terminar</span>
                    </div>
                    <div class="flex items-center gap-2 mt-2">
                        <input type="range" id="cfg-beep-volume" min="1" max="10" step="1"
                               class="accent-purple-500" style="width:90px"
                               oninput="document.getElementById('cfg-beep-volume-label').textContent=this.value">
                        <label for="cfg-beep-volume" class="text-xs text-white/55">Volumen: <span id="cfg-beep-volume-label">2</span></label>
                    </div>
                </div>
            </div>

            <!-- Sección: Historial -->
            <div class="set-sec collapsed" id="sec-historial" style="--sec-accent:rgba(52,211,153,.6)">
                <button type="button" class="set-sec-head" onclick="toggleSetSec('historial')" aria-expanded="false" aria-controls="sec-historial-body">
                    <span class="set-sec-title"><svg class="set-sec-chev ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span class="set-dot" style="background:rgba(52,211,153,.85)"></span>Historial</span>
                    <span class="set-sec-toggle"><span class="set-sec-toggle-label">Mostrar</span></span>
                </button>
                <div id="sec-historial-body" class="set-sec-body">
                    <div class="grid gap-4" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr))">
                        <div>
                            <label for="cfg-save-history" class="text-xs text-white/55 block mb-1">Guardar historial</label>
                            <div class="flex items-center gap-2" style="height:32px">
                                <label class="toggle-switch">
                                    <input type="checkbox" id="cfg-save-history">
                                    <span class="toggle-slider"></span>
                                </label>
                                <span class="text-xs text-white/55">Guardar transcripciones</span>
                            </div>
                        </div>
                        <div>
                            <label for="cfg-retention-days" class="text-xs text-white/55 block mb-1">Eliminar transcripciones después de (días)</label>
                            <select id="cfg-retention-days" class="cfg-select">
                                <option value="0">Nunca</option>
                                <option value="7">7 días</option>
                                <option value="30">30 días</option>
                                <option value="90">90 días</option>
                            </select>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Sección: Reuniones -->
            <div class="set-sec collapsed" id="sec-reuniones" style="--sec-accent:rgba(167,139,250,.6)">
                <button type="button" class="set-sec-head" onclick="toggleSetSec('reuniones')" aria-expanded="false" aria-controls="sec-reuniones-body">
                    <span class="set-sec-title"><svg class="set-sec-chev ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span class="set-dot" style="background:rgba(167,139,250,.85)"></span>Reuniones (análisis y acta)</span>
                    <span class="set-sec-toggle"><span class="set-sec-toggle-label">Mostrar</span></span>
                </button>
                <div id="sec-reuniones-body" class="set-sec-body">
                    <p class="text-xs text-white/55 mb-3">El análisis en vivo necesita velocidad (Groq recomendado); acta y Asistente de reuniones admiten modelos más potentes (OpenRouter, contexto 1 M).</p>
                    <div class="flex flex-wrap items-center gap-2 mb-3">
                        <button type="button" id="cfg-insights-preset-sub" onclick="applyInsightsPreset('subscription')"
                            class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 focus:outline-none focus:ring-2 focus:ring-purple-500/60 whitespace-nowrap">⭐ Usar mi suscripción</button>
                        <button type="button" id="cfg-insights-preset-api" onclick="applyInsightsPreset('apis')"
                            class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 focus:outline-none focus:ring-2 focus:ring-purple-500/60 whitespace-nowrap">🌐 Usar APIs (benchmark)</button>
                    </div>
                    <div class="mb-3">
                        <label class="flex items-center gap-2" style="height:32px">
                            <label class="toggle-switch">
                                <input type="checkbox" id="cfg-insights-fallback">
                                <span class="toggle-slider"></span>
                            </label>
                            <span class="text-xs text-white/55">Fallback automático a APIs si el backend activo falla (recomendado)</span>
                        </label>
                    </div>
                    <div class="flex flex-col gap-3">
                        <div>
                            <label for="cfg-insights-backend-live" class="text-xs text-white/55 block mb-1">Análisis en vivo</label>
                            <select id="cfg-insights-backend-live" class="cfg-select" onchange="onInsightsBackendChange()">
                                <option value="groq">Groq API (nube) — instantáneo, requiere internet</option>
                                <option value="openrouter">OpenRouter (nube) — multi-modelo, requiere internet</option>
                                <option value="endpoint">Local (LM Studio) — privado, sin internet</option>
                                <option value="anthropic">Anthropic (API oficial) — Claude Haiku, requiere internet</option>
                                <option value="claude-cli">Claude (tu suscripción) — vía Claude Code, consume tu cuota</option>
                            </select>
                        </div>
                        <div>
                            <label for="cfg-insights-backend-batch" class="text-xs text-white/55 block mb-1">Acta + Asistente de reuniones</label>
                            <select id="cfg-insights-backend-batch" class="cfg-select" onchange="onInsightsBackendChange()">
                                <option value="groq">Groq API (nube) — instantáneo, requiere internet</option>
                                <option value="openrouter">OpenRouter (nube) — multi-modelo, requiere internet</option>
                                <option value="endpoint">Local (LM Studio) — privado, sin internet</option>
                                <option value="anthropic">Anthropic (API oficial) — Claude Sonnet, requiere internet</option>
                                <option value="claude-cli">Claude (tu suscripción) — vía Claude Code, consume tu cuota</option>
                            </select>
                        </div>
                    </div>
                    <div id="cfg-insights-endpoint-wrap" class="mt-2 p-3 rounded-lg bg-white/[0.02] border border-white/[0.06] hidden">
                        <label for="cfg-insights-model" class="text-xs text-white/55 block mb-1">Modelo local (id en LM Studio)</label>
                        <input type="text" id="cfg-insights-model" placeholder="qwen/qwen2.5-vl-7b"
                            class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80 placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/45 w-full">
                        <p class="text-xs text-white/55 mt-1">Requiere LM Studio abierto con el servidor local activo (localhost:1234). Probados: <span class="text-white/55">qwen/qwen2.5-vl-7b</span> (calidad, ~40s) · <span class="text-white/55">llama-3.2-3b-instruct</span> (rápido, ~15s). Si LM Studio está cerrado, la reunión sigue transcribiendo pero sin análisis.</p>
                    </div>
                    <div id="cfg-insights-openrouter-wrap" class="mt-2 p-3 rounded-lg bg-white/[0.02] border border-white/[0.06] hidden">
                        <p class="text-xs text-white/55">Pon tu key abajo en <strong>API Keys</strong> (se guarda cifrada en este equipo). Consíguela en <span class="text-white/70">openrouter.ai/keys</span>. Modelo configurable con <code class="text-white/70">OPENROUTER_MODEL</code> (default: <span class="text-white/70">google/gemini-3-flash</span>).</p>
                    </div>
                    <div id="cfg-insights-anthropic-wrap" class="mt-2 p-3 rounded-lg bg-white/[0.02] border border-white/[0.06] hidden">
                        <p class="text-xs text-white/55">Pon tu key abajo en <strong>API Keys</strong> (se guarda cifrada en este equipo). Consíguela en <span class="text-white/70">console.anthropic.com/settings/keys</span>. Modelos: <code class="text-white/70">ANTHROPIC_MODEL_LIVE</code> (default Haiku) y <code class="text-white/70">ANTHROPIC_MODEL_BATCH</code> (default Sonnet).</p>
                    </div>
                    <div id="cfg-insights-claudecli-wrap" class="mt-2 p-3 rounded-lg bg-white/[0.02] border border-white/[0.06] hidden">
                        <p class="text-xs text-white/55">Usa tu suscripción de Claude vía Claude Code (requiere <code class="text-white/70">claude</code> instalado y logueado, sin API key). Sirve para acta, Asistente y análisis en vivo. <strong class="text-white/70">Consume la cuota de tu plan</strong> (Pro/Max): una reunión larga con análisis en vivo puede gastar decenas de mensajes. Modelos: <code class="text-white/70">CLAUDE_CLI_MODEL_LIVE</code> (haiku) y <code class="text-white/70">CLAUDE_CLI_MODEL_BATCH</code> (sonnet). Claude Code guarda transcripts locales propios.</p>
                    </div>
                </div>
            </div>

            <!-- Sección: API Keys -->
            <div class="set-sec collapsed" id="sec-apikeys" style="--sec-accent:rgba(251,191,36,.6)">
                <button type="button" class="set-sec-head" onclick="toggleSetSec('apikeys')" aria-expanded="false" aria-controls="sec-apikeys-body">
                    <span class="set-sec-title"><svg class="set-sec-chev ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span class="set-dot" style="background:rgba(251,191,36,.85)"></span>API Keys</span>
                    <span class="set-sec-toggle"><span class="set-sec-toggle-label">Mostrar</span></span>
                </button>
                <div id="sec-apikeys-body" class="set-sec-body space-y-3">
                    <p class="text-xs text-white/55">Se guardan <strong>cifradas (DPAPI)</strong> solo en este equipo y no se incluyen al empaquetar. El modelo local no necesita llave.</p>
                    <div>
                        <label for="cfg-groq-key" class="text-xs text-white/55 block mb-1">Groq API Key <span id="groq-key-status" class="ml-1 text-xs text-white/45"></span></label>
                        <div class="flex gap-2">
                            <input type="password" id="cfg-groq-key" placeholder="gsk_…" autocomplete="off"
                                class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80 placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/45 flex-1">
                            <button type="button" onclick="saveApiKey('groq')" class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 focus:outline-none focus:ring-2 focus:ring-purple-500/60 whitespace-nowrap">Guardar</button>
                        </div>
                    </div>
                    <div>
                        <label for="cfg-openrouter-key" class="text-xs text-white/55 block mb-1">OpenRouter API Key <span id="openrouter-key-status" class="ml-1 text-xs text-white/45"></span></label>
                        <div class="flex gap-2">
                            <input type="password" id="cfg-openrouter-key" placeholder="sk-or-…" autocomplete="off"
                                class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80 placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/45 flex-1">
                            <button type="button" onclick="saveApiKey('openrouter')" class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 focus:outline-none focus:ring-2 focus:ring-purple-500/60 whitespace-nowrap">Guardar</button>
                        </div>
                    </div>
                    <div>
                        <label for="cfg-anthropic-key" class="text-xs text-white/55 block mb-1">Anthropic API Key <span id="anthropic-key-status" class="ml-1 text-xs text-white/45"></span></label>
                        <div class="flex gap-2">
                            <input type="password" id="cfg-anthropic-key" placeholder="sk-ant-…" autocomplete="off"
                                class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80 placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/45 flex-1">
                            <button type="button" onclick="saveApiKey('anthropic')" class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 focus:outline-none focus:ring-2 focus:ring-purple-500/60 whitespace-nowrap">Guardar</button>
                        </div>
                    </div>
                    <div id="api-keys-feedback" class="text-xs hidden" aria-live="polite"></div>
                </div>
            </div>

            <div class="flex justify-end items-center mt-4 gap-3">
                <span id="cfg-saved" role="status" aria-live="polite" class="text-xs text-green-400" style="opacity:0;transition:opacity 0.3s">Guardado ✓</span>
                <button onclick="saveSettings()" class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 focus:outline-none focus:ring-2 focus:ring-purple-500/60">Guardar</button>
            </div>
        </div>

        <!-- Dictionary panel -->
        <div id="dictionary-panel" class="glass rounded-xl p-5 mb-6 hidden">
            <div class="flex items-center justify-between mb-1">
                <div class="text-sm font-medium text-white/60">Diccionario personal</div>
                <div class="flex items-center gap-2">
                    <a href="/api/dictionary/export" data-icon="download" class="text-xs px-2 py-1 rounded bg-white/5 text-white/40 hover:text-white/70 hover:bg-white/10" title="Exportar CSV">CSV</a>
                    <label data-icon="upload" class="text-xs px-2 py-1 rounded bg-white/5 text-white/40 hover:text-white/70 hover:bg-white/10 cursor-pointer" title="Importar CSV">CSV
                        <input type="file" accept=".csv,text/csv" class="hidden" onchange="importDictCSV(this)">
                    </label>
                </div>
            </div>
            <p class="text-xs text-white/55 mb-2">Las palabras se usan para que el modelo las reconozca; los pares corrigen la transcripción (ej. Johan → Johann).</p>
            <p class="text-xs text-white/55 mb-2">Se corrige así: cuando escuche «X» se escribe «Y». Rellena «Cuando escuche…» con lo mal reconocido y «Palabra» con la forma correcta.</p>
            <!-- Budget bar -->
            <div class="mb-3">
                <div class="text-xs text-white/30" id="dict-budget-label">Vocabulario en prompt: — de —</div>
                <div class="dict-budget-bar mt-1"><div class="dict-budget-bar-fill" id="dict-budget-fill" style="width:0%"></div></div>
            </div>
            <!-- Search -->
            <label for="dict-search" class="sr-only">Buscar en el diccionario</label>
            <input type="text" id="dict-search" placeholder="Buscar en el diccionario…" aria-label="Buscar en el diccionario"
                class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/45 w-full mb-3"
                oninput="filterDictList()">
            <form onsubmit="addDictEntry(event)" class="flex flex-wrap gap-2 mb-4 items-end">
                <div>
                    <label for="dict-replace-to" class="text-xs text-white/55 block mb-1">Palabra / forma canónica <span class="text-red-400">*</span></label>
                    <input type="text" id="dict-replace-to" placeholder="Johann"
                        class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                        placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/45 w-40" required>
                </div>
                <div>
                    <label for="dict-replace-from" class="text-xs text-white/55 block mb-1">Cuando escuche… (opcional)</label>
                    <input type="text" id="dict-replace-from" placeholder="Johan"
                        class="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white/80
                        placeholder-white/45 focus:outline-none focus:ring-2 focus:ring-purple-500/60 focus:border-white/45 w-40">
                </div>
                <button type="submit"
                    class="text-xs px-3 py-1.5 rounded bg-purple-600/30 text-purple-300 hover:bg-purple-600/50 focus:outline-none focus:ring-2 focus:ring-purple-500/60">
                    Añadir
                </button>
            </form>
            <div id="dict-import-result" role="status" aria-live="assertive" class="text-xs mb-2 hidden"></div>
            <div id="dict-list" class="space-y-1">
                <div class="text-xs text-white/20">Cargando...</div>
            </div>
        </div>

        <!-- Vista Dictados (home) -->
        <div id="dictados-view">
        <div class="grid gap-3 mb-4" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr))" id="stats-row">
            <div class="metric-card">
                <span class="metric-label">Dictados hoy</span>
                <span class="metric-value" id="st-today">–</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Minutos esta semana</span>
                <span class="metric-value" id="st-week">–</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Transcripciones totales</span>
                <span class="metric-value" id="st-total">–</span>
            </div>
            <div class="metric-card">
                <span class="metric-label">Términos del diccionario</span>
                <span class="metric-value" id="st-dict">–</span>
                <span class="metric-sub">activos</span>
            </div>
        </div>
        <div class="glass rounded-xl overflow-hidden">
            <div class="overflow-x-auto">
            <table class="w-full">
                <caption class="sr-only">Historial de transcripciones</caption>
                <thead>
                    <tr class="text-white/45 text-xs uppercase tracking-wider border-b border-white/5">
                        <th class="py-3 px-2 text-center w-10">
                            <input type="checkbox" id="select-all" onclick="toggleSelectAll(this)" class="accent-purple-500 cursor-pointer">
                        </th>
                        <th class="py-3 px-4 text-left w-36">Hora</th>
                        <th class="py-3 px-4 text-left">Transcripción</th>
                        <th class="py-3 px-4 text-right w-20">Dur.</th>
                        <th class="py-3 px-4 text-center w-32"></th>
                    </tr>
                </thead>
                <tbody id="tbody"></tbody>
            </table>
            </div>
            <div id="empty" class="hidden text-center py-16 px-6">
                <div class="text-white/25 mb-3" style="font-size: 28px;" data-icon="mic"></div>
                <p class="text-white/80 text-base font-medium mb-1">Graba tu primer dictado</p>
                <p class="text-white/50 text-sm mb-4">Mantén <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/30 bg-white/[0.07]">Ctrl</kbd> + <kbd class="px-2 py-0.5 text-xs font-mono rounded border border-white/30 bg-white/[0.07]">Alt</kbd> mientras hablas y suelta para pegar donde esté el cursor.</p>
                <p class="text-white/40 text-xs">Reuniones: <kbd class="px-1.5 py-0.5 text-xs font-mono rounded border border-white/25 bg-white/[0.05]">AltGr</kbd>+<kbd class="px-1.5 py-0.5 text-xs font-mono rounded border border-white/25 bg-white/[0.05]">R</kbd> · Todos los atajos en la sección <a href="#/atajos" class="text-purple-300/80 hover:text-purple-200 underline">Atajos</a></p>
            </div>
        </div>

        <!-- Footer -->
        <div class="mt-4 text-center text-white/45 text-xs">
            Vflow &middot; Ctrl+Alt para grabar &middot; AltGr+R reunión
        </div>
        </div><!-- /dictados-view -->
        </main>
    </div><!-- /content-inner -->
    </div><!-- /content -->
    </div><!-- /shell -->

    <!-- Add to dictionary floating button (appears on text selection in transcription table) -->
    <button class="dict-add-from-history" id="dict-from-history-btn" onclick="addSelectedTextToDict()">
        📖 Añadir al diccionario
    </button>

    <!-- Command palette (Ctrl+K) -->
    <div id="palette-overlay" class="hidden" role="dialog" aria-label="Paleta de comandos">
        <div id="palette-box">
            <input id="palette-input" type="text" placeholder="Buscar en todo el historial o saltar a una sección…"
                autocomplete="off" spellcheck="false">
            <div id="palette-list"></div>
            <div class="palette-hint">
                <span>↑↓ navegar</span><span>Enter seleccionar</span><span>Esc cerrar</span>
                <span style="margin-left:auto">Las transcripciones se copian al portapapeles</span>
            </div>
        </div>
    </div>

    <!-- Network / toast containers -->
    <div id="offline-banner">Sin conexión con el servidor — reintentando…</div>
    <div id="toast-container" aria-live="polite"></div>

    <!-- Selection bar -->
    <div class="selection-bar" id="selection-bar">
        <span class="count" id="sel-count">0 seleccionados</span>
        <button class="del-btn" onclick="deleteSelected()">Eliminar seleccionados</button>
        <button class="cancel-btn" onclick="clearSelection()">Cancelar</button>
    </div>

    <script>
        // --- Iconos SVG inline (sin dependencia de fuentes; idénticos en Win/Mac/Linux, sin tofu) ---
        function _ic(p){return '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'+p+'</svg>';}
        const ICONS = {
            settings: _ic('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
            book: _ic('<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>'),
            keyboard: _ic('<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M6 9h.01M10 9h.01M14 9h.01M18 9h.01M7 13h10"/>'),
            play: _ic('<polygon points="6 4 20 12 6 20 6 4" fill="currentColor" stroke="none"/>'),
            mic: _ic('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>'),
            chat: _ic('<path d="M21 11.5a8.4 8.4 0 0 1-8.5 8.5 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5z"/>'),
            chevronDown: _ic('<polyline points="6 9 12 15 18 9"/>'),
            arrow: _ic('<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>'),
            download: _ic('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'),
            upload: _ic('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>'),
            stop: _ic('<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>'),
            pencil: _ic('<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
            close: _ic('<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>'),
            star: _ic('<polygon points="12 2 15.1 8.3 22 9.3 17 14.1 18.2 21 12 17.8 5.8 21 7 14.1 2 9.3 8.9 8.3 12 2"/>'),
            starOn: _ic('<polygon points="12 2 15.1 8.3 22 9.3 17 14.1 18.2 21 12 17.8 5.8 21 7 14.1 2 9.3 8.9 8.3 12 2" fill="currentColor"/>'),
            copy: _ic('<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
            music: _ic('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>'),
            camera: _ic('<rect x="2" y="2" width="20" height="20" rx="5"/><circle cx="12" cy="12" r="4"/><line x1="17.5" y1="6.5" x2="17.51" y2="6.5"/>'),
            link: _ic('<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>'),
            bulb: _ic('<path d="M9 18h6M10 22h4M15 14c.2-1 .7-1.7 1.4-2.5A4.6 4.6 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.8.8 1.2 1.5 1.4 2.5"/>'),
            calendar: _ic('<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>'),
            speaker: _ic('<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor" stroke="none"/><path d="M19 5a10 10 0 0 1 0 14M15.5 8.5a5 5 0 0 1 0 7"/>'),
            youtube: _ic('<rect x="2" y="5" width="20" height="14" rx="4"/><polygon points="10 9 16 12 10 15 10 9" fill="currentColor" stroke="none"/>'),
        };
        // Rellena los iconos de los elementos estáticos con data-icon="nombre".
        function _fillIcons(root){(root||document).querySelectorAll('[data-icon]').forEach(el=>{const i=ICONS[el.dataset.icon];if(i&&!el.querySelector('svg.ic')){const hasText=el.textContent.trim().length>0;el.insertAdjacentHTML('afterbegin', i+(hasText?' ':''));}});}

        let allData = [];
        let renderedData = [];
        let editingId = null;
        let selectedIds = new Set();
        let expandedIds = new Set();
        let anchorIndex = null;

        function toast(msg,type){let c=document.getElementById('toast-container');if(!c)return;const t=document.createElement('div');t.className='toast'+(type==='err'?' err':type==='ok'?' ok':'');t.textContent=msg;c.appendChild(t);setTimeout(()=>t.remove(),4000);}
        function showOffline(on){const b=document.getElementById('offline-banner');if(b)b.classList.toggle('show',!!on);}

        async function loadData() {
            try {
                const res = await fetch('/api/transcriptions');
                if (!res.ok) throw new Error('HTTP ' + res.status);
                allData = await res.json();
                // Remove selected IDs that no longer exist
                const existingIds = new Set(allData.map(t => t.id));
                selectedIds = new Set([...selectedIds].filter(id => existingIds.has(id)));
                renderTable(allData);
                showOffline(false);
                loadStats();  // agregados baratos (indexados); mismo ciclo de refresh
            } catch (err) {
                // No relanzar: el setInterval debe seguir vivo.
                showOffline(true);
            }
        }

        async function loadStats() {
            try {
                const s = await fetch('/api/stats').then(r => r.json());
                document.getElementById('st-today').textContent = s.today_count;
                document.getElementById('st-week').textContent = Math.round((s.week_seconds || 0) / 60);
                document.getElementById('st-total').textContent = s.total_count;
                document.getElementById('st-dict').textContent = s.dict_active;
            } catch (e) { /* informativo: no romper el flujo del historial */ }
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
                // Badge de fuente con los valores REALES de la DB: mic (default) / system / youtube
                const srcBadge = t.source === 'youtube'
                    ? '<span class="badge badge-youtube" title="Transcrito desde URL">'+ICONS.youtube+' URL</span>'
                    : t.source === 'system'
                        ? '<span class="badge badge-system" title="Audio del sistema">'+ICONS.speaker+' Sistema</span>'
                        : '<span class="badge badge-mic" title="Micrófono">'+ICONS.mic+' Mic</span>';
                const isEditing = editingId === t.id;
                const checked = selectedIds.has(t.id) ? 'checked' : '';
                const rowClass = selectedIds.has(t.id) ? 'selected-row' : '';
                const textCell = isEditing
                    ? `<textarea class="edit-area" id="edit-${t.id}" onkeydown="if(event.key==='Escape'){event.preventDefault();cancelEdit()}else if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();saveEdit(${t.id})}">${escapeHtml(t.text)}</textarea>
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
                    <td class="py-3 px-4 text-white/45 text-xs whitespace-nowrap align-top">${time}<div class="mt-1">${srcBadge}</div></td>
                    <td class="py-3 px-4 align-top text-cell" style="color: var(--txt); font-size: 14px;">${textCell}</td>
                    <td class="py-3 px-4 text-white/40 text-xs text-right align-top">${dur}</td>
                    <td class="py-3 px-4 text-center align-top whitespace-nowrap actions-cell">
                        <button onclick="event.stopPropagation(); copyText(${i}, this)"
                            class="text-white/45 hover:text-white/70 text-xs p-2 rounded hover:bg-white/5"
                            title="Copiar" aria-label="Copiar texto">Copiar</button>
                        <button onclick="event.stopPropagation(); startEdit(${t.id})"
                            class="text-white/45 hover:text-white/70 text-xs p-2 rounded hover:bg-white/5 ml-0.5"
                            title="Editar" aria-label="Editar">${ICONS.pencil}</button>
                        <button onclick="event.stopPropagation(); deleteSingle(${t.id}, this)"
                            class="text-white/45 hover:text-red-400 text-xs p-2 rounded hover:bg-red-500/10 ml-0.5"
                            title="Eliminar" aria-label="Eliminar">${ICONS.close}</button>
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

        function copyEl(id, btn) {
            const el = document.getElementById(id);
            if (!el) return;
            navigator.clipboard.writeText(el.innerText.trim()).then(() => {
                const prev = btn.textContent;
                btn.textContent = '✓';
                setTimeout(() => { btn.textContent = prev; }, 1500);
            });
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
            if (!newText) { toast('No se puede guardar vacío', 'err'); ta.focus(); return; }
            const btn = document.querySelector('button[onclick*="saveEdit(' + id + ')"]');
            if (btn) btn.disabled = true;
            try {
                const res = await fetch('/api/transcriptions/' + id, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({text: newText})
                });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                editingId = null;
                showOffline(false);
                loadData();
            } catch (err) {
                showOffline(true);
                toast('No se pudo guardar el cambio', 'err');
                if (btn) btn.disabled = false;
            }
        }

        // --- Delete single ---
        async function deleteSingle(id, btn) {
            if (!confirm('¿Eliminar esta transcripción? Esta acción no se puede deshacer.')) return;
            btn.disabled = true;
            const row = btn.closest('tr');
            row.classList.add('deleted');
            setTimeout(async () => {
                try {
                    const res = await fetch('/api/transcriptions/' + id, {method: 'DELETE'});
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    showOffline(false);
                    loadData();
                } catch (err) {
                    showOffline(true);
                    toast('No se pudo eliminar la transcripción', 'err');
                    row.classList.remove('deleted');
                    btn.disabled = false;
                }
            }, 350);
        }

        // --- Bulk delete ---
        async function bulkDelete(range, label) {
            document.getElementById('cleanup-dropdown').classList.remove('open');
            const labels = {day: 'las transcripciones de hoy', week: 'las transcripciones de la última semana',
                month: 'las transcripciones del último mes', all: 'TODAS las transcripciones'};
            const desc = labels[range] || range;
            if (!confirm('¿Eliminar ' + desc + '? Esta acción no se puede deshacer.')) return;
            let url = '/api/transcriptions?range=' + range;
            if (range === 'day' && label === 'hoy') {
                url += '&date=' + new Date().toISOString().slice(0,10);
            }
            await fetch(url, {method: 'DELETE'});
            loadData();
        }

        async function deleteByDate(dateStr) {
            const picker = document.getElementById('cleanup-date');
            if (!dateStr) dateStr = picker ? picker.value : '';
            if (!dateStr || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(dateStr)) return;
            document.getElementById('cleanup-dropdown').classList.remove('open');
            if (!confirm('¿Eliminar las transcripciones del ' + dateStr + '? Esta acción no se puede deshacer.')) { if (picker) picker.value = ''; return; }
            await fetch('/api/transcriptions?range=day&date=' + dateStr, {method: 'DELETE'});
            if (picker) picker.value = '';
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
            if (!confirm('¿Eliminar ' + n + ' transcripción' + (n > 1 ? 'es' : '') + ' seleccionada' + (n > 1 ? 's' : '') + '? Esta acción no se puede deshacer.')) return;
            const btn = document.querySelector('#selection-bar button[onclick*="deleteSelected"]');
            if (btn) btn.disabled = true;
            try {
                const res = await fetch('/api/transcriptions/delete-batch', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ids: [...selectedIds]})
                });
                if (!res.ok) throw new Error('HTTP ' + res.status);
                selectedIds.clear();
                showOffline(false);
                loadData();
            } catch (err) {
                showOffline(true);
                toast('No se pudieron eliminar las transcripciones', 'err');
            } finally {
                if (btn) btn.disabled = false;
            }
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

        // Collapse all expanded rows on Escape (and close any open panel)
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                expandedIds.clear();
                document.querySelectorAll('.text-preview.expanded').forEach(el => {
                    el.classList.remove('expanded');
                });
                navigate('dictados');  // Escape vuelve a la vista principal
            }
        });

        _fillIcons();  // rellena los iconos SVG de los elementos estáticos (data-icon)

        // (El arranque del shell — _applyRoute + estado del sidebar — vive al final del
        // bloque del router: sus const _VIEWS/_VIEW_TITLES aún no existen aquí (TDZ).)

        // Auto-refresh every 5 seconds (pausado cuando la pestaña está oculta o en otra vista)
        loadData();
        setInterval(() => {
            if (document.hidden) return;
            if (_route !== 'dictados') return;  // el historial solo se refresca en su vista
            // No refrescar si el usuario está editando: destruiría el textarea.
            if (editingId !== null) return;
            // No refrescar si hay una selección de texto activa dentro de la tabla.
            const s = window.getSelection();
            if (s && !s.isCollapsed && s.toString().trim()) {
                const tb = document.getElementById('tbody');
                if (tb && s.anchorNode && tb.contains(s.anchorNode)) return;
            }
            loadData();
        }, 5000);
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) loadData();
        });

        // --- Enrutado por hash (U2): única fuente de verdad de visibilidad ---
        // Reemplaza al acordeón closeOtherPanels/_PANEL_IDS: cada panel es una vista.
        const _VIEWS = {
            dictados: 'dictados-view',
            reunion: 'meeting-panel',
            diccionario: 'dictionary-panel',
            url: 'url-queue-panel',
            atajos: 'shortcuts-panel',
            ajustes: 'settings-panel',
        };
        const _VIEW_TITLES = {
            dictados: 'Dictados', reunion: 'Reunión', diccionario: 'Diccionario',
            url: 'Transcribir desde URL', atajos: 'Atajos de teclado', ajustes: 'Ajustes',
        };
        let _route = 'dictados';

        function currentRoute() {
            const h = (location.hash || '').replace(/^#\\/?/, '');
            return _VIEWS[h] ? h : 'dictados';
        }
        function navigate(view) { if (_VIEWS[view]) location.hash = '#/' + view; }

        function _applyRoute() {
            const route = currentRoute();
            const prev = _route;
            _route = route;
            Object.entries(_VIEWS).forEach(([name, id]) => {
                const el = document.getElementById(id);
                if (el) el.classList.toggle('hidden', name !== route);
            });
            document.querySelectorAll('#sidebar .nav-item').forEach(a =>
                a.classList.toggle('active', a.dataset.view === route));
            const title = document.getElementById('view-title');
            if (title) title.textContent = _VIEW_TITLES[route] || 'Vflow';
            document.querySelectorAll('.dictados-only').forEach(el =>
                el.classList.toggle('hidden', route !== 'dictados'));
            // Salida de vista: detener polls del panel que se abandona
            if (prev === 'url' && route !== 'url') _stopUqPoll();
            if (prev === 'reunion' && route !== 'reunion') _stopMtPoll();
            // Entrada de vista: hooks de montaje
            if (route === 'ajustes') loadSettings();
            if (route === 'diccionario') { loadDictionary().then(() => _applyPendingDictPrefill()); }
            if (route === 'url') {
                loadUrlQueue().then(summary => {
                    if (summary && (summary.pending > 0 || summary.processing > 0)) _startUqPoll();
                });
            }
            if (route === 'reunion') {
                loadMeeting().then(st => { if (st && st.active) _startMtPoll(); });
            }
            if (route === 'dictados') loadData();
            const panel = document.getElementById(_VIEWS[route]);
            if (panel && route !== 'dictados') _focusPanel(panel);
        }
        window.addEventListener('hashchange', _applyRoute);

        // --- Command palette Ctrl+K (U4) ---
        let _palSel = 0, _palItems = [], _palTimer = null;
        const _PAL_NAV = [
            { label: 'Ir a Dictados', icon: 'mic', run: () => navigate('dictados') },
            { label: 'Ir a Reuniones', icon: 'chat', run: () => navigate('reunion') },
            { label: 'Ir a Diccionario', icon: 'book', run: () => navigate('diccionario') },
            { label: 'Ir a Transcribir desde URL', icon: 'play', run: () => navigate('url') },
            { label: 'Ir a Atajos de teclado', icon: 'keyboard', run: () => navigate('atajos') },
            { label: 'Ir a Ajustes', icon: 'settings', run: () => navigate('ajustes') },
            { label: 'Abrir ventana de reunión completa', icon: 'mic', run: () => window.open('/reunion', '_blank') },
            { label: 'Actualizar historial', icon: 'play', run: () => loadData() },
        ];

        function _palNorm(s) { return (s || '').toLowerCase(); }

        function openPalette() {
            document.getElementById('palette-overlay').classList.remove('hidden');
            const input = document.getElementById('palette-input');
            input.value = '';
            _renderPalette([]);
            _palQuery('');
            setTimeout(() => input.focus(), 0);
        }
        function closePalette() {
            document.getElementById('palette-overlay').classList.add('hidden');
        }
        function _paletteOpen() {
            return !document.getElementById('palette-overlay').classList.contains('hidden');
        }

        async function _palQuery(q) {
            const nq = _palNorm(q);
            let items = _PAL_NAV.filter(n => !nq || _palNorm(n.label).includes(nq))
                .map(n => ({ label: n.label, icon: n.icon, sub: '', run: n.run }));
            if (nq.length >= 2) {
                try {
                    const rows = await fetch('/api/transcriptions/search?q=' + encodeURIComponent(q)).then(r => r.json());
                    rows.forEach(t => {
                        const date = new Date(t.created_at + 'Z');
                        items.push({
                            label: (t.text || '').slice(0, 90),
                            icon: 'mic',
                            sub: date.toLocaleDateString('es-MX', { month: 'short', day: 'numeric' }),
                            run: () => {
                                navigator.clipboard.writeText(t.text || '');
                                toast('Transcripción copiada al portapapeles', 'ok');
                            },
                        });
                    });
                } catch (e) { /* búsqueda es best-effort */ }
            }
            _renderPalette(items);
        }

        function _renderPalette(items) {
            _palItems = items;
            _palSel = 0;
            const list = document.getElementById('palette-list');
            if (!items.length) {
                list.innerHTML = '<div class="palette-item" style="cursor:default">Sin resultados</div>';
                return;
            }
            list.innerHTML = items.map((it, i) =>
                '<div class="palette-item' + (i === 0 ? ' sel' : '') + '" data-i="' + i + '">' +
                (ICONS[it.icon] || '') + '<span class="truncate">' + escapeHtml(it.label) + '</span>' +
                (it.sub ? '<span class="pal-sub">' + escapeHtml(it.sub) + '</span>' : '') + '</div>'
            ).join('');
            list.querySelectorAll('.palette-item[data-i]').forEach(el => {
                el.addEventListener('click', () => { closePalette(); _palItems[parseInt(el.dataset.i)].run(); });
            });
        }

        function _palMove(delta) {
            if (!_palItems.length) return;
            _palSel = (_palSel + delta + _palItems.length) % _palItems.length;
            const els = document.querySelectorAll('#palette-list .palette-item[data-i]');
            els.forEach((el, i) => el.classList.toggle('sel', i === _palSel));
            if (els[_palSel]) els[_palSel].scrollIntoView({ block: 'nearest' });
        }

        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                _paletteOpen() ? closePalette() : openPalette();
            }
        });
        // El overlay maneja sus propias teclas ANTES que el listener global de Escape
        // (bubbling elemento→document): stopPropagation evita que Escape navegue a Dictados.
        document.getElementById('palette-overlay').addEventListener('keydown', (e) => {
            if (e.key === 'Escape') { e.stopPropagation(); closePalette(); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); _palMove(1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); _palMove(-1); }
            else if (e.key === 'Enter') {
                e.preventDefault();
                const it = _palItems[_palSel];
                if (it) { closePalette(); it.run(); }
            }
        });
        document.getElementById('palette-overlay').addEventListener('click', (e) => {
            if (e.target.id === 'palette-overlay') closePalette();
        });
        document.getElementById('palette-input').addEventListener('input', (e) => {
            clearTimeout(_palTimer);
            const q = e.target.value;
            _palTimer = setTimeout(() => _palQuery(q), 180);
        });

        // Arranque del shell (aquí los const del router ya están inicializados).
        _applyRoute();
        _refreshSidebarStatus();

        // Enfoca el primer control del panel tras hacerlo visible.
        function _focusPanel(panel) {
            setTimeout(() => {
                panel.querySelector('input,select,textarea')?.focus();
            }, 0);
        }

        // Estado del sistema en el pie del sidebar (backend + fuente de audio).
        async function _refreshSidebarStatus() {
            try {
                const s = await fetch('/api/settings').then(r => r.json());
                const be = (s.transcription_backend || 'groq') === 'local' ? 'Local (sin internet)' : 'Groq (nube)';
                const src = (s.audio_source || 'mic') === 'system' ? 'Audio del sistema' : 'Micrófono';
                document.getElementById('sb-backend').textContent = 'Backend: ' + be;
                document.getElementById('sb-source').textContent = 'Fuente: ' + src;
            } catch (e) { /* silencioso: es informativo */ }
        }

        // Compat: los antiguos toggles ahora navegan (cualquier onclick que quede sigue funcionando).
        function toggleSettings() { navigate('ajustes'); }

        let _lastSettings = null;  // cache de la última respuesta de /api/settings (presets de insights la consultan)

        async function loadSettings() {
            const [settings, mics] = await Promise.all([
                fetch('/api/settings').then(r => r.json()),
                fetch('/api/microphones').then(r => r.json()),
            ]);
            _lastSettings = settings;
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
            // Backend de insights (análisis de reuniones) — por tarea
            document.getElementById('cfg-insights-backend-live').value = settings.insights_backend_live || 'groq';
            document.getElementById('cfg-insights-backend-batch').value = settings.insights_backend_batch || 'groq';
            document.getElementById('cfg-insights-model').value = settings.insights_endpoint_model || 'qwen/qwen2.5-vl-7b';
            document.getElementById('cfg-insights-fallback').checked = settings.insights_fallback !== false;
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
                insights_backend_live: document.getElementById('cfg-insights-backend-live').value,
                insights_backend_batch: document.getElementById('cfg-insights-backend-batch').value,
                insights_endpoint_model: document.getElementById('cfg-insights-model').value.trim(),
                insights_fallback: document.getElementById('cfg-insights-fallback').checked ? 'true' : 'false',
            };
            await fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data),
            });
            const saved = document.getElementById('cfg-saved');
            saved.style.opacity = '1';
            setTimeout(() => { saved.style.opacity = '0'; }, 2000);
            _refreshSidebarStatus();  // el pie del sidebar refleja backend/fuente al instante
        }

        // --- Presets de un clic para el backend de insights ---
        async function applyInsightsPreset(kind) {
            // _lastSettings se refresca en cada apertura del panel Configuración (loadSettings).
            const s = _lastSettings || await fetch('/api/settings').then(r => r.json());
            if (kind === 'subscription') {
                if (!s.claude_cli_available) {
                    toast('Claude Code no está instalado/logueado', 'err');
                    return;
                }
                document.getElementById('cfg-insights-backend-live').value = 'claude-cli';
                document.getElementById('cfg-insights-backend-batch').value = 'claude-cli';
            } else if (kind === 'apis') {
                const missing = [];
                if (!s.has_groq_key) missing.push('Groq');
                if (!s.has_openrouter_key) missing.push('OpenRouter');
                if (missing.length) {
                    toast('Falta la key de ' + missing.join(' y ') + ' — ponla abajo en API Keys', 'err');
                    return;
                }
                document.getElementById('cfg-insights-backend-live').value = 'groq';
                document.getElementById('cfg-insights-backend-batch').value = 'openrouter';
            } else {
                return;
            }
            onInsightsBackendChange();
            await saveSettings();
            const label = kind === 'subscription' ? 'claude-cli / claude-cli' : 'groq / openrouter';
            toast('Backends configurados: ' + label, 'ok');
        }

        // --- Acordeón del panel de Configuración ---
        function toggleSetSec(id) {
            const sec = document.getElementById('sec-' + id);
            if (!sec) return;
            const collapsed = sec.classList.toggle('collapsed');
            const head = sec.querySelector('.set-sec-head');
            const lbl = sec.querySelector('.set-sec-toggle-label');
            if (head) head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
            if (lbl) lbl.textContent = collapsed ? 'Mostrar' : 'Ocultar';
            if (!collapsed && id === 'apikeys') loadApiKeyStatus();
        }

        function setNavGo(id) {
            const sec = document.getElementById('sec-' + id);
            if (!sec) return;
            if (sec.classList.contains('collapsed')) toggleSetSec(id);  // expandir si estaba cerrada
            sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        let _setAllCollapsed = false;
        function setCollapseAll() {
            _setAllCollapsed = !_setAllCollapsed;
            document.querySelectorAll('#settings-panel .set-sec').forEach(sec => {
                sec.classList.toggle('collapsed', _setAllCollapsed);
                const head = sec.querySelector('.set-sec-head');
                const lbl = sec.querySelector('.set-sec-toggle-label');
                if (head) head.setAttribute('aria-expanded', _setAllCollapsed ? 'false' : 'true');
                if (lbl) lbl.textContent = _setAllCollapsed ? 'Mostrar' : 'Ocultar';
            });
            const btn = document.getElementById('set-collapse-all');
            if (btn) btn.textContent = _setAllCollapsed ? 'Expandir todo' : 'Comprimir todo';
        }

        async function loadApiKeyStatus() {
            try {
                const s = await fetch('/api/keys').then(r => r.json());
                const set = (id, ok) => {
                    const el = document.getElementById(id);
                    if (!el) return;
                    el.textContent = ok ? '· configurada ✓' : '· no configurada';
                    el.className = 'ml-1 text-xs ' + (ok ? 'text-emerald-400/90' : 'text-white/45');
                };
                set('groq-key-status', s.groq);
                set('openrouter-key-status', s.openrouter);
                set('anthropic-key-status', s.anthropic);
            } catch (e) {}
        }

        async function saveApiKey(provider) {
            const input = document.getElementById('cfg-' + provider + '-key');
            const fb = document.getElementById('api-keys-feedback');
            const val = (input.value || '').trim();
            if (!val) { toast('Pega la API key primero', 'err'); input.focus(); return; }
            try {
                const res = await fetch('/api/keys', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({[provider]: val}),
                });
                const d = await res.json();
                if (!res.ok || d.error) { toast(d.error || 'No se pudo guardar la API key', 'err'); return; }
                input.value = '';
                if (fb) {
                    fb.classList.remove('hidden');
                    fb.style.color = '#4ade80';
                    fb.textContent = 'Guardada y cifrada en este equipo ✓';
                    setTimeout(() => fb.classList.add('hidden'), 4000);
                }
                loadApiKeyStatus();
            } catch (ex) {
                toast('Error de red al guardar la API key', 'err');
            }
        }

        // --- Backend local ---
        function onBackendChange() {
            updateLocalModelSection();
        }

        // --- Backend de insights (análisis de reuniones) — dos selectores por tarea ---
        function onInsightsBackendChange() {
            const backendLive = document.getElementById('cfg-insights-backend-live').value;
            const backendBatch = document.getElementById('cfg-insights-backend-batch').value;
            const anyEndpoint = backendLive === 'endpoint' || backendBatch === 'endpoint';
            const anyOpenrouter = backendLive === 'openrouter' || backendBatch === 'openrouter';
            const anyAnthropic = backendLive === 'anthropic' || backendBatch === 'anthropic';
            const anyClaudeCli = backendLive === 'claude-cli' || backendBatch === 'claude-cli';
            document.getElementById('cfg-insights-endpoint-wrap').classList.toggle('hidden', !anyEndpoint);
            document.getElementById('cfg-insights-openrouter-wrap').classList.toggle('hidden', !anyOpenrouter);
            document.getElementById('cfg-insights-anthropic-wrap').classList.toggle('hidden', !anyAnthropic);
            document.getElementById('cfg-insights-claudecli-wrap').classList.toggle('hidden', !anyClaudeCli);
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

        function toggleDictionary() { navigate('diccionario'); }
        function toggleShortcuts() { navigate('atajos'); }

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

        function toggleMeeting() { navigate('reunion'); }

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
            } catch(e) {
                // No matamos el intervalo: el polling sigue vivo para reintentar.
                toast('No se pudo actualizar la reunión', 'err');
                return {};
            }
        }

        // Sufijo de un pendiente: responsable + fecha/hora si existen → "(María · viernes 15:00)"
        function pendMeta(p) {
            const parts = [];
            if (p && p.responsable) parts.push(escapeHtml(String(p.responsable)));
            const fh = [p && p.fecha, p && p.hora].filter(Boolean).map(x => escapeHtml(String(x))).join(' ');
            if (fh) parts.push(ICONS.calendar+' ' + fh);
            return parts.length ? ' <span class="text-white/50">(' + parts.join(' · ') + ')</span>' : '';
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
                el.innerHTML = '<div class="text-xs text-white/45">Temas, pendientes y propuestas aparecerán aquí a medida que avance la reunión.</div>';
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
                        return '<div class="text-xs text-white/75 mb-0.5' + fadeCls(p.id) + '">'+ICONS.arrow+' ' + escapeHtml(String(p.texto || '')) + pendMeta(p) + '</div>';
                    }).join('');
                } else {
                    fhtml += '<div class="text-[11px] text-white/50">Sin pendientes detectados aún.</div>';
                }
                fhtml += '<div class="text-[10px] text-white/45 mt-2">Modo Foco: solo lo accionable. Cambia a Revisión para ver todo.</div>';
                el.innerHTML = fhtml;
                return;
            }
            let html = '';
            if (temas.length) {
                html += '<div><div class="text-[11px] uppercase tracking-wide text-white/55 mb-1">Temas</div>'
                    + temas.map(t => '<div class="text-xs text-white/75 mb-0.5' + fadeCls(t.id) + '">• ' + escapeHtml(String(t.text != null ? t.text : t)) + '</div>').join('') + '</div>';
            }
            if (pend.length) {
                html += '<div><div class="text-[11px] uppercase tracking-wide text-amber-300/50 mb-1">Pendientes</div>'
                    + pend.map(p => {
                        return '<div class="text-xs text-white/75 mb-0.5' + fadeCls(p.id) + '">'+ICONS.arrow+' ' + escapeHtml(String(p.texto || '')) + pendMeta(p) + '</div>';
                    }).join('') + '</div>';
            }
            if (prop.length) {
                html += '<div><div class="text-[11px] uppercase tracking-wide text-sky-300/50 mb-1">Propuestas</div>'
                    + prop.map(p => '<div class="text-xs text-white/75 mb-0.5' + fadeCls(p.id) + '">'+ICONS.bulb+' ' + escapeHtml(String(p.texto || '')) + '</div>').join('') + '</div>';
            }
            const citas = ins.citas || [];
            if (citas.length) {
                html += '<div><div class="text-[11px] uppercase tracking-wide text-emerald-300/50 mb-1">Próximas reuniones</div>'
                    + citas.map(c => '<div class="text-xs text-white/75 mb-0.5' + fadeCls(c.id) + '">'+ICONS.calendar+' ' + escapeHtml(String(c.texto || '')) + pendMeta({fecha: c.fecha, hora: c.hora}) + '</div>').join('') + '</div>';
            }
            el.innerHTML = html;
        }

        function renderMinutes(m) {
            const wrap = document.getElementById('mt-minutes');
            const body = document.getElementById('mt-minutes-body');
            if (!wrap || !body) return;
            m = m || {};
            const dec = m.decisiones || [], tem = m.temas || [], pen = m.pendientes || [], prop = m.propuestas || [], cit = m.citas || [], mom = m.momentos_destacados || [];
            if (!m.resumen && !dec.length && !tem.length && !pen.length && !prop.length && !cit.length && !mom.length) {
                wrap.classList.add('hidden');
                return;
            }
            let html = '';
            if (m.resumen) html += '<p class="text-white/80">' + escapeHtml(String(m.resumen)) + '</p>';
            if (mom.length) html += '<div><div class="text-xs text-yellow-300/50 mt-2 mb-1">⭐ Momentos destacados</div>'
                + mom.map(x => '<div class="text-xs text-white/75">'+ICONS.star+' <span class="text-white/50">' + escapeHtml(String(x.time || '')) + '</span> ' + escapeHtml(String(x.texto || '')) + '</div>').join('') + '</div>';
            if (dec.length) html += '<div><div class="text-xs text-white/40 mt-2 mb-1">Decisiones</div>'
                + dec.map(d => '<div class="text-xs text-white/75">• ' + escapeHtml(String(d)) + '</div>').join('') + '</div>';
            if (pen.length) html += '<div><div class="text-xs text-amber-300/50 mt-2 mb-1">Pendientes</div>'
                + pen.map(p => '<div class="text-xs text-white/75">'+ICONS.arrow+' ' + escapeHtml(String(p.texto || p)) + pendMeta(p) + '</div>').join('') + '</div>';
            if (prop.length) html += '<div><div class="text-xs text-sky-300/50 mt-2 mb-1">Propuestas</div>'
                + prop.map(p => '<div class="text-xs text-white/75">'+ICONS.bulb+' ' + escapeHtml(String(p.texto != null ? p.texto : p)) + '</div>').join('') + '</div>';
            if (cit.length) html += '<div><div class="text-xs text-emerald-300/50 mt-2 mb-1">Próximas reuniones</div>'
                + cit.map(c => '<div class="text-xs text-white/75">'+ICONS.calendar+' ' + escapeHtml(String(c.texto || c)) + pendMeta({fecha: c.fecha, hora: c.hora}) + '</div>').join('') + '</div>';
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
                    ? '<div class="text-xs text-white/45">Escuchando… el texto aparecerá cada ~20s.</div>'
                    : '<div class="text-xs text-white/45">El transcript en vivo aparecerá aquí cuando inicies una reunión.</div>';
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
                div.innerHTML = '<span class="text-[10px] font-mono text-white/55 mr-1">' + s.time + '</span>'
                    + '<span class="text-xs font-medium ' + color + ' mr-1">' + s.speaker + ':</span>'
                    + escapeHtml(s.text);
                container.appendChild(div);
            }
            container.dataset.count = String(segments.length);
            if (nearBottom) container.scrollTop = container.scrollHeight;  // solo auto-scroll si ya estabas abajo
        }

        async function startMeeting() {
            const btn = document.getElementById('mt-start');
            const fb = document.getElementById('mt-feedback');
            fb.classList.add('hidden');
            document.getElementById('mt-minutes').classList.add('hidden');  // limpiar acta previa
            _mtSegSig = _mtInsSig = _mtStatusSig = null;  // forzar render limpio de la nueva reunión
            _mtSeenInsightIds = new Set();
            _mtMinutesShown = false;
            document.getElementById('mt-transcript').dataset.count = '0';
            if (btn) btn.disabled = true;
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
            } finally {
                if (btn) btn.disabled = false;
            }
        }

        async function stopMeeting() {
            const btn = document.getElementById('mt-stop');
            const statusEl = document.getElementById('mt-status');
            statusEl.textContent = 'Terminando, transcribiendo lo último y generando el acta…';
            statusEl.className = 'text-xs text-white/40';
            if (btn) btn.disabled = true;
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
                statusEl.textContent = 'No se pudo terminar la reunión. Reintenta.';
                statusEl.className = 'text-xs text-red-300';
                toast('No se pudo terminar la reunión', 'err');
            } finally {
                if (btn) btn.disabled = false;
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
                container.innerHTML = '<div class="text-xs text-white/55 py-2">Aún no hay entradas. Escribe una palabra en el campo de arriba (o un par «escucho X → escribo Y») y pulsa Añadir para que Whisper la reconozca.</div>';
                return;
            }
            const includedSet = new Set(_dictBudget.included_ids || []);
            container.innerHTML = entries.map(e => {
                const label = e.replace_from
                    ? `<span class="text-white/50">${escapeHtml(e.replace_from)}</span> <span class="text-white/50 mx-1">→</span> <span class="text-white/80">${escapeHtml(e.replace_to)}</span>`
                    : `<span class="text-white/80">${escapeHtml(e.replace_to)}</span>`;
                const checked = e.enabled ? 'checked' : '';
                const pinned = e.pinned ? 'pinned' : '';
                const pinTitle = e.pinned ? 'Desfijado del prompt' : 'Fijar en el prompt (prioridad)';
                const pinIcon = e.pinned ? ICONS.starOn : ICONS.star;
                const inBudget = includedSet.has(e.id);
                const dimmed = !inBudget ? 'dict-entry-dimmed' : '';
                const outOfPromptStyle = !inBudget ? ' style="opacity:0.7"' : '';
                const outOfPromptTitle = !inBudget ? ' title="Fuera del prompt por límite de espacio; el reemplazo de corrección sigue activo"' : '';
                const outBudgetBadge = !inBudget
                    ? `<span class="text-amber-300/90 text-xs ml-1 px-1 py-0.5 rounded bg-amber-400/10 whitespace-nowrap" title="Este término no cabe en el prompt de vocabulario, pero su corrección sigue funcionando">fuera del prompt</span>`
                    : '';
                const hitBadge = (e.hit_count > 0)
                    ? `<span class="text-white/55 text-xs ml-1" title="Correcciones aplicadas">×${e.hit_count}</span>`
                    : '';
                return `
                <div class="flex items-center gap-3 py-1.5 border-b border-white/[0.04] ${dimmed}" data-dict-id="${e.id}"${outOfPromptStyle}${outOfPromptTitle}>
                    <button class="dict-pin-btn ${pinned} p-2" title="${pinTitle}" aria-label="${e.pinned ? 'Desfijar término del prompt' : 'Fijar término'}" aria-pressed="${e.pinned ? 'true' : 'false'}" onclick="pinDictEntry(${e.id}, ${e.pinned ? 0 : 1})">${pinIcon}</button>
                    <label class="toggle-switch flex-shrink-0">
                        <input type="checkbox" ${checked} aria-label="Activar o desactivar este término" onchange="toggleDictEntry(${e.id}, this.checked)">
                        <span class="toggle-slider"></span>
                    </label>
                    <span class="text-sm flex-1">${label}${hitBadge}${outBudgetBadge}</span>
                    <button onclick="deleteDictEntry(${e.id})" aria-label="Eliminar"
                        class="text-white/55 hover:text-red-400 text-xs p-2 rounded hover:bg-red-500/10">${ICONS.close}</button>
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
            const resultEl = document.getElementById('dict-import-result');
            if (file.size > 2 * 1024 * 1024) {
                resultEl.classList.remove('hidden');
                resultEl.textContent = 'El archivo es demasiado grande (máx. 2 MB). Reduce el CSV e inténtalo de nuevo.';
                resultEl.style.color = '#f87171';
                input.value = '';
                setTimeout(() => resultEl.classList.add('hidden'), 5000);
                return;
            }
            const fd = new FormData();
            fd.append('file', file);
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

        // Contrato U2 (rescate del flujo selección→diccionario): la tabla y el panel
        // diccionario ya NO coexisten en el DOM visible. El texto seleccionado se guarda
        // en estado y el prefill ocurre al montar la vista diccionario (hook del router).
        let _pendingDictText = null;

        function addSelectedTextToDict() {
            const btn = document.getElementById('dict-from-history-btn');
            _pendingDictText = (btn && btn._selectedText) || '';
            hideDictFromHistoryBtn();
            window.getSelection().removeAllRanges();
            if (_route === 'diccionario') {
                _applyPendingDictPrefill();   // ya estamos en la vista: prefill inmediato
            } else {
                navigate('diccionario');      // el hook del router hace el prefill al montar
            }
        }

        function _applyPendingDictPrefill() {
            if (_pendingDictText === null) return;
            const fromInput = document.getElementById('dict-replace-from');
            const toInput = document.getElementById('dict-replace-to');
            if (fromInput) fromInput.value = _pendingDictText;
            _pendingDictText = null;
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

        function toggleUrlQueue() { navigate('url'); }

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

        const _platformIcons = { youtube: ICONS.youtube, tiktok: ICONS.music, instagram: ICONS.camera, other: ICONS.link };
        const _statusLabels = {
            pending: '<span class="text-white/55">Pendiente</span>',
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
            const prevScroll = container ? container.scrollTop : 0;
            if (!items.length) {
                container.innerHTML = '<div class="text-xs text-white/55 py-2">La cola está vacía. Pega una URL de YouTube, TikTok o Instagram arriba y pulsa Añadir para transcribirla sin grabar audio.</div>';
                return;
            }
            container.innerHTML = items.map(item => {
                const icon = _platformIcons[item.platform] || ICONS.link;
                const statusHtml = _statusLabels[item.status] || item.status;
                const stageHtml = item.stage && item.status === 'processing'
                    ? '<span class="text-white/55 ml-1">(' + escapeHtml(item.stage) + ')</span>' : '';
                const titleHtml = item.title
                    ? '<span class="text-white/55 ml-1 truncate" style="max-width:200px" title="' + escapeHtml(item.title) + '">' + escapeHtml(item.title) + '</span>'
                    : '';
                const errorHtml = item.error && item.status === 'error'
                    ? '<div class="text-xs text-red-400/60 mt-0.5 truncate" title="' + escapeHtml(item.error) + '">' + escapeHtml(item.error) + '</div>'
                    : '';
                return '<div class="flex items-start gap-2 py-1.5 border-b border-white/[0.04]">' +
                    '<span class="text-white/55 text-xs mt-0.5 flex-shrink-0">' + icon + '</span>' +
                    '<div class="flex-1 min-w-0">' +
                    '<div class="flex items-center gap-2 flex-wrap">' +
                    '<span class="text-xs text-white/55 truncate" title="' + escapeHtml(item.url) + '">' + escapeHtml(_shortUrl(item.url)) + '</span>' +
                    '<span class="text-xs">' + statusHtml + stageHtml + '</span>' +
                    titleHtml +
                    '</div>' + errorHtml + '</div></div>';
            }).join('');
            if (container) container.scrollTop = prevScroll;
        }

        async function enqueueUrls() {
            const singleUrl = (document.getElementById('uq-url').value || '').trim();
            const bulkText = (document.getElementById('uq-bulk').value || '').trim();
            const allowInstagram = document.getElementById('uq-instagram').checked;
            const feedback = document.getElementById('uq-feedback');

            const combined = [singleUrl, bulkText].filter(Boolean).join('\\n');
            if (!combined.trim()) return;

            const enqueueBtn = document.getElementById('uq-btn');
            let _enqOld = '';
            if (enqueueBtn) { enqueueBtn.disabled = true; _enqOld = enqueueBtn.textContent; enqueueBtn.textContent = 'Añadiendo…'; }
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
            } finally {
                if (enqueueBtn) { enqueueBtn.disabled = false; enqueueBtn.textContent = _enqOld; }
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

        // Watcher global: refrescar historial cuando se completan items de la cola.
        // Condicionado a la RUTA (única fuente de verdad de visibilidad), no a .hidden.
        let _lastQueueDone = 0;
        setInterval(async () => {
            if (_route !== 'url') return;
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

let _mtNotes = [], _mtPaused = false;
async function loadLive(){
  try{
    const r = await fetch('/api/meeting'); const d = await r.json(); const s = d.status||{};
    const startB=document.getElementById('mt-start'), stopB=document.getElementById('mt-stop');
    const liveHeader=document.getElementById('mt-live-header'), actionBar=document.getElementById('mt-action-bar');
    const tabs=document.getElementById('mt-tabs');
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
    } else { startB.classList.remove('hidden'); stopB.classList.add('hidden'); document.getElementById('mt-status').textContent='';
      liveHeader.classList.add('hidden'); actionBar.classList.add('hidden'); tabs.classList.add('hidden');
      document.getElementById('mt-paused-badge').classList.add('hidden'); _mtPaused=false;
      if(d.last_minutes && !_actaShown){ renderActa(d.last_minutes); _actaShown=true; loadHistory(); } }
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
function actaHtml(m){
  m=m||{}; const dec=m.decisiones||[],tem=m.temas||[],pen=m.pendientes||[],pro=m.propuestas||[],cit=m.citas||[],mom=m.momentos_destacados||[]; let h='';
  if(m.resumen)h+='<p class="text-white/80">'+esc(m.resumen)+'</p>';
  if(mom.length)h+='<div><div class="text-xs text-yellow-300/50 mt-2 mb-1">\\u2b50 Momentos destacados</div>'+mom.map(x=>'<div class="text-xs text-white/75">'+ICONS.spark+' <span class="text-white/50">'+esc(x.time||'')+'</span> '+esc(x.texto||'')+'</div>').join('')+'</div>';
  if(dec.length)h+='<div><div class="text-xs text-white/40 mt-2 mb-1">Decisiones</div>'+dec.map(d=>'<div class="text-xs text-white/75">\\u2022 '+esc(d)+'</div>').join('')+'</div>';
  if(pen.length)h+='<div><div class="text-xs text-amber-300/50 mt-2 mb-1">Pendientes</div>'+pen.map(p=>'<div class="text-xs text-white/75">'+ICONS.arrow+' '+esc(p.texto||p)+pendMeta(p)+'</div>').join('')+'</div>';
  if(pro.length)h+='<div><div class="text-xs text-sky-300/50 mt-2 mb-1">Propuestas</div>'+pro.map(p=>'<div class="text-xs text-white/75">'+ICONS.bulb+' '+esc(p.texto!=null?p.texto:p)+'</div>').join('')+'</div>';
  if(cit.length)h+='<div><div class="text-xs text-emerald-300/50 mt-2 mb-1">Próximas reuniones</div>'+cit.map(c=>'<div class="text-xs text-white/75">'+ICONS.calendar+' '+esc(c.texto||c)+pendMeta({fecha:c.fecha,hora:c.hora})+'</div>').join('')+'</div>';
  if(tem.length)h+='<div><div class="text-xs text-white/40 mt-2 mb-1">Temas tratados</div>'+tem.map(t=>'<div class="text-xs text-white/75">\\u2022 '+esc(t)+'</div>').join('')+'</div>';
  return h||'<div class="text-xs text-white/30">Acta vacía.</div>';
}
function renderActa(m){ document.getElementById('mt-acta-body').innerHTML=actaHtml(m); document.getElementById('mt-acta').classList.remove('hidden'); }

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
      return '<div class="glass rounded-lg px-3 py-2 flex items-center gap-2 hover:bg-white/[0.04]">'
        +'<span class="text-xs text-white/70 flex-1 cursor-pointer" onclick="openMeeting('+m.id+')">'+esc(m.started_at||m.created_at||'')+'</span>'
        +'<span class="text-[11px] text-white/30">'+dur+' min</span>'
        +'<button onclick="delMeeting('+m.id+')" class="text-white/25 hover:text-red-300 text-sm px-1" title="Eliminar esta reunión">'+ICONS.trash+'</button>'
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
      body:JSON.stringify({message:msg,history:asstHistory.slice(-6),meeting_id:asstMeetingId,reasoning:reasoningVal})
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


@app.route("/")
def index():
    """Sirve la página principal del dashboard de transcripciones."""
    return render_template_string(HTML_TEMPLATE)


@app.route("/reunion")
def reunion():
    """Ventana dedicada al modo reunión: en vivo (transcript + análisis + acta) + historial."""
    return render_template_string(MEETING_PAGE)


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


# Proveedores cuya API key se guarda cifrada (DPAPI) por equipo, como <ENVVAR>_ENC.
_SECRET_KEYS = {
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
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
        "insights_backend_live": "INSIGHTS_BACKEND_LIVE",
        "insights_backend_batch": "INSIGHTS_BACKEND_BATCH",
        "insights_fallback": "INSIGHTS_FALLBACK",
        "insights_endpoint_model": "INSIGHTS_ENDPOINT_MODEL",
        "audio_source": "AUDIO_SOURCE",
        "anthropic_model_live": "ANTHROPIC_MODEL_LIVE",
        "anthropic_model_batch": "ANTHROPIC_MODEL_BATCH",
        "claude_cli_model_batch": "CLAUDE_CLI_MODEL_BATCH",
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
    """Lista de reuniones pasadas (sin transcript completo) para el historial."""
    return jsonify({"meetings": _db.meetings_recent(limit=200)})


@app.route("/api/meetings/<int:meeting_id>", methods=["GET"])
def meeting_detail(meeting_id):
    """Devuelve una reunión completa (transcript + acta + insights) para el visor."""
    m = _db.meeting_get(meeting_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    import json as _json
    for k in ("minutes_json", "insights_json", "segments_json", "chapters_json"):
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
