"""Token local de sesión del dashboard (auth v1).

PROBLEMA QUE CIERRA: ``web/state.py::_csrf_check`` exime por diseño
``GET``/``HEAD``/``OPTIONS``, así que cualquier proceso corriendo en la misma
máquina podía leer ``http://127.0.0.1:5678/api/transcriptions``,
``/api/meetings`` y ``/api/meetings/<id>`` con un simple ``curl``. En esta app
esos endpoints no devuelven dictados sueltos: devuelven transcripts y actas de
reuniones con otras personas.

LÍMITE CONOCIDO Y ACEPTADO (no es un pendiente, es el alcance elegido): un
proceso que corre como el MISMO usuario de Windows también puede leer
``dashboard_token.txt`` o abrir directamente la SQLite. El token sube el listón
de "curl trivial" a "hay que leer un archivo del directorio de datos"; no
pretende más y no sustituye al cifrado en reposo.

ASIMETRÍA DELIBERADA CON ``core/ops_briefing.py``: aquel módulo es fail-OPEN
total (si su archivo no se puede leer, la feature simplemente no aporta
contexto y eso es inocuo). Este es fail-CLOSED: si el token no se puede crear
ni leer (permisos, disco lleno, ruta ocupada por un directorio), ``verify()``
devuelve ``False`` y el guard DENIEGA. Abrirse ante un fallo de I/O sería
exactamente el bug que este módulo existe para cerrar.

Diseño:
  - Caché a nivel módulo bajo un ``threading.Lock``; la I/O de disco ocurre
    FUERA del lock (mismo patrón que ``core/ops_briefing.py``), el lock solo
    protege leer/reemplazar el valor cacheado.
  - Creación con ``O_CREAT | O_EXCL``: si dos procesos/hilos compiten, gana el
    que creó el archivo y el otro relee: el ARCHIVO es la fuente de verdad, no
    la caché.
  - Comparación en tiempo constante (``secrets.compare_digest``).
  - Kill-switch ``DASHBOARD_AUTH_ENABLED`` (default ``"true"``) de lectura
    PEREZOSA, igual que el resto de killswitches del proyecto: se apaga sin
    reiniciar la app.
  - Privacidad: este módulo NUNCA loguea el valor del token, solo la ruta y el
    tipo de excepción.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading

from config import APP_DATA_DIR

logger = logging.getLogger(__name__)

# Nombre del archivo dentro de APP_DATA_DIR (dev: raíz del repo; bundle:
# %APPDATA%\Vflow). Está en .gitignore junto al resto de datos de usuario.
TOKEN_FILENAME = "dashboard_token.txt"

# 32 bytes -> 64 caracteres hex. Mismo tamaño que el SECRET_KEY de Flask.
_TOKEN_BYTES = 32

_token: str = ""
_lock = threading.Lock()


def is_enabled() -> bool:
    """Kill-switch ``DASHBOARD_AUTH_ENABLED`` (default ``"true"``).

    Lectura perezosa (kind ``lazy`` en ``config.ENV_CATALOG``): se relee en cada
    request, así se apaga sin reiniciar la app. Solo el literal ``"false"``
    (case-insensitive) apaga el guard: cualquier basura en el .env deja la
    protección ENCENDIDA, que es el lado seguro del fallo.
    """
    return (os.getenv("DASHBOARD_AUTH_ENABLED", "true") or "true").strip().lower() != "false"


def _token_path() -> str:
    return os.path.join(APP_DATA_DIR, TOKEN_FILENAME)


def _restrict_permissions(path: str) -> None:
    """Permisos restrictivos, best-effort.

    En Windows ``os.chmod`` solo mueve el bit de solo-lectura (las ACL reales no
    se tocan), así que esto es un gesto barato para POSIX y un no-op efectivo en
    Windows. La protección real en Windows es que el archivo vive en el perfil
    del usuario. Un fallo aquí NO invalida el token.
    """
    try:
        os.chmod(path, 0o600)
    except Exception as exc:  # noqa: BLE001, plataforma/ACL; nunca bloquea
        logger.debug("localauth: no se pudo ajustar permisos de %r (%s)", path, type(exc).__name__)


def _read_token_file(path: str) -> str:
    """Devuelve el token del archivo, o "" si no existe / está vacío.

    Solo trata la AUSENCIA como "" (caso normal: primer arranque). Cualquier
    otro error de I/O propaga: fail-closed, el guard debe denegar, no abrirse.
    """
    try:
        with open(path, "r", encoding="ascii") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _read_or_create_token() -> str:
    """Lee el token del disco o lo crea. Propaga si no se puede ninguna de las dos."""
    path = _token_path()
    existing = _read_token_file(path)
    if existing:
        return existing

    os.makedirs(APP_DATA_DIR, exist_ok=True)
    candidate = secrets.token_hex(_TOKEN_BYTES)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Otro proceso/hilo lo creó entre el read y el open: gana el suyo.
        existing = _read_token_file(path)
        if existing:
            return existing
        # Existe pero está vacío (creación interrumpida a medias): reescribir.
        with open(path, "w", encoding="ascii") as f:
            f.write(candidate)
        _restrict_permissions(path)
        return candidate

    try:
        os.write(fd, candidate.encode("ascii"))
    finally:
        os.close(fd)
    _restrict_permissions(path)
    logger.info("localauth: token de dashboard creado en %r", path)
    return candidate


def get_token() -> str:
    """Token local de sesión: lo lee de disco o lo crea la primera vez.

    Propaga la excepción si el token no se puede resolver (fail-closed: quien
    llama decide, y el guard de ``web/state.py`` traduce eso en un 401).
    La I/O de disco ocurre FUERA del lock.
    """
    global _token
    with _lock:
        cached = _token
    if cached:
        return cached

    token = _read_or_create_token()

    with _lock:
        # El primero que llegue fija el valor del proceso; los demás lo adoptan
        # (el archivo ya es la fuente de verdad, así que serán idénticos).
        if not _token:
            _token = token
        return _token


def verify(candidate: str | None) -> bool:
    """Compara ``candidate`` con el token en tiempo constante.

    ``False`` ante ``None``, vacío, o si el token no se pudo resolver
    (fail-closed).
    """
    if not candidate:
        return False
    try:
        expected = get_token()
    except Exception as exc:  # noqa: BLE001, permisos/disco: se DENIEGA, no se abre
        logger.error(
            "localauth: no se pudo resolver el token (%s); se deniega el acceso.",
            type(exc).__name__,
        )
        return False
    if not expected:
        return False
    return secrets.compare_digest(str(candidate), expected)


def invalidate() -> None:
    """Olvida el token cacheado; la próxima llamada vuelve a leer el disco."""
    global _token
    with _lock:
        _token = ""
