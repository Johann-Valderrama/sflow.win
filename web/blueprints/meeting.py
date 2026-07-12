"""Blueprint del modo reunión en vivo (captura dual mic + loopback): /api/meeting*."""

from flask import Blueprint, jsonify, request

from web.state import MEETING

bp = Blueprint("meeting", __name__)


@bp.route("/api/meeting", methods=["GET"])
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


@bp.route("/api/meeting/start", methods=["POST"])
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


@bp.route("/api/meeting/template", methods=["POST"])
def meeting_set_template():
    """Cambia la plantilla activa (general/ventas/one_on_one/clase, unidad 4.3).

    Única fuente de verdad en el servidor (MEETING._template): el hotkey AltGr+R
    (proceso Python) y el dropdown del dashboard ven siempre el mismo estado.
    """
    data = request.get_json(silent=True) or {}
    name = str(data.get("template", "")).strip()
    ok = MEETING.set_template(name)
    return jsonify({"ok": ok, "template": MEETING.get_template()}), (200 if ok else 400)


@bp.route("/api/meeting/stop", methods=["POST"])
def meeting_stop():
    """Detiene la reunión, persiste el acta y devuelve el transcript final."""
    res = MEETING.stop()
    return jsonify(res)


@bp.route("/api/meeting/highlight", methods=["POST"])
def meeting_highlight():
    """Marca el instante actual como momento destacado (equivalente web de AltGr+H)."""
    item = MEETING.add_highlight()
    return jsonify({"ok": item is not None, "item": item})


@bp.route("/api/meeting/note", methods=["POST"])
def meeting_note():
    """Añade una nota rápida al instante actual de la reunión."""
    data = request.get_json(silent=True) or {}
    item = MEETING.add_note(data.get("text", ""))
    return jsonify({"ok": item is not None, "item": item})


@bp.route("/api/meeting/feedback", methods=["POST"])
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


@bp.route("/api/meeting/pause", methods=["POST"])
def meeting_pause():
    """Pausa la captura de la reunión (congela el reloj)."""
    res = MEETING.pause()
    return jsonify(res)


@bp.route("/api/meeting/resume", methods=["POST"])
def meeting_resume():
    """Reanuda la captura de la reunión tras una pausa."""
    res = MEETING.resume()
    return jsonify(res)
