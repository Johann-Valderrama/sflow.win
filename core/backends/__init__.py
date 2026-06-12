"""Factory de backends de transcripción.

Uso:
    from core.backends import get_backend
    backend = get_backend("groq")   # o get_backend() para leer la env var

La variable de entorno ``TRANSCRIPTION_BACKEND`` (por defecto ``"groq"``)
controla qué backend se instancia.  Si el nombre es desconocido se emite un
warning y se devuelve el backend Groq como fallback seguro.

Singleton por nombre
--------------------
``get_backend`` devuelve SIEMPRE la misma instancia para cada nombre de
backend (patrón singleton por clave).  Esto garantiza que el warmup del
dashboard y el ``Transcriber`` usen **exactamente** el mismo objeto
``LocalBackend``, de modo que el precalentamiento del modelo sea efectivo.

El singleton NO impide llamar a ``release()`` en la instancia: la instancia
persiste en el caché y simplemente recargará el modelo en el siguiente uso
(``_load_model`` es lazy).  Si ``Transcriber._get_backend()`` cambia de
backend, llama a ``release()`` sobre la instancia antigua (que queda en caché
para reutilizarse si se vuelve a seleccionar).
"""
import logging
import os
import threading

from core.backends.base import TranscriptionBackend

logger = logging.getLogger(__name__)

# Registro de clases disponibles.
_REGISTRY: dict[str, type[TranscriptionBackend]] = {}

# Caché singleton: nombre → instancia compartida.
_instances: dict[str, TranscriptionBackend] = {}
_instances_lock = threading.Lock()


def _register_defaults() -> None:
    """Registra los backends incluidos en el paquete (import diferido para
    evitar importar dependencias opcionales que podrían no estar instaladas)."""
    from core.backends.groq_backend import GroqBackend  # noqa: PLC0415
    _REGISTRY["groq"] = GroqBackend

    from core.backends.local_backend import LocalBackend  # noqa: PLC0415
    _REGISTRY["local"] = LocalBackend


_register_defaults()


def get_backend(name: str | None = None) -> TranscriptionBackend:
    """Devuelve la instancia singleton del backend solicitado.

    La misma instancia se reutiliza en todas las llamadas con el mismo nombre,
    de forma que el warmup del dashboard y el ``Transcriber`` comparten el
    objeto y el modelo en memoria.

    Args:
        name: Nombre del backend (p. ej. ``"groq"``).  Si es ``None`` o no se
              pasa, se lee ``TRANSCRIPTION_BACKEND`` del entorno; si la variable
              tampoco está definida se usa ``"groq"``.

    Returns:
        Instancia de :class:`TranscriptionBackend`.

    Note:
        Si ``name`` no coincide con ningún backend registrado se emite un
        warning y se devuelve el backend ``"groq"`` como fallback.
    """
    resolved = (name or os.getenv("TRANSCRIPTION_BACKEND", "groq")).strip().lower()
    backend_cls = _REGISTRY.get(resolved)
    if backend_cls is None:
        logger.warning(
            "Backend de transcripción desconocido: %r — usando 'groq' como fallback",
            resolved,
        )
        resolved = "groq"
        backend_cls = _REGISTRY["groq"]

    with _instances_lock:
        if resolved not in _instances:
            _instances[resolved] = backend_cls()
        return _instances[resolved]
