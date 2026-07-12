"""Blueprint del historial de reuniones (/api/meetings*) y el chat del Asistente de reuniones."""

import json as _json
import os as _os
import re as _re
import time as _time
import unicodedata as _unicodedata

from flask import Blueprint, jsonify, request

from config import MEETINGS_DIR
from core import assistant as _assistant
from core import insights as _insights
from core import meeting_export as _meeting_export
from web.state import MEETING, _db

bp = Blueprint("meetings", __name__)


def _normalize_highlights(raw: object) -> list:
    """Normaliza la lista de highlights de una reunión para el visor (unidad 5.1 v2).

    Retrocompat: entradas guardadas ANTES de esta feature no tienen "source"
    (siempre eran manuales) — se completan con "source": "manual" al leer, sin
    migrar la DB. Tolerante ante basura: ítems que no sean dict se descartan.
    """
    if not isinstance(raw, list):
        return []
    out = []
    for h in raw:
        if not isinstance(h, dict):
            continue
        item = dict(h)
        item.setdefault("source", "manual")
        out.append(item)
    return out


@bp.route("/api/meetings", methods=["GET"])
def meetings_list():
    """Lista de reuniones pasadas (sin transcript completo) para el historial.

    Cada fila incluye "metrics" parseado (dict o None) para la tarjeta del
    historial (unidad 3.2); el metrics_json crudo no se expone en la lista.
    """
    meetings = _db.meetings_recent(limit=200)
    for m in meetings:
        try:
            m["metrics"] = _json.loads(m.pop("metrics_json", None) or "null")
        except Exception:  # noqa: BLE001
            m["metrics"] = None
    return jsonify({"meetings": meetings})


@bp.route("/api/meetings/<int:meeting_id>", methods=["GET"])
def meeting_detail(meeting_id):
    """Devuelve una reunión completa (transcript + acta + insights) para el visor."""
    m = _db.meeting_get(meeting_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    for k in ("minutes_json", "insights_json", "segments_json", "chapters_json", "metrics_json",
              "highlights_json"):
        try:
            m[k.replace("_json", "")] = _json.loads(m.get(k) or "null")
        except Exception:  # noqa: BLE001
            m[k.replace("_json", "")] = None
    m["highlights"] = _normalize_highlights(m.get("highlights"))
    return jsonify(m)


@bp.route("/api/meetings/<int:meeting_id>/chapters", methods=["POST"])
def meeting_chapters_generate(meeting_id):
    """Genera (o regenera) la línea de tiempo de capítulos de una reunión."""
    m = _db.meeting_get(meeting_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    try:
        segments = _json.loads(m.get("segments_json") or "[]") or []
    except Exception:  # noqa: BLE001
        segments = []
    chapters = _insights.generate_chapters(m.get("transcript") or "", segments)
    _db.meeting_set_chapters(meeting_id, _json.dumps(chapters, ensure_ascii=False))
    return jsonify({"chapters": chapters})


@bp.route("/api/meetings/<int:meeting_id>/delete", methods=["POST"])
def meeting_delete_endpoint(meeting_id):
    """Elimina una reunión (DB + su .md)."""
    n = _db.meeting_delete(meeting_id)
    _meeting_export.delete_meeting_files(MEETINGS_DIR, meeting_id)
    return jsonify({"ok": True, "deleted": n})


@bp.route("/api/meetings/clear", methods=["POST"])
def meetings_clear():
    """Elimina TODAS las reuniones (DB + .md)."""
    n = _db.meetings_delete_all()
    _meeting_export.clear_all_files(MEETINGS_DIR)
    return jsonify({"ok": True, "deleted": n})


@bp.route("/api/meetings/open-folder", methods=["POST"])
def meetings_open_folder():
    """Abre en el Explorador la carpeta donde se guardan los .md de reuniones."""
    import os
    try:
        os.makedirs(MEETINGS_DIR, exist_ok=True)
        os.startfile(MEETINGS_DIR)  # noqa: S606 — Windows; abre el Explorador
        return jsonify({"ok": True, "path": MEETINGS_DIR})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc), "path": MEETINGS_DIR}), 500


@bp.route("/api/meetings/search")
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


