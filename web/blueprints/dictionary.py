"""Blueprint del diccionario personal: entradas, sugeridas, export/import CSV."""

from flask import Blueprint, current_app, jsonify, request

from core import dictionary as _dictionary
from web.state import _db

bp = Blueprint("dictionary", __name__)


@bp.route("/api/dictionary")
def get_dictionary():
    """Retorna todas las entradas del diccionario personal, incluyendo budget de vocabulario."""
    entries = _db.list_dictionary()
    budget = _dictionary.vocab_budget_info()
    return jsonify({"entries": entries, "budget": budget})


@bp.route("/api/dictionary/suggested")
def get_suggested_dictionary():
    """Bandeja de revisión: entradas sugeridas (source='suggested') pendientes de aceptar/descartar."""
    return jsonify({"entries": _db.list_suggested_dictionary()})


@bp.route("/api/dictionary/suggested/<int:eid>/accept", methods=["POST"])
def accept_suggested_dictionary(eid):
    """Acepta una sugerencia: enabled=1, source pasa a 'manual'."""
    updated = _db.accept_suggested_entry(eid)
    if updated == 0:
        return jsonify({"error": "not found"}), 404
    _dictionary.invalidate()
    return jsonify({"ok": True})


@bp.route("/api/dictionary/suggested/<int:eid>", methods=["DELETE"])
def discard_suggested_dictionary(eid):
    """Descarta una sugerencia (DELETE directo; no toca entradas manuales)."""
    deleted = _db.delete_dictionary_entry(eid)
    if deleted == 0:
        return jsonify({"error": "not found"}), 404
    return "", 204


@bp.route("/api/dictionary/export")
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
    return current_app.response_class(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=vflow-diccionario.csv"},
    )


@bp.route("/api/dictionary/import", methods=["POST"])
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


@bp.route("/api/dictionary", methods=["POST"])
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


@bp.route("/api/dictionary/<int:eid>", methods=["DELETE"])
def delete_dictionary_entry(eid):
    """Elimina una entrada del diccionario por ID."""
    deleted = _db.delete_dictionary_entry(eid)
    if deleted == 0:
        return jsonify({"error": "not found"}), 404
    _dictionary.invalidate()
    return "", 204


@bp.route("/api/dictionary/<int:eid>", methods=["PATCH"])
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
