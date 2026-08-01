import ctypes
import ctypes.wintypes
import os
import time
import logging

from pynput.keyboard import Controller, Key

logger = logging.getLogger(__name__)

_saved_hwnd = None
_saved_exe = None  # basename del .exe en foco al momento de save_frontmost_app() (best-effort)

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

# Unidad 3a: detectar si un Ctrl+C copió algo, sin comparar contenidos.
_user32.GetClipboardSequenceNumber.restype = ctypes.wintypes.DWORD

# Estado físico de los modificadores (3a-fix): sin esto, el Ctrl+C sintético sale
# como Ctrl+Alt+C mientras el usuario sostiene el AltGr de su atajo.
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short

# Tope DURO de la captura de selección (unidad 3a). No es el presupuesto del
# modelo (ese vive en core/transform.py con budget_chars): es la guarda de
# cordura para que un Ctrl+A sobre un documento gigante no entre a memoria.
CAPTURE_MAX_CHARS = 200_000

# ---------------------------------------------------------------------------
# Resolución HWND → nombre de proceso (unidad 6.3, modos de dictado por app).
# Cero dependencias nuevas: ctypes puro sobre user32/kernel32.
# ---------------------------------------------------------------------------
_user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
_kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
_kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

# QueryFullProcessImageNameW vive en kernel32 (Vista+).
_kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.wintypes.DWORD,
    ctypes.wintypes.LPWSTR,
    ctypes.POINTER(ctypes.wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL


def _get_foreground_exe_name(hwnd) -> "str | None":
    """Resuelve el basename del .exe en foco a partir de su HWND. Best-effort:
    cualquier fallo (proceso protegido, API no disponible, etc.) devuelve None
    sin lanzar, y el caller trata eso como "sin reformateo" (unidad 6.3)."""
    try:
        pid = ctypes.wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        h_process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_process:
            return None
        try:
            buf_len = ctypes.wintypes.DWORD(260)
            buf = ctypes.create_unicode_buffer(buf_len.value)
            ok = _kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(buf_len))
            if not ok:
                return None
            full_path = buf.value
            return os.path.basename(full_path) if full_path else None
        finally:
            _kernel32.CloseHandle(h_process)
    except Exception as e:
        logger.debug("No se pudo resolver el exe de la ventana en foco: %s", e)
        return None


def save_frontmost_app():
    """Save the currently focused window before recording starts.

    También captura (best-effort, unidad 6.3) el nombre del .exe en foco en
    este mismo instante — mientras la ventana destino AÚN tiene el foco, antes
    de que empiece la grabación. Si falla, ``_saved_exe`` queda en None y
    simplemente no habrá reformateo de dictado para esta grabación.
    """
    global _saved_hwnd, _saved_exe
    try:
        _saved_hwnd = _user32.GetForegroundWindow()
    except Exception as e:
        logger.warning("Failed to save foreground window: %s", e)
        _saved_hwnd = None
    _saved_exe = _get_foreground_exe_name(_saved_hwnd) if _saved_hwnd else None


def get_saved_hwnd():
    """Devuelve el HWND guardado en el último ``save_frontmost_app()``, SIN consumirlo.

    Existe para Transform (unidad 3d-fix): ``_saved_hwnd`` es una global de módulo
    que comparten el dictado y Transform, y ``paste_text`` la CONSUME. Con un
    Transform esperando la aprobación del usuario, un dictado normal en medio la
    sobrescribe o la deja en None, y el texto ya aceptado terminaba pegándose en
    la ventana equivocada o quedándose solo en el portapapeles. Quien necesite
    recordar SU ventana destino se la guarda con esto y se la pasa luego a
    ``paste_text(hwnd=...)``.
    """
    return _saved_hwnd


def get_saved_exe() -> "str | None":
    """Devuelve el basename del .exe capturado en el último ``save_frontmost_app()``.

    NO lo consume/limpia (a diferencia de ``_saved_hwnd``, que ``paste_text``
    resetea a None tras usarlo): el flujo de reformateo de 6.3 lo lee en el hilo
    background de transcripción, en un momento distinto a cuando ``paste_text``
    consume el HWND. Se sobrescribe en el próximo ``save_frontmost_app()``.
    """
    return _saved_exe


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