@bp.route("/api/meetings/export", methods=["POST"])
def meetings_export():
    """Backfill: exporta todas las reuniones de la DB a Markdown."""
    try:
        n = _meeting_export.export_all(_db, MEETINGS_DIR)
        return jsonify({"ok": True, "exported": n, "path": MEETINGS_DIR})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/api/meetings/chat", methods=["POST"])
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


# ---------------------------------------------------------------------------
# Chat de memoria CROSS-reunión + entregables (unidad 5.3)
# ---------------------------------------------------------------------------

@bp.route("/api/meetings/chat-multi", methods=["POST"])
def meetings_chat_multi():
    """Chat sobre un CONJUNTO de reuniones (selección manual o por tema) + entregables.

    Body: {meeting_ids?: [int], fts_query?: str, message: str, template?: str|null,
    history?: [...]}. ``meeting_ids`` (si trae al menos un id entero válido) tiene
    prioridad SOBRE ``fts_query`` — nunca se combinan (mismo contrato que
    core.assistant.answer_multi). 400 si no hay ni ids ni query, o si el template no
    es una de las 3 plantillas conocidas.
    """
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "mensaje vacío"}), 400

    raw_ids = data.get("meeting_ids")
    meeting_ids = None
    if isinstance(raw_ids, list) and raw_ids:
        cleaned = []
        for x in raw_ids:
            try:
                cleaned.append(int(x))
            except (TypeError, ValueError):
                continue
        meeting_ids = cleaned or None

    fts_query = (data.get("fts_query") or "").strip() or None
    if meeting_ids:
        fts_query = None  # ids explícitos ganan siempre (nunca se combinan)

    if not meeting_ids and not fts_query:
        return jsonify({"error": "indica meeting_ids o fts_query"}), 400

    template = data.get("template")
    if template is not None:
        template = str(template).strip() or None
    if template is not None and template not in _assistant.DELIVERABLE_TEMPLATES:
        return jsonify({"error": f"plantilla desconocida: {template}"}), 400

    history = data.get("history") or []
    result = _assistant.answer_multi(_db, message, meeting_ids=meeting_ids, fts_query=fts_query,
                                     history=history, template=template)
    if not result.get("ok"):
        return jsonify({"error": result.get("error", "error")}), 503
    return jsonify(result)


def _deliverable_slug(titulo: str) -> str:
    """Slug ASCII corto para el nombre del archivo exportado (sin acentos/espacios)."""
    text = (titulo or "").strip().lower()
    text = _unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if _unicodedata.category(c) != "Mn")
    text = _re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60] or "entregable"


@bp.route("/api/meetings/deliverable-export", methods=["POST"])
def meetings_deliverable_export():
    """Exporta el texto de un entregable (respuesta del chat multi-reunión) a un .md.

    Escribe a ``<PENDING_EXPORT_DIR>/entregables/vflow-entregable-<yyyymmdd-hhmm>-<slug>.md``
    — SUBCARPETA ``entregables/`` y prefijo ``vflow-entregable-``, JAMÁS
    ``vflow-pendientes-`` (ese naming es el contrato de tareas del dead-drop de
    core/webhook.py y lo vigila un consumidor externo; mezclar prefijos rompería ese
    contrato). 400 si no hay contenido o si PENDING_EXPORT_DIR no está configurado
    (reutiliza la misma variable/validación que el dead-drop de pendientes, unidad 5.4 —
    sin flag nuevo). Acción explícita del usuario: un error de I/O es 500, no fail-open.
    """
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "contenido vacío"}), 400

    export_dir = _os.getenv("PENDING_EXPORT_DIR", "").strip()
    if not export_dir:
        return jsonify({"error": "Configura la carpeta de exportación en Ajustes."}), 400

    titulo = (data.get("titulo") or "").strip()
    slug = _deliverable_slug(titulo)
    stamp = _time.strftime("%Y%m%d-%H%M")

    try:
        target_dir = _os.path.join(export_dir, "entregables")
        _os.makedirs(target_dir, exist_ok=True)
        fname = f"vflow-entregable-{stamp}-{slug}.md"
        path = _os.path.join(target_dir, fname)
        n = 1
        while _os.path.exists(path):
            n += 1
            path = _os.path.join(target_dir, f"vflow-entregable-{stamp}-{slug}-{n}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return jsonify({"ok": True, "path": path})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 500
