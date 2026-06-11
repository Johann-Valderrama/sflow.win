"""Cifrado por usuario usando DPAPI de Windows (crypt32.dll) sin dependencias externas.

Expone encrypt() y decrypt() para proteger la GROQ_API_KEY en disco.
El cifrado está vinculado al usuario y máquina actual — otro usuario/máquina no puede descifrar.
"""

import base64
import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Estructura DATA_BLOB usada por CryptProtectData / CryptUnprotectData
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    """Estructura DPAPI DATA_BLOB: puntero a bytes + longitud."""

    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> _DATA_BLOB:
    """Crea un _DATA_BLOB que apunta a un buffer ctypes con los datos dados."""
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


# ---------------------------------------------------------------------------
# Declaración de funciones de crypt32.dll y kernel32.dll
# ---------------------------------------------------------------------------

_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32

# CryptProtectData(pDataIn, szDataDescr, pOptionalEntropy, pvReserved,
#                  pPromptStruct, dwFlags, pDataOut) -> BOOL
_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),  # pDataIn
    wintypes.LPCWSTR,            # szDataDescr (descripción opcional, puede ser None)
    ctypes.POINTER(_DATA_BLOB),  # pOptionalEntropy
    ctypes.c_void_p,             # pvReserved
    ctypes.c_void_p,             # pPromptStruct
    wintypes.DWORD,              # dwFlags
    ctypes.POINTER(_DATA_BLOB),  # pDataOut
]
_crypt32.CryptProtectData.restype = wintypes.BOOL

# CryptUnprotectData(pDataIn, ppszDataDescr, pOptionalEntropy, pvReserved,
#                    pPromptStruct, dwFlags, pDataOut) -> BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_DATA_BLOB),   # pDataIn
    ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr (salida, puede ser None)
    ctypes.POINTER(_DATA_BLOB),   # pOptionalEntropy
    ctypes.c_void_p,              # pvReserved
    ctypes.c_void_p,              # pPromptStruct
    wintypes.DWORD,               # dwFlags
    ctypes.POINTER(_DATA_BLOB),   # pDataOut
]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL

# LocalFree para liberar el buffer que DPAPI asigna en pDataOut
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def encrypt(plaintext: str) -> str:
    """Cifra *plaintext* con DPAPI (CryptProtectData) y devuelve el resultado en base64.

    El cifrado es por usuario y máquina: solo el mismo usuario en la misma
    máquina puede descifrar el valor resultante.

    Raises:
        OSError: si CryptProtectData falla (por ejemplo, sin acceso a DPAPI).
    """
    in_blob = _blob(plaintext.encode("utf-8"))
    out_blob = _DATA_BLOB()

    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,                   # sin descripción
        None,                   # sin entropía adicional
        None,                   # pvReserved
        None,                   # sin PromptStruct
        0,                      # dwFlags = 0 → protección por usuario
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(f"CryptProtectData falló (error={_kernel32.GetLastError()})")

    try:
        encrypted_bytes = bytes(out_blob.pbData[: out_blob.cbData])
        return base64.b64encode(encrypted_bytes).decode("ascii")
    finally:
        _kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def decrypt(b64: str) -> "str | None":
    """Descifra un blob DPAPI codificado en base64.

    Devuelve el texto plano si el descifrado tiene éxito, o None si falla
    (clave de otra máquina/usuario, dato corrupto, o cualquier otra excepción).
    Nunca lanza excepciones hacia el llamador.
    """
    try:
        encrypted_bytes = base64.b64decode(b64)
    except Exception as exc:
        logger.warning("decrypt: base64 inválido — %s", exc)
        return None

    try:
        in_blob = _blob(encrypted_bytes)
        out_blob = _DATA_BLOB()

        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,                    # no necesitamos la descripción de vuelta
            None,                    # sin entropía adicional
            None,                    # pvReserved
            None,                    # sin PromptStruct
            0,                       # dwFlags = 0
            ctypes.byref(out_blob),
        )
        if not ok:
            logger.warning("decrypt: CryptUnprotectData falló — blob de otro usuario/máquina o corrupto")
            return None

        try:
            return bytes(out_blob.pbData[: out_blob.cbData]).decode("utf-8")
        finally:
            _kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
    except Exception as exc:
        logger.warning("decrypt: excepción inesperada — %s", exc)
        return None
