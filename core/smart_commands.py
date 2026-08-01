"""Smart commands: voz -> puntuación (unidad 1a, Ola 1 de PLAN-DICTADO-2026-07-31).

Pasada 3 del contrato del pipeline de texto (`CLAUDE.md` sección 19, "Contrato del
pipeline de texto"). Corre sobre el texto YA ENSAMBLADO de un dictado (después del
join de chunks), nunca por chunk y nunca dentro de `core/transcriber.py` (eso violaría
el Eje 2 del contrato: reunión/URL heredarían la pasada por construcción y meterían
puntuación inventada en habla de terceros). El cableado en `main.py` es la unidad 1b,
fuera de esta unidad.

Regex puro, local, sin red ni I/O de disco — presupuesto duro de 5 ms sobre un texto
de ~5.000 caracteres (Eje 3 del contrato). Los patrones se compilan UNA vez a nivel de
módulo, nunca dentro de `apply_smart_commands`.

Decisión de Johann, no re-litigar: los disparadores llevan PREFIJO obligatorio
("signo"/"signos" en español, "symbol" en inglés) delante de la palabra de comando
("signo coma", nunca "coma" pelada). Medido: el upstream (`daniel-carreon/sflow`)
dispara con la palabra pelada y una guardia `(?<=\\w)\\s+` insuficiente — "hay dos
puntos importantes" se convertía en "hay: importantes" y "entró en coma profundo" en
"entró en, profundo". El prefijo casi elimina esos falsos positivos y, de regalo,
deja dictar la palabra literal ("la palabra coma se escribe así" sale intacta, porque
no lleva el prefijo delante).

**"puntuación"/"puntuacion"/"punctuation" NO son prefijos válidos — decisión del
director, 2026-07-31, no revertir por descuido.** La primera versión de este módulo
sí los incluía (venían de un paréntesis del plan, no de Johann: en su decisión
original solo usó "signo"). Se quitaron porque reintroducen exactamente el problema
que el prefijo existe para matar: "puntuación" tiene significado genérico real en
español ("revisemos la puntuación coma por coma", dicho sin intención de insertar
nada, dispara igual que si llevara el prefijo de verdad). "signo coma" no es habla
natural en NINGÚN contexto — por eso funciona como prefijo y "puntuación" no. Quitarlo
no cuesta ninguna capacidad: todo lo que se podía decir con "puntuación X" se puede
decir con "signo X". Si algún día hace falta, reintroducirlo es una línea en
`_PREFIX_PATTERN`, pero hay que traer también de vuelta el caso negativo que lo mató
(ver `TestNegativosSinPrefijo` en `tests/test_smart_commands.py`, caso
"revisemos la puntuación coma por coma").

Tabla de comandos (ver `apply_smart_commands`): el orden de evaluación es por CANTIDAD
DE PALABRAS de la frase descendente (`_COMMANDS` se ordena así al compilar), no el
orden en que están escritas aquí — así "punto y aparte"/"punto y coma" (3 palabras)
siempre se evalúan antes que "punto" (1 palabra), y "punto" nunca se come la frase
larga dejando suelto un " y aparte"/" y coma".

FUERA DE ALCANCE en v1 (decidido, no agregar sin que Johann lo re-abra):

- **Signos de interrogación/exclamación.** En español necesitan apertura Y cierre
  (¿…?, ¡…!), y decidir DÓNDE va la apertura a partir de una frase dictada en el medio
  del flujo es un problema de diseño propio (¿se abre al inicio de la frase actual? ¿al
  inicio del dictado completo? ¿el usuario dicta "signo interrogación" dos veces, una
  para abrir y otra para cerrar?). Ninguna de esas respuestas es obvia, así que no se
  adivina una; si en el uso real hace falta, es una unidad barata aparte con su propio
  diseño explícito.
- **Capitalizar la letra siguiente tras un punto o un salto.** Whisper ya devuelve las
  frases capitalizadas (usa mayúscula inicial de oración por su cuenta); una segunda
  pasada que adivine dónde va la mayúscula después de CADA punto/salto que esta unidad
  inserta puede pisar una capitalización que Whisper ya acertó, o capitalizar en medio
  de una abreviatura/sigla que el usuario dictó a propósito en minúscula. Más daño que
  beneficio para el caso común.

Ambas exclusiones están documentadas aquí, no omitidas en silencio: si se detecta que
hacen falta, son unidades nuevas, no un parche sobre este módulo.
"""
import os
import re

