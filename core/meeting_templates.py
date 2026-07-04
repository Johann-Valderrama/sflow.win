"""Plantillas por tipo de reunión (unidad 4.3, Ola 4).

EXACTAMENTE 4 plantillas fijas (general, ventas, one_on_one, clase). Cada una
moldea dos superficies del modo reunión:

  - ``acta_extra``: bloque adicional que se añade al FINAL del system prompt
    de ``core.insights.generate_minutes`` (énfasis de la reunión + claves
    condicionales como "bant" en ventas). Nunca reemplaza el prompt base.
  - ``chips``: 3 sugerencias del chat en vivo (pestaña Preguntar de /reunion),
    reemplazan la const fija ``ASST_CHIPS_LIVE`` cuando hay una reunión activa
    con esta plantilla.
  - ``live_extra``: 1-2 líneas que se añaden al system prompt del chat en vivo
    (``core.assistant._SYSTEM_LIVE``) para orientar el rol del copiloto.

CORRECCIÓN DE DEBATE: el template activo vive SOLO en el singleton MEETING
(proceso), nunca en localStorage del navegador — así el hotkey AltGr+R y el
dropdown del dashboard ven la misma verdad.

Prompts compactos (<600 tokens, ~2400 chars) y en español.
"""

DEFAULT_TEMPLATE = "general"

TEMPLATES: dict = {
    "general": {
        "label": "General",
        "acta_extra": "",
        "chips": [
            "¿Puntos clave hasta ahora?",
            "¿Qué me falta preguntar?",
            "Pendientes y responsables",
        ],
        "live_extra": "",
    },
    "ventas": {
        "label": "Ventas",
        "acta_extra": (
            "ÉNFASIS DE ESTA REUNIÓN (plantilla ventas): es una reunión comercial. "
            "Presta especial atención a las OBJECIONES planteadas por el cliente y a los "
            "PRÓXIMOS PASOS de cierre (quién hace qué para avanzar el trato). Además de las "
            "claves habituales, si detectas evidencia EXPLÍCITA de calificación BANT, añade "
            "la clave adicional:\n"
            '  "bant": objeto {"budget": string, "authority": string, "need": string, '
            '"timeline": string} — cada campo es una frase breve con lo detectado '
            "textualmente en la conversación, o cadena vacía \"\" si no se mencionó. "
            "NUNCA inventes un campo BANT sin evidencia explícita; si NINGÚN campo tiene "
            "evidencia, omite la clave \"bant\" por completo (no la incluyas vacía)."
        ),
        "chips": [
            "¿Qué objeciones han salido?",
            "¿Cómo va el BANT?",
            "¿Cuál es el próximo paso de cierre?",
        ],
        "live_extra": (
            "Actúas como copiloto de venta: prioriza detectar objeciones del cliente, "
            "señales de presupuesto/autoridad/necesidad/tiempo (BANT) y el próximo paso de cierre."
        ),
    },
    "one_on_one": {
        "label": "1:1",
        "acta_extra": (
            "ÉNFASIS DE ESTA REUNIÓN (plantilla 1:1): es una reunión individual entre un "
            "responsable y una persona de su equipo. Presta especial atención a los ACUERDOS "
            "PERSONALES tomados (compromisos concretos de cada uno), al FEEDBACK dado y "
            "recibido (qué se dijo, en qué dirección) y a los TEMAS DE CARRERA/desarrollo "
            "tratados (crecimiento, objetivos, inquietudes). No se añaden claves nuevas al "
            "formato de acta."
        ),
        "chips": [
            "¿Qué acordamos cada uno?",
            "¿Qué feedback salió?",
            "¿Temas para la próxima 1:1?",
        ],
        "live_extra": (
            "Esta es una reunión 1:1: presta especial atención a acuerdos personales, "
            "feedback dado/recibido y temas de carrera."
        ),
    },
    "clase": {
        "label": "Clase",
        "acta_extra": (
            "ÉNFASIS DE ESTA REUNIÓN (plantilla clase): es una sesión educativa/formativa. "
            "Presta especial atención a los CONCEPTOS CLAVE explicados (con su definición o "
            "explicación tal como se dio), a los EJEMPLOS concretos usados para ilustrarlos y "
            "a las DUDAS que quedaron abiertas o sin resolver. No se añaden claves nuevas al "
            "formato de acta."
        ),
        "chips": [
            "¿Conceptos clave hasta ahora?",
            "¿Qué ejemplos se dieron?",
            "¿Qué dudas quedaron abiertas?",
        ],
        "live_extra": (
            "Esta es una sesión educativa: presta especial atención a conceptos clave, "
            "ejemplos dados y dudas abiertas."
        ),
    },
}


def is_valid(name: str) -> bool:
    """¿``name`` es una plantilla válida (una de las 4 claves de TEMPLATES)?"""
    return name in TEMPLATES


def get(name: str) -> dict:
    """Devuelve la plantilla ``name``, o la plantilla ``general`` si no es válida."""
    return TEMPLATES.get(name, TEMPLATES[DEFAULT_TEMPLATE])


def chips_map() -> dict:
    """Mapa {template_name: [chip1, chip2, chip3]} para generar el JS del dashboard
    sin duplicar los textos a mano (ver web/server.py, const inyectada desde Python)."""
    return {name: list(tpl["chips"]) for name, tpl in TEMPLATES.items()}
