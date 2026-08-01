"""Blueprint del panel de snippets: alta, edición, borrado, activar/desactivar y listado.

Unidad 4c de PLAN-DICTADO-2026-07-31 (Ola 4). Consume el CRUD ya escrito en
``db/database.py`` (unidad 4a): ``list_snippets``, ``add_snippet``,
``update_snippet``, ``set_snippet_enabled``, ``delete_snippet`` y la excepción
``DuplicateTriggerError``. Este blueprint NO reimplementa ninguna validación:
toda regla (disparador/body vacío, tope de tamaño, unicidad del disparador
normalizado) vive en el CRUD, este módulo solo la traduce a HTTP — mismo
principio que dejó escrito la unidad 4a ("resuélvelo en el ESQUEMA/CRUD, no
con disciplina del llamador"), y mismo patrón de blueprint que
``web/blueprints/dictionary.py``.

Tras CUALQUIER escritura (alta/edición/borrado/toggle) invalida la caché del
matcher (``core.snippets_matcher.invalidate()``) para que el siguiente
dictado vea el cambio de inmediato — mismo patrón que ``dictionary.py`` con
``core.dictionary.invalidate()``.

Prohibido tocar ``main.py``/``core/``/``db/database.py``/``config.py`` desde
esta unidad (así lo delimitó el orquestador): si el CRUD necesitara algo más,
se reporta, no se agrega aquí.
"""

from flask import Blueprint, jsonify, request

from core import snippets_matcher as _snippets_matcher
from db.database import DuplicateTriggerError
from web.state import _db

bp = Blueprint("snippets", __name__)


@bp.route("/api/snippets")
def get_snippets():
    """Todos los snippets, incluidos los apagados: el panel los lista y permite reactivarlos
    (mismo criterio que ``GET /api/dictionary``; el matcher en caliente usa
    ``list_snippets(enabled_only=True)`` por su cuenta, no este endpoint)."""
    return jsonify({"snippets": _db.list_snippets()})


@bp.route("/api/snippets", methods=["POST"])
def add_snippet():
    """Crea un snippet. 400 si el disparador/cuerpo son inválidos, 409 si el
    disparador normalizado ya existe (mensaje listo para mostrar al usuario,
    ver ``DuplicateTriggerError``)."""
    data = request.get_json(silent=True) or {}
    try:
        snippet_id = _db.add_snippet(data.get("trigger", ""), data.get("body", ""))
    except DuplicateTriggerError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    _snippets_matcher.invalidate()
    return jsonify({"id": snippet_id}), 201


@bp.route("/api/snippets/<int:sid>", methods=["PUT"])
def edit_snippet(sid):
    """Edita disparador y/o cuerpo de un snippet existente. Mismas 400/409 que el alta."""
    data = request.get_json(silent=True) or {}
    if "trigger" not in data and "body" not in data:
        return jsonify({"error": "trigger o body requerido"}), 400
    try:
        updated = _db.update_snippet(sid, trigger=data.get("trigger"), body=data.get("body"))
    except DuplicateTriggerError as exc:
        return jsonify({"error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if updated == 0:
        return jsonify({"error": "not found"}), 404
    _snippets_matcher.invalidate()
    return jsonify({"ok": True})


@bp.route("/api/snippets/<int:sid>", methods=["PATCH"])
def toggle_snippet(sid):
    """Activa o desactiva un snippet sin borrarlo (apagador granular por fila)."""
    data = request.get_json(silent=True)
    if data is None or "enabled" not in data:
        return jsonify({"error": "enabled field required"}), 400
    updated = _db.set_snippet_enabled(sid, bool(data["enabled"]))
    if updated == 0:
        return jsonify({"error": "not found"}), 404
    _snippets_matcher.invalidate()
    return jsonify({"ok": True})


@bp.route("/api/snippets/<int:sid>", methods=["DELETE"])
def delete_snippet(sid):
    """Elimina un snippet por id."""
    deleted = _db.delete_snippet(sid)
    if deleted == 0:
        return jsonify({"error": "not found"}), 404
    _snippets_matcher.invalidate()
    return "", 204
