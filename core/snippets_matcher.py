"""Snippets: disparador -> texto guardado (unidad 4b, Ola 4 de PLAN-DICTADO-2026-07-31).

Pasada 4 del contrato del pipeline de texto (`CLAUDE.md` sección 19, "Contrato del
pipeline de texto"). Corre en `main.py`, sobre el texto YA ENSAMBLADO, JUSTO
DESPUÉS de smart commands (pasada 3, `core/smart_commands.py`) y ANTES de
`dictation_modes` (pasada 5), bajo los MISMOS gates ('not translate' y
'recorder.source != "system"'). Motivo del orden (Eje 1 del contrato): el texto
que expande un snippet es texto que el usuario ESCRIBIÓ y ya viene puntuado; si
esta pasada corriera antes que smart commands, esa pasada volvería a escanear el
texto guardado y mutilaría cualquier palabra literal que contenga (p. ej. un
snippet cuyo cuerpo diga "signo coma"). Con este orden, lo que inserta un
snippet no lo vuelve a tocar nadie.

Cableado SOLO en `main.py` y NUNCA en `core/transcriber.py`: ese módulo lo
comparten reunión (`core/meeting.py`) y URL (`core/url_transcribe.py`), que
contienen habla de OTRAS personas — un disparador dicho por un tercero en una
reunión no puede expandirse a la firma/plantilla del usuario dentro del acta.
Ver `TestAlcanceEstructural` en `tests/test_pipeline_texto.py`, que falla si
`core/transcriber.py` llega a mencionar `snippets_matcher`.

Mismo patrón de caché que `core/dictionary.py` (documentado en su docstring):
caché en memoria INMUTABLE con swap atómico bajo lock, invalidación perezosa
cada 300s, y `hit_count` incrementado en un hilo daemon fire-and-forget para no
añadir latencia al hot-path. El matcher NUNCA hace una consulta SQLite por
dictado (presupuesto duro de 15ms sobre ~5.000 caracteres, Eje 3 del contrato).

## Sin killswitch de entorno (decisión de esta unidad, pensada y justificada)

Los smart commands (Ola 1) sí llevan `SMART_COMMANDS_ENABLED`: sus reglas son
FIJAS y el usuario no las eligió, así que podían chocar con su forma de hablar
sin que él tuviera control granular sobre CUÁL regla apagar. Los snippets son
lo opuesto: no existen hasta que el usuario crea el primero, y CADA disparador
lo elige él. Dos consecuencias:

1. **Una tabla `snippets` vacía —el estado de fábrica— ya es un "apagado"
   natural.** `expand_snippets` lo detecta en la primera línea útil (caché con
   `by_first_word` vacío) y devuelve el texto sin tokenizar nada.
2. **Ya existe un apagador GRANULAR por fila**: la columna `snippets.enabled`
   (`db.database.set_snippet_enabled`, expuesto en el panel de la unidad 4c).
   Si UN disparador molesta, el usuario lo apaga a él, no a la feature entera.

Un env var global sería una segunda llave para algo que solo se activa si el
usuario metió la primera, y no resolvería nada que el apagador por fila no
resuelva ya. Si el uso real revela que hace falta un apagador de emergencia
GLOBAL, es una variable de entorno nueva — pero eso exige una entrada en
`config.ENV_CATALOG` (guardián AST en `tests/test_env_catalog.py`) y esta
unidad tiene prohibido tocar `config.py`; se decide aparte, no se cuela aquí.

## Coincidencia: frase completa, fronteras de palabra, prefijo determinista

El texto dictado se tokeniza por palabras Unicode (`\\w+`, ver `_WORD_RE`); un
disparador de una palabra nunca dispara DENTRO de una palabra más larga
("firma" no coincide dentro de "firmamos el contrato": son tokens distintos).
Cada palabra tokenizada se normaliza con `db.database.normalize_trigger` — LA
MISMA función que normaliza el disparador al guardarlo (instrucción explícita
en su docstring: "el matcher de la unidad 4b tiene que normalizar el texto
dictado con esta MISMA función antes de comparar"). Así, mayúsculas y acentos
no importan ni del lado guardado ni del lado dictado.

**Disparador que es prefijo de otro ("firma" y "firma larga"): gana el MÁS
LARGO.** Mismo principio que la tabla de comandos de `core/smart_commands.py`
("punto y aparte"/3 palabras se evalúa antes que "punto"/1 palabra, para que la
frase larga no se la coma la corta): el usuario definió ambos disparadores a
propósito, y si dictó las DOS palabras del más específico, esa es una señal más
fuerte que si solo dijo la primera. La decisión es determinista y NO depende
del orden en que SQLite devolvió las filas de `list_snippets`: al construir la
caché, cada lista de candidatos que empiezan con la misma palabra se ordena por
`(cantidad de palabras descendente, trigger_key ascendente)` — el segundo
criterio es un desempate estable que tampoco depende de IDs ni de inserción.

## Límite conocido, documentado y no en v1

Un disparador con puntuación interna (guión, coma, apóstrofe) nunca puede
coincidir de forma completa: la tokenización por `\\w+` corta ahí, así que
"co-firma" se ve como dos tokens ("co", "firma") del lado dictado pero como UNA
sola palabra del lado guardado (`trigger_key` solo colapsa espacios, no
puntuación). Los disparadores reales son frases cortas de palabras separadas
por espacios; si algún día hace falta soportar puntuación dentro de un
disparador, es una revisión aparte del tokenizador, no un parche silencioso
aquí.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from db.database import normalize_trigger

logger = logging.getLogger(__name__)

# Invalidación perezosa: mismo intervalo que core/dictionary.py.
_LAZY_RECOMPILE_SECS = 300

# Tokenización del texto dictado: palabras Unicode. Mismo criterio que
# core/dictionary.py::suggest_dictionary_pairs (_WORD_RE), reutilizado aquí
# para el mismo propósito (fronteras de palabra reales, no substring).
_WORD_RE = re.compile(r"\w+", re.UNICODE)


# ---------------------------------------------------------------------------
# Estado de la caché (inmutable una vez construido; se reemplaza atómicamente)
# ---------------------------------------------------------------------------

class _SnippetEntry:
    """Un candidato de disparador ya resuelto: palabras normalizadas + id/body."""

    __slots__ = ("words", "id", "body", "trigger_key")

    def __init__(self, words: tuple[str, ...], entry_id: int, body: str, trigger_key: str):
        self.words = words
        self.id = entry_id
        self.body = body
        self.trigger_key = trigger_key


class _SnippetCache:
    """Snapshot inmutable de los snippets ACTIVOS (enabled=1), indexado por la
    primera palabra normalizada de su disparador. Cada lista de candidatos ya
    viene ordenada más-palabras-primero (ver docstring del módulo, sección de
    prefijos) — el matcher nunca vuelve a ordenar nada en el hot-path."""

    def __init__(self, by_first_word: dict):
        self.by_first_word = by_first_word
        self.built_at = time.monotonic()


_EMPTY_CACHE = _SnippetCache({})


# ---------------------------------------------------------------------------
# Módulo-nivel: instancia global lazy + lock
# ---------------------------------------------------------------------------

_cache: _SnippetCache = _EMPTY_CACHE
_cache_lock = threading.Lock()
_db = None          # inyectado desde invalidate()/_get_db() la primera vez


def _get_db():
    """Obtiene la instancia compartida de TranscriptionDB (lazy, evita import
    circular) — mismo patrón que core/dictionary.py::_get_db."""
    global _db
    if _db is None:
        from db.database import TranscriptionDB  # noqa: PLC0415
        _db = TranscriptionDB()
    return _db


# ---------------------------------------------------------------------------
# Construcción de caché
# ---------------------------------------------------------------------------

def _build_cache() -> "_SnippetCache":
    """Lee los snippets ACTIVOS de la DB y construye el índice por primera
    palabra. `enabled_only=True` es el apagador granular por fila (ver
    docstring del módulo); un snippet con enabled=0 simplemente no entra aquí."""
    try:
        rows = _get_db().list_snippets(enabled_only=True)
    except Exception as exc:
        logger.error("snippets_matcher: error leyendo DB — %s", exc)
        return _EMPTY_CACHE

    by_first_word: dict = {}
    for row in rows:
        trigger_key = (row.get("trigger_key") or "").strip()
        words = tuple(trigger_key.split())
        if not words:
            # Fila corrupta/legada sin trigger_key normalizado: no hay nada
            # contra qué matchear, se ignora en vez de reventar el resto.
            continue
        entry = _SnippetEntry(words, row["id"], row["body"], trigger_key)
        by_first_word.setdefault(words[0], []).append(entry)

    # Orden determinista por lista: más palabras primero, trigger_key como
    # desempate estable — NUNCA depende del orden de llegada de list_snippets.
    for candidates in by_first_word.values():
        candidates.sort(key=lambda e: (-len(e.words), e.trigger_key))

    return _SnippetCache(by_first_word)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def invalidate() -> None:
    """Recompila la caché desde la DB y la reemplaza atómicamente bajo el lock.

    Misma justificación que core/dictionary.py::invalidate: la construcción se
    hace *dentro* del lock para que dos escrituras concurrentes (dos POSTs del
    panel de gestión, unidad 4c) no dejen una caché vieja pisando a una nueva."""
    global _cache
    with _cache_lock:
        new_cache = _build_cache()
        new_cache.built_at = time.monotonic()
        _cache = new_cache
    logger.debug(
        "snippets_matcher: caché invalidada (%d palabras raíz)", len(new_cache.by_first_word)
    )


def _get_or_lazy_recompile() -> "_SnippetCache":
    """Devuelve la caché actual, recompilándola si pasaron más de
    _LAZY_RECOMPILE_SECS desde la última construcción — mismo patrón que
    core/dictionary.py::_get_or_lazy_recompile (double-check bajo lock)."""
    global _cache
    with _cache_lock:
        cache = _cache
    if (time.monotonic() - cache.built_at) > _LAZY_RECOMPILE_SECS:
        with _cache_lock:
            if (time.monotonic() - _cache.built_at) > _LAZY_RECOMPILE_SECS:
                _cache = _build_cache()
            cache = _cache
    return cache


def expand_snippets(text: "str | None") -> "str | None":
    """Expande disparadores de snippet en `text` (YA ENSAMBLADO, después de
    smart commands — ver Eje 1 del contrato en el docstring del módulo).

    Entrada vacía/None-ish: se devuelve tal cual, sin evaluar nada. Sin
    snippets activos (tabla vacía o todos con enabled=0): se devuelve el texto
    intacto SIN tokenizar (guardián de latencia barato para el caso de
    fábrica). Coincidencias no solapadas, izquierda a derecha, más-larga-gana
    en cada posición de inicio (ver docstring del módulo)."""
    if not text:
        return text

    cache = _get_or_lazy_recompile()
    if not cache.by_first_word:
        return text

    tokens = list(_WORD_RE.finditer(text))
    if not tokens:
        return text

    normalized = [normalize_trigger(m.group()) for m in tokens]

    pieces: list[str] = []
    matched_ids: list[int] = []
    last_end = 0
    i = 0
    total_tokens = len(tokens)
    matched_any = False

    while i < total_tokens:
        candidates = cache.by_first_word.get(normalized[i])
        chosen = None
        if candidates:
            for entry in candidates:
                word_count = len(entry.words)
                end_i = i + word_count
                if end_i <= total_tokens and tuple(normalized[i:end_i]) == entry.words:
                    chosen = (entry, end_i)
                    break
        if chosen is not None:
            entry, end_i = chosen
            start_char = tokens[i].start()
            end_char = tokens[end_i - 1].end()
            pieces.append(text[last_end:start_char])
            pieces.append(entry.body)
            last_end = end_char
            matched_ids.append(entry.id)
            matched_any = True
            i = end_i
        else:
            i += 1

    if not matched_any:
        return text

    pieces.append(text[last_end:])
    result = "".join(pieces)

    # Incrementar hit_count en background (fire-and-forget), mismo patrón que
    # core/dictionary.py::apply_replacements: nunca bloquear el hot-path por
    # una escritura de contador.
    if matched_ids:
        ids_snapshot = list(matched_ids)

        def _inc_hits():
            try:
                from collections import Counter  # noqa: PLC0415
                counts = Counter(ids_snapshot)
                db = _get_db()
                for eid, occurrences in counts.items():
                    for _ in range(occurrences):
                        db.increment_snippet_hits([eid])
            except Exception as exc:
                logger.debug("snippets_matcher: error incrementando hit_count — %s", exc)

        t = threading.Thread(target=_inc_hits, daemon=True)
        t.start()

    return result