# ---------------------------------------------------------------------------
# Prefijo obligatorio (decisión de Johann + decisión del director, ver docstring
# del módulo). Solo `signos?` (singular/plural) en español y `symbol` en inglés.
# "puntuación"/"puntuacion"/"punctuation" quedaron FUERA a propósito: no los agregues
# de vuelta sin leer el docstring del módulo primero. Case-insensitive se aplica al
# compilar la regla.
# ---------------------------------------------------------------------------
_PREFIX_PATTERN = r"(?:signos?|symbol)"

# ---------------------------------------------------------------------------
# Tabla de comandos: (frase dicha TRAS el prefijo, texto que produce).
# Español e inglés en una sola tabla — el prefijo ya filtra los falsos positivos,
# así que no hace falta separarlas por idioma para evaluarlas con seguridad.
# ---------------------------------------------------------------------------
_COMMANDS: list[tuple[str, str]] = [
    # Español
    ("punto y aparte", ".\n\n"),
    ("nuevo párrafo", "\n\n"),
    ("nuevo parrafo", "\n\n"),
    ("nueva línea", "\n"),
    ("nueva linea", "\n"),
    ("salto de línea", "\n"),
    ("salto de linea", "\n"),
    ("punto y coma", "; "),
    ("dos puntos", ": "),
    ("puntos suspensivos", "… "),
    ("coma", ", "),
    ("punto", ". "),
    # Inglés
    ("new paragraph", "\n\n"),
    ("new line", "\n"),
    ("semicolon", "; "),
    ("colon", ": "),
    ("ellipsis", "… "),
    ("comma", ", "),
    ("period", ". "),
    ("full stop", ". "),
]


# ---------------------------------------------------------------------------
# UN solo regex combinado, no 20 pasadas secuenciales. Con 20 patrones aplicados
# uno tras otro (`re.sub` por regla, cada uno escaneando el texto completo) el
# presupuesto de 5ms del Eje 3 quedaba al límite (~4.8ms medianos, picos por
# encima de 5ms sobre ~5.000 caracteres) — 20 escaneos O(n) del mismo texto en
# vez de uno solo. Con una sola alternancia y un callback de reemplazo, el texto
# se recorre UNA vez.
#
# Orden de las alternativas dentro del regex: por CANTIDAD DE PALABRAS
# descendente (sort estable, conserva el orden de `_COMMANDS` entre frases con
# el mismo número de palabras). `re` con alternancia `|` toma la PRIMERA
# alternativa que matchea en esa posición, no la más larga, así que el orden
# es lo que garantiza que "punto y aparte"/"punto y coma" (3 palabras) ganen
# sobre "punto" (1 palabra) — ver docstring del módulo.
# ---------------------------------------------------------------------------
_ORDERED_PHRASES = sorted(
    {phrase for phrase, _ in _COMMANDS}, key=lambda phrase: -len(phrase.split())
)
_REPLACEMENT_BY_PHRASE = {phrase.lower(): replacement for phrase, replacement in _COMMANDS}

_PHRASE_ALTERNATION = "|".join(
    r"\s+".join(re.escape(w) for w in phrase.split()) for phrase in _ORDERED_PHRASES
)
_COMBINED_PATTERN = re.compile(
    rf"\s*\b{_PREFIX_PATTERN}\s+(?P<phrase>{_PHRASE_ALTERNATION})\b\s*",
    re.IGNORECASE,
)
# Normaliza espacios internos de una frase capturada (el regex tolera `\s+`
# variable entre palabras) a un solo espacio, para buscarla en el diccionario.
_INTERNAL_WHITESPACE = re.compile(r"\s+")