def _clipboard_sequence() -> int:
    """Número de secuencia del portapapeles (se incrementa en CADA cambio).

    Es la forma correcta de saber si un Ctrl+C copió algo, incluso cuando lo
    copiado es IDÉNTICO a lo que ya había: comparar el texto no distingue "no
    había selección" de "la selección era igual al portapapeles". Devuelve 0 si
    la API no está disponible (sin acceso a la window station), y el caller trata
    ese 0 como "detección no disponible" y se pone más estricto, nunca más laxo.
    """
    try:
        return int(_user32.GetClipboardSequenceNumber())
    except Exception as e:
        logger.debug("GetClipboardSequenceNumber no disponible: %s", e)
        return 0


def _send_ctrl_c() -> bool:
    """Simula Ctrl+C sobre la ventana en foco. True si se pudo enviar."""
    try:
        ctrl = Controller()
        with ctrl.pressed(Key.ctrl):
            ctrl.press('c')
            ctrl.release('c')
        return True
    except Exception as e:
        logger.warning("capture_selection: fallo al simular Ctrl+C: %s", e)
        return False


# Modificadores que hay que ver ARRIBA antes de mandar un Ctrl+C sintético.
_MODIFIER_VKS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win_izq": 0x5B, "win_der": 0x5C}


def _modifiers_down() -> list:
    """Nombres de los modificadores que están físicamente presionados ahora mismo."""
    abajo = []
    for nombre, vk in _MODIFIER_VKS.items():
        try:
            if _user32.GetAsyncKeyState(vk) & 0x8000:
                abajo.append(nombre)
        except Exception:  # noqa: BLE001 — si la API falla, se asume libre y se sigue
            pass
    return abajo


