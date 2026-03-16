import subprocess
import time
import os
import ctypes

_saved_app: str | None = None
_saved_hwnd = None


def save_frontmost_app():
    """Save the currently focused application/window before recording starts."""
    global _saved_app, _saved_hwnd
    
    if os.name == 'nt':
        try:
            _saved_hwnd = ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=2,
            )
            name = result.stdout.strip()
            if name and name != "SFlow":
                _saved_app = name
        except Exception:
            pass


def paste_text(text: str):
    """Copy text to clipboard and paste into the previously active app."""
    global _saved_app, _saved_hwnd
    
    # 1. Copy to clipboard
    if os.name == 'nt':
        try:
            # Using PowerShell to set clipboard handles UTF-8 correctly
            escaped_text = text.replace("'", "''").replace('"', '`"')
            subprocess.run(
                ["powershell", "-Command", f'Set-Clipboard -Value "{escaped_text}"'],
                check=True,
                capture_output=True
            )
        except Exception:
            pass
    else:
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(text, NSPasteboardTypeString)
        except Exception:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)

    # 2. Restore focus
    if os.name == 'nt':
        if _saved_hwnd:
            try:
                ctypes.windll.user32.SetForegroundWindow(_saved_hwnd)
                time.sleep(0.15)
            except Exception:
                pass
    else:
        if _saved_app:
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{_saved_app}" to activate'],
                    check=True, timeout=2,
                )
                time.sleep(0.15)
            except Exception:
                pass

    # 3. Simulate Paste (Ctrl+V or Cmd+V)
    if os.name == 'nt':
        try:
            from pynput.keyboard import Controller, Key
            keyboard = Controller()
            with keyboard.pressed(Key.ctrl):
                keyboard.press('v')
                keyboard.release('v')
        except Exception:
            pass
    else:
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
                check=True,
            )
        except Exception:
            pass
            
    _saved_app = None
    _saved_hwnd = None
