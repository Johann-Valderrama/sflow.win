"""Atajo global que Windows CONSUME: la app en foco nunca ve la tecla.

**Por qué existe, y por qué no es "otra combinación más".** El atajo de Transform
falló cinco veces seguidas por la misma razón de fondo: el listener de teclado del
resto de la app (pynput, `core/hotkey.py`) **observa** las teclas pero no se las
queda, así que la combinación llega igual a la aplicación en foco. Y Transform
actúa sobre texto SELECCIONADO, así que cualquier tecla que la aplicación
interprete (insertar un carácter, cortar) **destruye justo lo que la feature iba a
transformar**, antes de poder copiarlo.

Medido en el camino, para que nadie repita el ciclo:

- `AltGr+X` inserta una "X" (el editor pasa de `TEXTO-9931` a `X`).
- `Ctrl+Alt+X` hace lo mismo: Windows trata Ctrl+Alt como AltGr.
- `Ctrl+Shift+X` no inserta nada en un `QTextEdit`, pero Johann reportó que SÍ le
  borra la selección en su aplicación real. O sea que "buscar una combinación que
  no haga nada" depende de cada aplicación y **no se puede cerrar por búsqueda**.

`RegisterHotKey` cambia el eje: Windows entrega la combinación a Vflow y **no la
propaga**. Deja de importar qué hace esa tecla en la aplicación de abajo, porque
la aplicación no la recibe. Es la razón por la que este módulo existe en vez de
una sexta combinación.

Convive con `core/hotkey.py` sin tocarlo: aquel sigue con todos los atajos de
dictado y reunión (que no actúan sobre una selección, así que insertar un carácter
les es inofensivo); este se usa SOLO para Transform.
"""
import ctypes
import ctypes.wintypes
import logging

from PyQt6.QtCore import QAbstractNativeEventFilter, QObject, pyqtSignal

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_user32.RegisterHotKey.argtypes = [
    ctypes.wintypes.HWND, ctypes.c_int, ctypes.wintypes.UINT, ctypes.wintypes.UINT,
]
_user32.RegisterHotKey.restype = ctypes.wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = ctypes.wintypes.BOOL

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000       # sin auto-repeat: Windows ya resuelve lo que en pynput
                            # había que apagar a mano con los flags `_x_held`.
WM_HOTKEY = 0x0312

#: Atajo de Transform. `MOD_NOREPEAT` va siempre.
TRANSFORM_HOTKEY_ID = 0xB001
# AltGr+X, que en Windows ES Ctrl+Alt+X. Se vuelve al atajo original (a Johann le
# funcionaba y ya tiene la costumbre) porque con RegisterHotKey deja de importar
# que AltGr+X inserte una "X": Windows se lo entrega a Vflow y no lo propaga.
TRANSFORM_MODS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
TRANSFORM_VK = 0x58          # 'X'
TRANSFORM_LABEL = "AltGr+X"


class _MessageFilter(QAbstractNativeEventFilter):
    """Filtro PURO, sin mezclar con QObject.

    La versión anterior era `class GlobalHotkey(QObject, QAbstractNativeEventFilter)`
    y **su `nativeEventFilter` no se llamaba jamás**: 0 llamadas contra 162 mensajes
    que sí vio una clase que hereda solo del filtro (medido). PyQt acepta la herencia
    múltiple e `installNativeEventFilter` no se queja, así que el fallo es MUDO y el
    síntoma es "el atajo no hace nada". Separar las dos responsabilidades cuesta diez
    líneas y quita la clase entera de problemas.
    """

    def __init__(self, hotkey_id: int, on_hotkey):
        super().__init__()
        self._id = hotkey_id
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, event_type, message):
        try:
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and int(msg.wParam) == self._id:
                self._on_hotkey()
                return True, 0        # consumido
        except Exception as exc:  # noqa: BLE001 — un filtro nativo JAMÁS puede lanzar
            logger.warning("global_hotkey: error en el filtro nativo: %s", exc)
        return False, 0


class GlobalHotkey(QObject):
    """Registra una combinación con Windows y emite ``activated`` al pulsarla.

    Se instala como filtro de eventos nativos de Qt: ``RegisterHotKey`` con
    ``hwnd=None`` publica ``WM_HOTKEY`` en la cola del hilo, y Qt entrega esos
    mensajes a los filtros nativos instalados en la aplicación.
    """

    activated = pyqtSignal()

    def __init__(self, hotkey_id: int = TRANSFORM_HOTKEY_ID,
                 mods: int = TRANSFORM_MODS, vk: int = TRANSFORM_VK):
        super().__init__()
        self._id = hotkey_id
        self._filter = _MessageFilter(hotkey_id, self.activated.emit)
        self._mods = mods
        self._vk = vk
        self._registered = False
        self._app = None

    def register(self, app) -> bool:
        """Registra el atajo e instala el filtro. Devuelve False si Windows lo negó.

        Un False NO es un fallo silencioso: significa que **otra aplicación ya tiene
        esa combinación** y hay que elegir otra. El caller tiene que avisar al
        usuario, porque si no, el atajo simplemente "no hace nada" y eso es
        indistinguible de un bug.
        """
        if self._registered:
            return True
        ok = bool(_user32.RegisterHotKey(None, self._id, self._mods, self._vk))
        if not ok:
            logger.warning(
                "global_hotkey: Windows negó el registro de %s (id=%s). "
                "Lo más probable es que otra aplicación ya lo tenga tomado.",
                TRANSFORM_LABEL, self._id,
            )
            return False
        app.installNativeEventFilter(self._filter)
        self._app = app
        self._registered = True
        logger.info("global_hotkey: %s registrado y CONSUMIDO por Vflow", TRANSFORM_LABEL)
        return True

    def unregister(self):
        if not self._registered:
            return
        try:
            if self._app is not None:
                self._app.removeNativeEventFilter(self._filter)
            _user32.UnregisterHotKey(None, self._id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("global_hotkey: fallo al liberar el atajo: %s", exc)
        finally:
            self._registered = False
            self._app = None

    def is_registered(self) -> bool:
        return self._registered
