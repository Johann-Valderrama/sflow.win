import ctypes
import ctypes.wintypes
import os
import time
import logging

from pynput.keyboard import Controller, Key

logger = logging.getLogger(__name__)

_saved_hwnd = None

# ---------------------------------------------------------------------------
# Win32 API type annotations (critical for 64-bit Windows)
# Without these, ctypes defaults to c_int (32-bit) for return values,
# truncating 64-bit HANDLE/HWND/HGLOBAL pointers.
# ---------------------------------------------------------------------------
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
_user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
_user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
_user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
_user32.IsWindow.restype = ctypes.wintypes.BOOL
_user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
_user32.OpenClipboard.restype = ctypes.wintypes.BOOL
_user32.CloseClipboard.restype = ctypes.wintypes.BOOL
_user32.EmptyClipboard.restype = ctypes.wintypes.BOOL
_user32.SetClipboardData.argtypes = [ctypes.wintypes.UINT, ctypes.wintypes.HANDLE]
_user32.SetClipboardData.restype = ctypes.wintypes.HANDLE
_user32.GetClipboardData.argtypes = [ctypes.wintypes.UINT]
_user32.GetClipboardData.restype = ctypes.wintypes.HANDLE

_kernel32.GlobalAlloc.argtypes = [ctypes.wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
_kernel32.GlobalFree.argtypes = [ctypes.wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = ctypes.wintypes.HGLOBAL


def save_frontmost_app():
    """Save the currently focused window before recording starts."""
    global _saved_hwnd
    try:
        _saved_hwnd = _user32.GetForegroundWindow()
    except Exception as e:
        logger.warning("Failed to save foreground window: %s", e)


def _set_clipboard_text(text: str):
    """Copy text to clipboard using Win32 API (safe from shell injection)."""
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    encoded = text.encode("utf-16-le") + b"\x00\x00"
    h_mem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not h_mem:
        raise OSError("GlobalAlloc failed")
    try:
        p_mem = _kernel32.GlobalLock(h_mem)
        if not p_mem:
            raise OSError("GlobalLock failed")
        ctypes.memmove(p_mem, encoded, len(encoded))
        _kernel32.GlobalUnlock(h_mem)

        # Retry OpenClipboard — another process may briefly hold it
        opened = False
        for _ in range(5):
            if _user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            raise OSError("OpenClipboard failed after retries")
        try:
            _user32.EmptyClipboard()
            _user32.SetClipboardData(CF_UNICODETEXT, h_mem)
            h_mem = None  # clipboard owns the memory now
        finally:
            _user32.CloseClipboard()
    finally:
        if h_mem:
            _kernel32.GlobalFree(h_mem)


def _get_clipboard_text() -> "str | None":
    """Lee el texto actual del clipboard. Devuelve None si no hay texto o falla."""
    CF_UNICODETEXT = 13
    try:
        opened = False
        for _ in range(5):
            if _user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            return None
        try:
            h_data = _user32.GetClipboardData(CF_UNICODETEXT)
            if not h_data:
                return None
            p_mem = _kernel32.GlobalLock(h_data)
            if not p_mem:
                return None
            try:
                text = ctypes.wstring_at(p_mem)
            finally:
                _kernel32.GlobalUnlock(h_data)
            return text if text else None
        finally:
            _user32.CloseClipboard()
    except Exception as e:
        logger.warning("Failed to read clipboard text: %s", e)
        return None


def paste_text(text: str) -> str:
    """Copy text to clipboard and paste into the previously active window.

    Returns:
        "pasted"         — texto copiado al clipboard y Ctrl+V simulado con éxito.
        "clipboard_only" — texto en clipboard pero Ctrl+V no simulado (ventana no verificada
                           o excepción al simular).
        "failed"         — _set_clipboard_text lanzó excepción; nada se copió.
    """
    global _saved_hwnd

    restore_clipboard = os.getenv("RESTORE_CLIPBOARD", "false").lower() == "true"

    # 1. Leer clipboard previo (solo si la restauración está habilitada)
    prev_clipboard: "str | None" = None
    if restore_clipboard:
        prev_clipboard = _get_clipboard_text()

    # 2. Copiar al clipboard via Win32 API (sin riesgo de shell injection)
    try:
        _set_clipboard_text(text)
    except Exception as e:
        logger.error("Failed to set clipboard text: %s", e)
        _saved_hwnd = None
        return "failed"

    # 3. Verificar ventana destino
    hwnd = _saved_hwnd
    _saved_hwnd = None

    if not hwnd or not _user32.IsWindow(hwnd):
        logger.warning("No valid target window to paste into (hwnd=%s).", hwnd)
        return "clipboard_only"

    # Intentar restaurar foco y verificar que quedó correctamente
    try:
        _user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
    except Exception as e:
        logger.warning("SetForegroundWindow raised exception: %s", e)

    current_fg = _user32.GetForegroundWindow()
    if current_fg != hwnd:
        logger.warning(
            "Focus verification failed: expected hwnd=%s, got %s. Skipping Ctrl+V.",
            hwnd, current_fg,
        )
        return "clipboard_only"

    # 4. Simular Ctrl+V
    try:
        ctrl = Controller()
        with ctrl.pressed(Key.ctrl):
            ctrl.press('v')
            ctrl.release('v')
    except Exception as e:
        logger.warning("Failed to simulate Ctrl+V: %s", e)
        return "clipboard_only"

    # 5. Restaurar clipboard previo si está habilitado
    if restore_clipboard and prev_clipboard is not None:
        time.sleep(1.5)
        try:
            _set_clipboard_text(prev_clipboard)
        except Exception as e:
            logger.warning("Failed to restore previous clipboard: %s", e)

    return "pasted"