def _replacement_for_match(match: "re.Match") -> str:
    """Resuelve el reemplazo de UN match. Corrige el borde derecho del texto aquí
    mismo (no con un `.strip()` global sobre el resultado completo — esta función es
    una transformación pura y recortar el texto final es asunto del llamador, no de
    esta pasada): varios reemplazos (". ", "; ", ": ", "… ", ", ") llevan un espacio
    de separación pensado para la palabra que viene DESPUÉS. Si el comando es lo
    ÚLTIMO del texto (`match.end() == len(match.string)`, con el `\\s*` de cierre del
    regex ya habiendo consumido cualquier espacio sobrante del original), no hay
    palabra siguiente y ese espacio queda colgando: "esto es todo signo punto" daba
    "esto es todo. " con un espacio de más al final. Se recorta SOLO el espacio de
    cierre de ESTE reemplazo cuando el match toca el borde — no toca los saltos de
    línea (`\\n`/`\\n\\n`), que si el usuario los pidió al final del dictado son una
    decisión suya, no un artefacto de formato."""
    phrase = _INTERNAL_WHITESPACE.sub(" ", match.group("phrase")).strip().lower()
    replacement = _REPLACEMENT_BY_PHRASE[phrase]
    if match.end() == len(match.string):
        replacement = replacement.rstrip(" ")
    return replacement

# Limpieza de espacios en blanco (vale la pena copiar del upstream): espacios/tabs
# antes de un salto se van, espacios/tabs después de un salto se van, y tres o más
# saltos seguidos colapsan a dos. Corre DESPUÉS de aplicar los comandos.
_TRAILING_WS_BEFORE_NEWLINE = re.compile(r"[ \t]+\n")
_LEADING_WS_AFTER_NEWLINE = re.compile(r"\n[ \t]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def apply_smart_commands(text: "str | None") -> "str | None":
    """Aplica la tabla de comandos de voz->puntuación sobre `text` YA ENSAMBLADO
    (después del join de chunks; ver Eje 1 del contrato en `CLAUDE.md` sección 19).

    Entrada vacía o None-ish: devuelve lo que entró, sin reventar (ni una regla se
    evalúa sobre algo que no es texto real)."""
    if not text:
        return text

    result = _COMBINED_PATTERN.sub(_replacement_for_match, text)

    result = _TRAILING_WS_BEFORE_NEWLINE.sub("\n", result)
    result = _LEADING_WS_AFTER_NEWLINE.sub("\n", result)
    result = _MULTI_NEWLINE.sub("\n\n", result)
    return result


def smart_commands_enabled() -> bool:
    """Lee `SMART_COMMANDS_ENABLED` con lectura PEREZOSA (se relee en cada llamada,
    para poder apagarlo sin reiniciar la app) — mismo patrón que
    `core.dictation_modes.modes_enabled()`.

    Default **ON** (a diferencia de `modes_enabled()`, que es opt-in con default
    OFF): esta pasada es regex puro y local, no manda nada a ninguna parte, y el
    error se ve dictando (CLAUDE.md sección 19, Eje 3). Por eso el ancla de la
    comparación va AL REVÉS que en `modes_enabled()`: ahí el default es "false" y
    la comparación es `== "true"`; aquí, si se copiara la misma comparación
    (`== "true"`) a secas, cualquier `.env` que NO defina la variable en el string
    exacto "true" (vacío, ausente, "1", un typo) apagaría la pasada — justo el bug
    que el default ON busca evitar. La comparación correcta es la inversa: SOLO el
    valor explícito "false" (case-insensitive, trimmed) apaga; cualquier otra cosa
    -incluida basura como "sí", "1" o una cadena vacía- deja la pasada ENCENDIDA.
    Es un fail-open deliberado: el peor caso de un valor basura es que la pasada
    quede activa cuando alguien quiso apagarla, nunca un fallo silencioso ni una
    fuga de datos (no hay red ni disco de por medio)."""
    raw = os.getenv("SMART_COMMANDS_ENABLED", "true")
    return raw.strip().lower() != "false"
