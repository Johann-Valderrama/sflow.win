import time
from pynput import keyboard
from PyQt6.QtCore import QObject, pyqtSignal
from config import DOUBLE_TAP_INTERVAL


class HotkeyListener(QObject):
    """Global hotkey listener with four modes:

    1. Hold Ctrl+Alt:          press-and-hold → transcribe (release to stop)
    2. Triple-tap Shift:       hands-free toggle → transcribe (tap Shift again to stop)
    3. Hold Ctrl+Shift+Alt:    press-and-hold → translate → target language
                               (Shift must be held BEFORE Alt)
    4. Alt Gr + Space toggle:  hands-free toggle → translate → target language
                               (press once to start, press again to stop)

    NOTE: Alt Gr is tracked separately from regular Alt so pressing Alt Gr alone
    does NOT accidentally trigger Ctrl+Alt (mode 1).
    """

    pressed = pyqtSignal()
    released = pyqtSignal()
    toggle_pill = pyqtSignal()
    translate_pressed = pyqtSignal()

    def __init__(self):
        """Inicializa el estado de teclas y la detección de doble-tap."""
        super().__init__()
        self._ctrl_held = False
        self._alt_held = False       # regular Alt only (not Alt Gr)
        self._alt_gr_held = False    # Alt Gr tracked separately
        self._shift_held = False
        self._recording = False
        self._hands_free = False
        self._alt_gr_space_mode = False  # True when in Alt Gr + Space translate toggle
        self._listener: keyboard.Listener | None = None

        # Triple-tap detection (Shift)
        self._last_shift_press = 0.0
        self._shift_tap_count = 0

    def start(self):
        """Inicia el listener global de teclado en un hilo daemon."""
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        """Detiene y libera el listener de teclado."""
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        """Maneja press de teclas para los cuatro modos de activación."""
        is_ctrl   = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt    = key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r)
        is_alt_gr = key == keyboard.Key.alt_gr
        is_shift  = key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r)
        is_space  = key == keyboard.Key.space

        # Alt+J: toggle pill visibility
        try:
            if self._alt_held and hasattr(key, 'char') and key.char == 'j':
                self.toggle_pill.emit()
                return
        except AttributeError:
            pass

        # --- Mode 4: Alt Gr + Space toggle (translate, hands-free style) ---
        if is_space and self._alt_gr_held:
            if self._alt_gr_space_mode and self._recording:
                # Second press → stop recording
                self._alt_gr_space_mode = False
                self._recording = False
                self.released.emit()
            elif not self._recording:
                # First press → start translate (no hold needed)
                self._alt_gr_space_mode = True
                self._recording = True
                self.translate_pressed.emit()
            return

        # --- Modifier state tracking ---
        if is_ctrl:
            self._ctrl_held = True
        elif is_alt:
            self._alt_held = True
        elif is_alt_gr:
            self._alt_gr_held = True
        elif is_shift:
            now = time.time()

            # Ignore repeats (Windows auto-repeat fix)
            if self._shift_held:
                return
            self._shift_held = True

            # Hands-free (mode 2): single Shift tap stops it
            if self._hands_free and self._recording:
                self._hands_free = False
                self._recording = False
                self.released.emit()
                return

            # Triple-tap detection
            if now - self._last_shift_press < DOUBLE_TAP_INTERVAL:
                self._shift_tap_count += 1
            else:
                self._shift_tap_count = 1
            self._last_shift_press = now

            if self._shift_tap_count >= 3 and not self._recording:
                # Mode 2: Triple-tap Shift → hands-free transcribe
                self._shift_tap_count = 0
                self._hands_free = True
                self._recording = True
                self.pressed.emit()
                return

        # --- Mode 1 / Mode 3: Ctrl+Alt hold ---
        if self._ctrl_held and self._alt_held and not self._recording:
            self._recording = True
            self._hands_free = False
            if self._shift_held:
                # Mode 3: Ctrl+Shift+Alt → translate
                self.translate_pressed.emit()
            else:
                # Mode 1: Ctrl+Alt → transcribe
                self.pressed.emit()

    def _on_release(self, key):
        """Detecta liberación de teclas y detiene grabación en modos hold."""
        is_ctrl   = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt    = key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r)
        is_alt_gr = key == keyboard.Key.alt_gr
        is_shift  = key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r)

        # Update modifier states
        if is_ctrl:
            self._ctrl_held = False
        elif is_alt:
            self._alt_held = False
        elif is_alt_gr:
            self._alt_gr_held = False
        elif is_shift:
            self._shift_held = False

        # Mode 4 (Alt Gr + Space toggle): stop is handled by second press in _on_press.
        # Releasing Alt Gr does NOT stop recording — user must press Alt Gr + Space again.
        if self._alt_gr_space_mode:
            return

        # Mode 1 / Mode 3 (hold): stop when Ctrl or Alt released
        # Mode 2 (hands-free): stop handled in _on_press (Shift tap)
        if self._recording and not self._hands_free:
            if not (self._ctrl_held and self._alt_held):
                self._recording = False
                self.released.emit()