def _wait_modifiers_released(timeout: float = 0.6) -> list:
    """Espera a que el usuario suelte los modificadores. Devuelve los que sigan abajo.

    **Esto es lo que hace que la captura funcione con un atajo que usa AltGr**, y la
    causa está MEDIDA, no supuesta (2026-08-01, banco en el scratchpad de la sesión:
    con los modificadores libres `capture_selection` devuelve `ok`; con AltGr
    presionado devuelve `empty`). El motivo lo dice el propio repo en
    ``core/hotkey.py``: *"En Windows, AltGr genera internamente Ctrl+Alt"*. Como el
    atajo dispara en el PRESS, en ese instante el usuario todavía tiene AltGr abajo,
    así que el Ctrl+C sintético le llega a la aplicación como **Ctrl+Alt+C**, que no
    copia nada. La captura abortaba con "no hay texto seleccionado" teniéndolo.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pendientes = _modifiers_down()
        if not pendientes:
            return []
        time.sleep(0.02)
    return _modifiers_down()


def _force_release_modifiers():
    """Suelta los modificadores a la fuerza (respaldo si el usuario no los suelta).

    Soltar una tecla que no estaba pulsada es inofensivo, y cuando el usuario suelte
    la suya de verdad el evento extra tampoco hace daño. Es preferible a quedarse
    esperando: sin esto, alguien que mantenga AltGr pulsado un segundo de más se
    queda sin captura y lee "no hay texto seleccionado" con el texto seleccionado.
    """
    try:
        ctrl = Controller()
        for tecla in (Key.alt_gr, Key.alt_r, Key.alt_l, Key.alt,
                      Key.ctrl_r, Key.ctrl_l, Key.ctrl, Key.shift, Key.cmd):
            try:
                ctrl.release(tecla)
            except Exception:  # noqa: BLE001 — best-effort por tecla
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning("capture_selection: no se pudieron soltar los modificadores: %s", e)


def capture_selection(timeout: float = 0.8) -> "tuple[str | None, str]":
    """Captura el texto SELECCIONADO en la app en foco vía portapapeles (unidad 3a).

    Guarda primero la ventana destino (``save_frontmost_app()``) para que el
    pegado posterior sepa a dónde volver, manda Ctrl+C, espera a que el
    portapapeles cambie y devuelve lo copiado.

    Reglas de la unidad 3z (diseño), que son la razón de casi todo lo de abajo:

    - **Jamás cae al contenido PREVIO del portapapeles.** Si el usuario dispara
      el atajo sin nada seleccionado, esto devuelve ``"empty"`` y aborta. Sin esa
      guarda, Vflow mandaría a un modelo remoto lo que hubiera copiado desde
      antes (que puede ser cualquier cosa) y el usuario no tendría cómo notarlo:
      es el fallo silencioso más caro de esta ola.
    - **Restaura el portapapeles previo** cuando ese contenido era texto. Ver la
      enmienda de 3z sobre contenido no textual en el plan: una imagen copiada no
      se puede restaurar con esta API, y se prefiere dejar la selección (el mismo
      estado que produciría un Ctrl+C manual del usuario) antes que destruirle la
      imagen vaciando el portapapeles.
    - **Tope duro de tamaño** (``CAPTURE_MAX_CHARS``). Es una guarda de cordura
      contra un Ctrl+A sobre un documento entero, NO el presupuesto del modelo:
      ese lo aplica ``core/transform.py`` (unidad 3b) con ``budget_chars`` y con
      aviso visible.
    - **Nunca loguea el CONTENIDO capturado**, solo su longitud.

    Returns:
        ``(texto, "ok")``        — hay selección y cabe.
        ``(None, "empty")``      — no había nada seleccionado (o no se pudo
                                   confirmar que el Ctrl+C copiara algo nuevo).
        ``(None, "too_long")``   — la selección supera el tope duro.
        ``(None, "failed")``     — no se pudo simular Ctrl+C.
    """
    save_frontmost_app()

    # ANTES de nada: el Ctrl+C no sirve mientras el usuario tenga abajo el
    # modificador de su propio atajo (ver _wait_modifiers_released, con la medición).
    pendientes = _wait_modifiers_released()
    if pendientes:
        logger.info("capture_selection: modificadores aún abajo (%s), se sueltan a la fuerza",
                    ", ".join(pendientes))
        _force_release_modifiers()
        time.sleep(0.05)

    prev_text = _get_clipboard_text()
    seq_before = _clipboard_sequence()
    seq_available = seq_before != 0

    if not _send_ctrl_c():
        return None, "failed"

    changed = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.02)
        if seq_available:
            if _clipboard_sequence() != seq_before:
                changed = True
                break
        else:
            # Sin número de secuencia solo queda comparar el texto, así que se
            # exige que sea DISTINTO del previo. Es un falso "empty" cuando la
            # selección coincidía con el portapapeles, y ese es el lado seguro
            # del error: aborta con aviso en vez de transformar texto que el
            # usuario no seleccionó ahora.
            current = _get_clipboard_text()
            if current and current != prev_text:
                changed = True
                break

    if not changed:
        return None, "empty"

    try:
        text = _get_clipboard_text()
    finally:
        if prev_text is not None:
            try:
                _set_clipboard_text(prev_text)
            except Exception as e:
                logger.warning("capture_selection: no se pudo restaurar el portapapeles: %s", e)

    if not text or not text.strip():
        return None, "empty"
    if len(text) > CAPTURE_MAX_CHARS:
        logger.info("capture_selection: selección descartada por tamaño (%d caracteres)", len(text))
        return None, "too_long"

    logger.debug("capture_selection: %d caracteres capturados", len(text))
    return text, "ok"


def copy_text(text: str) -> bool:
    """Copia texto al portapapeles sin simular Ctrl+V (modo audio del sistema).

    Returns True si el texto quedó en el portapapeles, False si falló.
    """
    try:
        _set_clipboard_text(text)
        return True
    except Exception as e:
        logger.error("copy_text: fallo al copiar al portapapeles: %s", e)
        return False


def paste_text(text: str, hwnd=None) -> str:
    """Copy text to clipboard and paste into the previously active window.

    Args:
        hwnd: ventana destino EXPLÍCITA (unidad 3d-fix). Con ``None`` se usa y se
            CONSUME la global ``_saved_hwnd``, que es el comportamiento de siempre
            del dictado. Quien pase un hwnd propio no toca esa global: así un
            Transform que estuvo esperando aprobación se pega en la ventana donde
            se hizo la selección, aunque en medio haya habido un dictado, y sin
            robarle a ese dictado su propia ventana destino.

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
    if hwnd is None:
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
