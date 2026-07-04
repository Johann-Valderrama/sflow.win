"""Briefing de contexto del usuario (Ola 7, unidad 7.1 — copiloto con contexto OPS).

Feature opt-in: un archivo .md externo, curado por el usuario (proyectos activos,
compromisos, metas), cuyo contenido se inyecta SOLO en el chat en vivo "Preguntar"
(core/assistant.py answer_live/build_context_live) para que el copiloto conecte lo
hablado en la reunión con ese contexto. Apagado por default (OPS_BRIEFING_PATH vacío).

Alcance v1 (decisión ya cerrada, no se re-litiga aquí): SOLO chat pull. El insight
stream (core/insights.py) y las tarjetas proactivas NO leen este módulo — eso es
backlog v1.1.

Diseño (cada punto viene de un side-case real):
  - Caché a nivel módulo bajo un lock, TTL 60s por time.monotonic() (NUNCA mtime:
    resolución NTFS ~1s + TOCTOU + carreras). invalidate() fuerza recarga inmediata.
  - Concurrencia: la I/O de disco se hace FUERA del lock (el archivo puede vivir en
    una carpeta de red y colgar); el lock solo protege leer/reemplazar el snapshot.
  - Guard de tamaño en BYTES sobre el contenido REALMENTE LEÍDO (no st_size, que
    puede cambiar entre stat y read): > 8192 bytes se trata como ausente.
  - Fail-open total: cualquier excepción (ausente, directorio, symlink roto, permiso,
    no-UTF8) devuelve "" sin propagar.
  - Privacidad dura: este módulo NUNCA loguea el CONTENIDO del briefing, solo el path
    y metadatos (tamaño). Ni en warning, ni en debug, ni en excepción.
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# TTL de la caché en memoria (segundos, time.monotonic()).
_TTL_SECONDS = 60.0

# Tope de tamaño del archivo de briefing, en bytes del contenido leído.
_MAX_BYTES = 8192

_DELIMITER_HEADER = (
    "CONTEXTO DEL USUARIO (briefing OPS) — úsalo solo para conectar lo hablado "
    "con los proyectos/compromisos del usuario; nunca inventes hechos que no "
    "vengan al caso:"
)


class _BriefingCache:
    """Snapshot inmutable: (path_normalizado, bloque_formateado, built_at_monotonic)."""

    __slots__ = ("path", "block", "built_at")

    def __init__(self, path: str, block: str, built_at: float):
        self.path = path
        self.block = block
        self.built_at = built_at


_EMPTY_CACHE = _BriefingCache("", "", 0.0)

_cache: _BriefingCache = _EMPTY_CACHE
_cache_lock = threading.Lock()


def _resolve_path() -> str:
    """Lee OPS_BRIEFING_PATH del entorno, tolerante a comillas/espacios de dotenv."""
    raw = os.getenv("OPS_BRIEFING_PATH", "").strip().strip('"').strip("'")
    if not raw:
        return ""
    return os.path.normpath(raw)


def _format_block(content: str) -> str:
    return f"{_DELIMITER_HEADER}\n<<<BRIEFING\n{content}\n>>>"


def _read_briefing(path: str) -> str:
    """Lee y formatea el archivo de briefing. Fail-open total: "" ante cualquier
    problema. NUNCA loguea el contenido, solo el path y metadatos."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            raw_bytes = f.read(_MAX_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 — ausente/permiso/directorio/etc.
        logger.debug("ops_briefing: no se pudo leer %r (%s)", path, type(exc).__name__)
        return ""

    if len(raw_bytes) > _MAX_BYTES:
        logger.warning(
            "ops_briefing: archivo %r excede el límite de %d bytes; se ignora.",
            path, _MAX_BYTES,
        )
        return ""

    try:
        text = raw_bytes.decode("utf-8-sig")
    except Exception as exc:  # noqa: BLE001 — bytes no-UTF8
        logger.debug("ops_briefing: %r no es UTF-8 válido (%s)", path, type(exc).__name__)
        return ""

    text = text.strip()
    if not text:
        return ""

    return _format_block(text)


def invalidate() -> None:
    """Fuerza recarga en la próxima llamada a get_briefing()."""
    global _cache
    with _cache_lock:
        _cache = _EMPTY_CACHE


def get_briefing() -> str:
    """Devuelve el bloque de briefing ya formateado, o "" si la feature está
    apagada o el archivo no sirve (fail-open).

    La lectura de disco ocurre FUERA del lock; el lock solo protege el snapshot.
    """
    global _cache
    path = _resolve_path()
    if not path:
        # Feature apagada: silencioso, sin tocar la caché ni loguear.
        return ""

    with _cache_lock:
        cache = _cache

    now = time.monotonic()
    if cache.path == path and (now - cache.built_at) <= _TTL_SECONDS:
        return cache.block

    # I/O de disco fuera del lock (puede ser una carpeta de red y colgar).
    block = _read_briefing(path)
    built_at = time.monotonic()

    with _cache_lock:
        # Última escritura gana; suficiente aquí (el peor caso es una relectura
        # extra dentro del TTL, nunca un dato corrupto).
        _cache = _BriefingCache(path, block, built_at)

    return block
