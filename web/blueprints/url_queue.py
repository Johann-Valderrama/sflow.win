"""Blueprint de la cola de transcripción por URL (Fase 3, paso 2): encolar, listar,
limpiar y el worker serial en background que la procesa."""

import os
import re
import threading

from flask import Blueprint, jsonify, request

from web import state as _state
from web.state import _db

bp = Blueprint("url_queue", __name__)

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


@bp.route("/api/url-queue", methods=["POST"])
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


@bp.route("/api/url-queue")
def url_queue_list():
    """Devuelve la lista completa de items de la cola y un resumen por status."""
    items = _db.url_queue_list()
    summary = _db.url_queue_summary()
    return jsonify({"items": items, "summary": summary})


@bp.route("/api/url-queue/clear", methods=["POST"])
def url_queue_clear():
    """Elimina filas con status 'done' o 'error'. Deja pending/processing intactos."""
    deleted = _db.url_queue_clear_finished()
    return jsonify({"deleted": deleted})


@bp.route("/api/url-queue/cancel-pending", methods=["POST"])
def url_queue_cancel_pending():
    """Elimina filas 'pending' sin tocar la que está procesando."""
    deleted = _db.url_queue_cancel_pending()
    return jsonify({"deleted": deleted})


# ---------------------------------------------------------------------------
# Worker serial de la cola de URLs (Fase 3, paso 2)
# ---------------------------------------------------------------------------

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
    """Arranca el worker de la cola de URLs una sola vez (guard contra doble arranque).

    El lock y el flag de "ya arrancado" viven en ``web.state`` (dueño único del
    guard compartido, unidad 4.2); se mutan aquí vía el módulo importado
    (``_state.xxx = ...``) en vez de ``from web.state import _url_worker_started``
    porque ese flag es un bool inmutable: reasignarlo tras un `from ... import`
    solo rebindearía la copia local, sin tocar el original en ``web.state``.
    """
    with _state._url_worker_lock:
        if _state._url_worker_started:
            return
        _state._url_worker_started = True
    t = threading.Thread(target=_url_queue_worker, daemon=True, name="url-queue-worker")
    t.start()
