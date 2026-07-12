"""Blueprint del historial de reuniones (/api/meetings*) y el chat del Asistente de reuniones."""

import json as _json

from flask import Blueprint, jsonify, request

from config import MEETINGS_DIR
from core import assistant as _assistant
from core import insights as _insights
from core import meeting_export as _meeting_export
from web.state import MEETING, _db

bp = Blueprint("meetings", __name__)


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
    for k in ("minutes_json", "insights_json", "segments_json", "chapters_json", "metrics_json"):
        try:
            m[k.replace("_json", "")] = _json.loads(m.get(k) or "null")
        except Exception:  # noqa: BLE001
            m[k.replace("_json", "")] = None
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
