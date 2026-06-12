"""Diccionario personal: correcciones de transcripción y vocabulario de contexto.

Mantiene una caché en memoria con regex compilado y cadena de vocabulario para
Whisper.  La caché se reconstruye con swap atómico al llamar a invalidate().
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Presupuesto máximo para la cadena de vocabulario (caracteres)
_VOCAB_MAX_CHARS = 480

# Presupuesto máximo para el prompt efectivo enviado al backend (caracteres)
# Whisper solo mira los últimos ~224 tokens; usamos ~896 chars como límite seguro.
_PROMPT_MAX_CHARS = 896

# Invalidate perezoso: si pasaron más de 5 min desde la última compilación, recompilar
_LAZY_RECOMPILE_SECS = 300


# ---------------------------------------------------------------------------
# Estado de la caché (inmutable una vez construido; se reemplaza atómicamente)
# ---------------------------------------------------------------------------

class _DictCache:
    """Snapshot inmutable del diccionario compilado."""

    def __init__(
        self,
        pattern: Optional[re.Pattern],
        replace_map: dict[str, str],
        vocab: str,
        id_map: dict[str, int],
        budget_included: list[int],
        budget_total: int,
    ):
        self.pattern = pattern            # None si no hay pares activos
        self.replace_map = replace_map   # lower(from) -> canonical replace_to
        self.vocab = vocab               # cadena de vocabulario lista para prompt
        self.id_map = id_map             # lower(from) -> id (para rastrear hits)
        self.budget_included = budget_included  # ids de entradas que entran al prompt
        self.budget_total = budget_total         # total de entradas enabled con vocab
        self.built_at = time.monotonic()


_EMPTY_CACHE = _DictCache(None, {}, "", {}, [], 0)


# ---------------------------------------------------------------------------
# Módulo-nivel: instancia global lazy + lock
# ---------------------------------------------------------------------------

_cache: _DictCache = _EMPTY_CACHE
_cache_lock = threading.Lock()
_db = None          # inyectado desde invalidate() la primera vez


def _get_db():
    """Obtiene la instancia compartida de TranscriptionDB (lazy, evita import circular)."""
    global _db
    if _db is None:
        from db.database import TranscriptionDB  # noqa: PLC0415
        _db = TranscriptionDB()
    return _db


# ---------------------------------------------------------------------------
# Construcción de caché
# ---------------------------------------------------------------------------

def _build_cache() -> _DictCache:
    """Lee el diccionario de la DB y construye el snapshot de caché."""
    try:
        entries = _get_db().list_dictionary()
    except Exception as exc:
        logger.error("dictionary: error leyendo DB — %s", exc)
        return _EMPTY_CACHE

    enabled = [e for e in entries if e.get("enabled", 1)]

    # --- Pares de reemplazo (replace_from IS NOT NULL) ---
    pairs = [e for e in enabled if e.get("replace_from")]

    replace_map: dict[str, str] = {}
    id_map: dict[str, int] = {}
    if pairs:
        # Ordenar por longitud descendente para que alternativas más largas ganen
        pairs_sorted = sorted(pairs, key=lambda e: len(e["replace_from"]), reverse=True)
        for e in pairs_sorted:
            key = e["replace_from"].lower()
            replace_map[key] = e["replace_to"]
            id_map[key] = e["id"]

        alts = [
            r"(?<!\w)" + re.escape(e["replace_from"]) + r"(?!\w)"
            for e in pairs_sorted
        ]
        try:
            pattern = re.compile("|".join(alts), re.IGNORECASE)
        except re.error as exc:
            logger.error("dictionary: error compilando regex — %s", exc)
            pattern = None
            replace_map = {}
            id_map = {}
    else:
        pattern = None

    # --- Cadena de vocabulario ---
    # Orden: primero pinned=1 (por created_at desc), luego el resto por hit_count desc, created_at desc
    # list_dictionary ya devuelve: ORDER BY pinned DESC, hit_count DESC, created_at DESC
    seen: set[str] = set()
    vocab_terms: list[tuple[str, int]] = []  # (term, id)
    for e in enabled:
        term = e["replace_to"].strip()
        if term and term not in seen:
            seen.add(term)
            vocab_terms.append((term, e["id"]))

    budget_total = len(vocab_terms)

    # Truncar al presupuesto de caracteres (conservar los primeros = pinned > hits > recencia)
    vocab = ""
    budget_included: list[int] = []
    for term, eid in vocab_terms:
        candidate = (vocab + ", " + term) if vocab else term
        if len(candidate) + 1 > _VOCAB_MAX_CHARS:  # +1 para el punto final
            break
        vocab = candidate
        budget_included.append(eid)
    if vocab:
        vocab = vocab + "."

    return _DictCache(pattern, replace_map, vocab, id_map, budget_included, budget_total)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def invalidate() -> None:
    """Recompila la caché desde la DB y la reemplaza atómicamente bajo el lock.

    La construcción se hace *dentro* del lock para evitar que dos escrituras
    concurrentes (p. ej. dos POSTs simultáneos al dashboard) dejen una caché
    vieja sobreescribiendo una nueva.  La I/O de SQLite aquí es trivial y las
    escrituras son raras, por lo que el lock simple es preferible a la
    microoptimización de build-fuera / swap-dentro.
    """
    global _cache
    with _cache_lock:
        new_cache = _build_cache()
        new_cache.built_at = time.monotonic()
        _cache = new_cache
    logger.debug("dictionary: caché invalidada (%d pares, vocab=%r)", len(new_cache.replace_map), new_cache.vocab)


def _get_or_lazy_recompile() -> "_DictCache":
    """Devuelve la caché actual, recompilándola si han pasado más de _LAZY_RECOMPILE_SECS."""
    global _cache
    with _cache_lock:
        cache = _cache
    # Recompilación perezosa fuera del lock para no bloquear
    if (time.monotonic() - cache.built_at) > _LAZY_RECOMPILE_SECS:
        with _cache_lock:
            # Double-check: otro hilo puede haberla recompilado mientras esperábamos
            if (time.monotonic() - _cache.built_at) > _LAZY_RECOMPILE_SECS:
                new_cache = _build_cache()
                _cache = new_cache
            cache = _cache
    return cache


def apply_replacements(text: str) -> str:
    """Aplica los pares de reemplazo activos al texto transcrito.

    Regla de capitalización:
    - Si el primer carácter del match era mayúscula → replace_to tal cual está guardado.
    - Si era minúscula → replace_to con la inicial en minúscula.

    Los hit_count de los pares que matchearon se incrementan en un thread daemon
    (fire-and-forget) para no añadir latencia al hot path.
    """
    cache = _get_or_lazy_recompile()

    if not cache.pattern or not text:
        return text

    matched_ids: list[int] = []

    def _replacer(m: re.Match) -> str:
        matched = m.group(0)
        key = matched.lower()
        canonical = cache.replace_map.get(key, matched)
        # Rastrear id para hit_count
        eid = cache.id_map.get(key)
        if eid is not None:
            matched_ids.append(eid)
        # Capitalización: conservar caso del primer carácter del match
        if matched and matched[0].isupper():
            return canonical
        # Primer char era minúscula: asegurar que replace_to empiece en minúscula
        if canonical:
            return canonical[0].lower() + canonical[1:]
        return canonical

    result = cache.pattern.sub(_replacer, text)

    # Incrementar hit_count en background (fire-and-forget, sin invalidar caché)
    # Se pasan todos los ids (con repeticiones) para contar ocurrencias correctamente
    if matched_ids:
        ids_snapshot = list(matched_ids)  # copia para el closure

        def _inc_hits():
            try:
                # Llamar una vez por ocurrencia: agrupar por id y llamar N veces
                from collections import Counter
                counts = Counter(ids_snapshot)
                db = _get_db()
                for eid, n in counts.items():
                    for _ in range(n):
                        db.increment_dictionary_hits([eid])
            except Exception as exc:
                logger.debug("dictionary: error incrementando hit_count — %s", exc)

        t = threading.Thread(target=_inc_hits, daemon=True)
        t.start()

    return result


def build_vocab_string() -> str:
    """Devuelve la cadena de vocabulario lista para incluir en el prompt de Whisper."""
    return _get_or_lazy_recompile().vocab


def vocab_budget_info() -> dict:
    """Devuelve información sobre el presupuesto de vocabulario del prompt.

    Returns:
        {included: int, total: int, included_ids: list[int],
         used_chars: int, max_chars: int}
    """
    cache = _get_or_lazy_recompile()
    return {
        "included": len(cache.budget_included),
        "total": cache.budget_total,
        "included_ids": list(cache.budget_included),
        "used_chars": len(cache.vocab),
        "max_chars": _VOCAB_MAX_CHARS,
    }


def compose_prompt(context: Optional[str], include_vocab: bool = True) -> Optional[str]:
    """Compone el prompt efectivo para el backend de Whisper.

    Whisper trunca por la izquierda, así que el vocabulario va SIEMPRE AL FINAL.

    Args:
        context:       Texto del chunk anterior (puede ser None o vacío).
        include_vocab: Si False, no añade vocabulario (p.ej. backend local en traducción).

    Returns:
        Cadena de prompt o None si no hay nada que incluir.
    """
    vocab = build_vocab_string() if include_vocab else ""
    ctx = (context or "").strip()

    if not ctx and not vocab:
        return None

    if not vocab:
        return ctx or None

    if not ctx:
        return vocab

    # Ambos presentes: contexto + espacio + vocab, truncando el contexto por la izquierda
    separator = " "
    max_ctx = _PROMPT_MAX_CHARS - len(separator) - len(vocab)
    if max_ctx <= 0:
        # El vocab solo ya ocupa el presupuesto completo
        return vocab
    if len(ctx) > max_ctx:
        # Truncar por la izquierda; buscar el primer espacio tras el corte para
        # no partir palabras en mitad (Whisper lee mejor en límites de palabra).
        raw = ctx[-max_ctx:]
        space_pos = raw.find(" ")
        ctx = raw[space_pos + 1:] if space_pos != -1 else raw
    return ctx + separator + vocab
