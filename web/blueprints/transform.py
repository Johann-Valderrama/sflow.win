"""Blueprint del panel de Transform: los 8 prompts y su advertencia (unidad 3d).

Consume ``core/transform.py`` (unidad 3b) y no reimplementa ninguna regla: la
validación de qué prompt existe, el merge con los editados por el usuario y el
guardado viven allá. Mismo principio que ``web/blueprints/snippets.py``.

Lo que este panel tiene que decir SIN eufemismos (obligación escrita en la
unidad 3z, y sin ella la ola incumple su juicio aunque el código funcione):
Transform manda el texto que selecciones a un modelo de lenguaje, y por defecto
ese modelo corre en la nube. El aviso lee el backend REALMENTE configurado, no
uno supuesto — mismo patrón que ya construyó la unidad 2c para los modos de
dictado, que es la razón de que ``GET /api/transform/prompts`` devuelva también
el bloque ``aviso``.
"""

import os

from flask import Blueprint, jsonify, request

from core import transform as _transform

bp = Blueprint("transform", __name__)


def _aviso() -> dict:
    """Estado REAL del envío: a qué backend va el texto y si el modo local es de fiar.

    Se calcula aquí y no en el front para que la advertencia no pueda quedarse
    desactualizada respecto de la configuración (el front pinta lo que reciba).
    """
    from core import insights as _insights

    backend = _insights._resolve_backend("batch")
    en_la_nube = backend not in ("endpoint",)
    aviso = {
        "backend": backend,
        "en_la_nube": en_la_nube,
        "texto": (
            "Transform manda el texto que tengas seleccionado a un modelo de lenguaje."
        ),
        "problema": None,
    }
    if en_la_nube:
        aviso["detalle"] = (
            f"Hoy ese modelo corre en la nube (backend «{backend}», el mismo de "
            "«Acta + Asistente de reuniones»). El texto seleccionado sale de tu equipo."
        )
    else:
        aviso["detalle"] = (
            "Hoy ese modelo corre en tu equipo (servidor local), así que el texto "
            "seleccionado no sale de la máquina."
        )
        # La trampa de la objeción A2: con el respaldo encendido, "local" no
        # garantiza nada el día que el servidor no esté levantado.
        if _insights._fallback_enabled():
            aviso["problema"] = (
                "El respaldo en la nube está encendido, así que Transform se negará a "
                "funcionar en local hasta que lo apagues: si no, el día que el servidor "
                "local no responda tu texto se iría a la nube sin avisarte."
            )
    return aviso


@bp.route("/api/transform/prompts")
def get_transform_prompts():
    """Los 8 prompts (con su línea de cuándo se usa y si están editados) + el aviso."""
    return jsonify({
        "prompts": _transform.list_prompts(),
        "aviso": _aviso(),
        "hotkey": "AltGr+X",
        "target_lang": os.getenv("TRANSLATE_TARGET_LANG", "en"),
    })


@bp.route("/api/transform/prompts/<key>", methods=["PUT"])
def put_transform_prompt(key):
    """Guarda un prompt editado; cuerpo vacío o ``{"system": ""}`` vuelve al de fábrica."""
    if key not in _transform.VALID_PROMPTS:
        return jsonify({"error": "Prompt desconocido"}), 404
    data = request.get_json(silent=True) or {}
    system = data.get("system")
    if system is not None and not isinstance(system, str):
        return jsonify({"error": "El prompt tiene que ser texto"}), 400
    if not _transform.save_prompt_override(key, system):
        return jsonify({"error": "No se pudo guardar el prompt"}), 500
    return jsonify({"ok": True, "prompts": _transform.list_prompts()})
