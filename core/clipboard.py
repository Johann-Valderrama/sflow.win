import subprocess
import time
import ctypes

_saved_hwnd = None


def save_frontmost_app():
    """Save the currently focused window before recording starts."""
    global _saved_hwnd
    try:
        _saved_hwnd = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        pass


def paste_text(text: str):
    """Copy text to clipboard and paste into the previously active window."""
    global _saved_hwnd

    # 1. Copy to clipboard
    try:
        escaped_text = text.replace("'", "''").replace('"', '`"')
        subprocess.run(
            ["powershell", "-Command", f'Set-Clipboard -Value "{escaped_text}"'],
            check=True,
            capture_output=True,
        )
    except Exception:
        pass

    # 2. Restore focus
    if _saved_hwnd:
        try:
            ctypes.windll.user32.SetForegroundWindow(_saved_hwnd)
            time.sleep(0.15)
        except Exception:
            pass

    # 3. Simulate Ctrl+V
    try:
        from pynput.keyboard import Controller, Key
        keyboard = Controller()
        with keyboard.pressed(Key.ctrl):
            keyboard.press('v')
            keyboard.release('v')
    except Exception:
        pass

    _saved_hwnd = None
