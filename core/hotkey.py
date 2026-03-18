import time
from pynput import keyboard
from PyQt6.QtCore import QObject, pyqtSignal
from config import DOUBLE_TAP_INTERVAL


class HotkeyListener(QObject):
    """Global hotkey listener with two modes:

    1. Hold Ctrl+Option: press-and-hold recording
    2. Double-tap Ctrl: hands-free mode (tap Ctrl again to stop)
    """

    pressed = pyqtSignal()
    released = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._ctrl_held = False
        self._alt_held = False
        self._recording = False
        self._hands_free = False
        self._listener: keyboard.Listener | None = None

        # Double-tap detection
        self._last_ctrl_release = 0.0
        self._last_ctrl_press = 0.0
        self._ctrl_tap_count = 0

    def start(self):
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        is_ctrl = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt = key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r,
                         keyboard.Key.alt_gr)

        if is_ctrl:
            now = time.time()

            # Ignore repeats if key is already held (fix for Windows auto-repeat)
            if self._ctrl_held:
                return
            
            self._ctrl_held = True

            # Hands-free: if recording, single Ctrl press stops it
            if self._hands_free and self._recording:
                self._hands_free = False
                self._recording = False
                self.released.emit()
                return

            # Double-tap detection
            if now - self._last_ctrl_press < DOUBLE_TAP_INTERVAL:
                self._ctrl_tap_count += 1
            else:
                self._ctrl_tap_count = 1
            self._last_ctrl_press = now

            if self._ctrl_tap_count >= 2 and not self._recording:
                # Double-tap Ctrl → hands-free mode
                self._ctrl_tap_count = 0
                self._hands_free = True
                self._recording = True
                self.pressed.emit()
                return

        elif is_alt:
            self._alt_held = True

        # Hold mode: Ctrl+Option together
        if self._ctrl_held and self._alt_held and not self._recording:
            self._recording = True
            self._hands_free = False
            self.pressed.emit()

    def _on_release(self, key):
        is_ctrl = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt = key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r,
                         keyboard.Key.alt_gr)

        if is_ctrl:
            self._ctrl_held = False
            self._last_ctrl_release = time.time()
        elif is_alt:
            self._alt_held = False

        # Hold mode: stop when any key released (but not in hands-free mode)
        if self._recording and not self._hands_free:
            if not (self._ctrl_held and self._alt_held):
                self._recording = False
                self.released.emit()
