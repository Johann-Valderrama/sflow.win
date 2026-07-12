"""Blueprint de dictados/historial: listar, buscar, editar y eliminar transcripciones."""

import re
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from core import dictionary as _dictionary
from web.state import _db

bp = Blueprint("transcriptions", __name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@bp.route("/api/transcriptions")
def get_transcriptions():
    """Retorna las últimas 200 transcripciones en formato JSON."""
    return jsonify(_db.get_recent(limit=200))


@bp.route("/api/stats")
def get_stats():
    """Agregados de uso para las metric cards del dashboard (read-only, indexado)."""
    return jsonify(_db.stats())


@bp.route("/api/transcriptions/search")
def search_transcriptions():
    """Búsqueda para la command palette (LIKE sobre todo el historial, no solo las 200 recientes)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    return jsonify(_db.search(q, limit=8))


@bp.route("/api/transcriptions/<int:tid>", methods=["DELETE"])
def delete_transcription(tid):
    """Elimina una transcripción individual por su ID."""
    deleted = _db.delete_by_id(tid)
    return jsonify({"deleted": deleted})


@bp.route("/api/transcriptions", methods=["DELETE"])
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


@bp.route("/api/transcriptions/delete-batch", methods=["POST"])
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


@bp.route("/api/transcriptions/<int:tid>", methods=["PUT"])
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
