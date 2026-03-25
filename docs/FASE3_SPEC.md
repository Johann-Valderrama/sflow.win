# FASE 3 — IMPLEMENTATION SPECIFICATION
# Target reader: AI agent. Not a human document.
# Project: Vflow — Windows voice-to-text tool (PyQt6 + Groq Whisper + Flask + pynput)
# Branch: windows-variant
# Working directory: C:\Users\OswyDesktop.0\Antigravity proyectos\Sflow.Win\

---

## MANDATORY PRE-READ (read ALL files listed before writing a single line of code)

```
core/hotkey.py        — HotkeyListener class, pynput daemon thread, signal architecture
main.py               — VflowApp class, signal connections, threading model
web/server.py         — Flask endpoints, HTML_TEMPLATE string, _set_env_key() helper
config.py             — _RESOURCE_DIR, _DATA_DIR, APP_DATA_DIR, load_dotenv() at import time
requirements.txt      — current deps: PyQt6, sounddevice, numpy, pynput, groq, python-dotenv, flask
vflow.spec            — PyInstaller spec, datas list pattern
```

Read the full content of each file before any edit. Do not rely on memory or summaries.

---

## CONTEXT: WHAT PHASES 1 AND 2 ALREADY IMPLEMENTED

These are DONE. Do not re-implement.

**Phase 1 (done):**
- `WHISPER_LANGUAGE` read from `os.getenv()` in `transcriber.py` at transcription time
- Microphone selection by name via `_resolve_device()` in `recorder.py`, reads `AUDIO_DEVICE_NAME` env var
- Interaction sounds via `winsound.Beep()` in daemon thread, controlled by `SOUNDS_ENABLED` env var
- Dashboard settings panel at ⚙ button: language dropdown, mic dropdown, sounds toggle
- New Flask endpoints: `GET/POST /api/settings`, `GET /api/microphones`
- Helper `_set_env_key(key, value)` in `web/server.py` — uses `dotenv.set_key()` + updates `os.environ`

**Phase 2 (done):**
- `Transcriber.translate(wav_buffer)` in `core/transcriber.py` — calls `audio.translations.create()` with `model="whisper-large-v3"` (NOT turbo — turbo not supported for translations on Groq)
- `translate_pressed = pyqtSignal()` in `HotkeyListener`
- Hotkey A: `Ctrl+Shift+Alt` hold → translate mode (Shift must be held before Alt)
- Hotkey B: `Alt Gr + Space` hold → translate mode (`_alt_gr_space_mode` flag, separate release path)
- `_alt_gr_held` tracked separately from `_alt_held` (alt_gr does NOT trigger Ctrl+Alt mode)
- `_on_translate_pressed()` in `VflowApp` — starts recording without chunk timer
- `_transcribe_final(wav_buffer, duration, translate: bool)` — routes to translate() or transcribe()
- `_translate_mode: bool` flag on VflowApp, reset to False before background thread starts

**Env vars written to `.env` by Phase 1:**
```
GROQ_API_KEY=...         # existing, written by FirstRunDialog
WHISPER_LANGUAGE=es      # configurable, default "es"
AUDIO_DEVICE_NAME=       # empty = system default
SOUNDS_ENABLED=true      # "true" / "false"
```

---

## PHASE 3 — FEATURE A: MUTE SYSTEM AUDIO WHILE DICTATING

### Requirement
Silence all other application audio output (music, videos, browser) while Vflow is recording, then restore it when recording stops. Equivalent to Typeless "Silenciar al dictar" toggle.

### Architecture decision
Use `pycaw` (Python Core Audio Windows). It exposes the Windows Core Audio API (WASAPI) via COM objects. This is the only reliable way to mute per-application audio on Windows without affecting microphone input.

### Risk profile (HIGH — read before implementing)
1. `pycaw` can raise `COMError` if audio devices change during dictation
2. Virtual audio devices (VB-Cable, Voicemeeter, OBS Virtual) can cause `Exception` on `GetAllSessions()`
3. If mute logic crashes WITHOUT cleanup → user's audio stays muted permanently until app restart
4. Video call apps (Teams, Zoom) will be muted — this is a known side effect, acceptable
5. If no other app is playing audio → no-op (correct behavior, do not error)

### Implementation rules
- **ALWAYS wrap in try/except at every COM call**, never let an exception propagate
- **ALWAYS restore audio in a finally block** — mute/unmute must be paired unconditionally
- Add as `MUTE_ON_DICTATE=false` env var, **disabled by default** — user must explicitly enable
- Toggle exposed in dashboard settings panel (same ⚙ panel from Phase 1)

### New dependency
```
pycaw>=20181205
```
Add to `requirements.txt`. No changes to `vflow.spec` needed (pycaw is pure Python + ctypes).

### New module to create: `core/audio_session.py`

```python
# core/audio_session.py
"""Mute/unmute system audio sessions using pycaw (Windows Core Audio API).

SAFETY CONTRACT: unmute() MUST always be called after mute(), even on exception.
Use as context manager or ensure finally block in caller.
"""
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Type alias: list of (session, original_volume) pairs for cleanup
_MutedSessions = List[Tuple]


def mute_all_except_self() -> _MutedSessions:
    """Mute all active audio output sessions except Vflow's own process.

    Returns list of (ISimpleAudioVolume, original_volume) for unmute_all().
    Returns empty list if pycaw unavailable, no sessions, or any error.
    Never raises.
    """
    import os
    if os.getenv("MUTE_ON_DICTATE", "false") != "true":
        return []
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        import comtypes
        sessions = AudioUtilities.GetAllSessions()
        muted = []
        for session in sessions:
            try:
                # Skip Vflow's own process (Process is None for system sounds, skip those too)
                if session.Process is None:
                    continue
                if session.Process.name().lower() in ("vflow.exe", "python.exe", "pythonw.exe"):
                    continue
                vol = session._ctl.QueryInterface(ISimpleAudioVolume)
                original = vol.GetMasterVolume()
                vol.SetMasterVolume(0.0, None)
                muted.append((vol, original))
            except Exception as e:
                logger.warning("Could not mute session: %s", e)
        return muted
    except Exception as e:
        logger.warning("pycaw mute failed (non-fatal): %s", e)
        return []


def unmute_all(muted_sessions: _MutedSessions) -> None:
    """Restore all previously muted sessions. Never raises.

    Args:
        muted_sessions: list returned by mute_all_except_self()
    """
    for vol, original_volume in muted_sessions:
        try:
            vol.SetMasterVolume(original_volume, None)
        except Exception as e:
            logger.warning("Could not unmute session: %s", e)
```

### Changes to `main.py`

In `VflowApp.__init__`, add:
```python
self._muted_sessions = []  # stores pycaw session handles for cleanup
```

In `_on_hotkey_pressed` and `_on_translate_pressed`, AFTER `self.recorder.start()` succeeds, add:
```python
from core.audio_session import mute_all_except_self
self._muted_sessions = mute_all_except_self()
```

In `_on_hotkey_released`, BEFORE `self.pill.set_state(PillWidget.STATE_PROCESSING)`, add:
```python
from core.audio_session import unmute_all
unmute_all(self._muted_sessions)
self._muted_sessions = []
```

Also add cleanup on error in `_on_hotkey_pressed` exception handler (if recorder.start() fails):
```python
self._muted_sessions = []  # nothing was muted
```

### Changes to `web/server.py`

In `get_settings()` response dict, add:
```python
"mute_on_dictate": os.getenv("MUTE_ON_DICTATE", "false") == "true",
```

In `update_settings()` allowed dict, add:
```python
"mute_on_dictate": "MUTE_ON_DICTATE",
```

In `HTML_TEMPLATE`, inside the settings panel grid (the 3-column div), add a 4th column:
```html
<div>
    <label class="text-xs text-white/40 block mb-1">Silenciar al dictar</label>
    <div class="flex items-center gap-2" style="height:32px">
        <label class="toggle-switch">
            <input type="checkbox" id="cfg-mute">
            <span class="toggle-slider"></span>
        </label>
        <span class="text-xs text-white/40">Silencia otras apps</span>
    </div>
</div>
```

In `loadSettings()` JS function, add:
```javascript
document.getElementById('cfg-mute').checked = settings.mute_on_dictate === true;
```

In `saveSettings()` JS function, add to `data` object:
```javascript
mute_on_dictate: document.getElementById('cfg-mute').checked ? 'true' : 'false',
```

### Verification
1. Enable toggle in dashboard → `MUTE_ON_DICTATE=true` written to `.env`
2. Play music in browser → start dictating → music goes silent
3. Stop dictating → music resumes
4. Connect VB-Cable or OBS Virtual → verify app does not crash (warning log only)
5. Disable toggle → music not silenced during dictation

---

## PHASE 3 — FEATURE B: CONFIGURABLE HOTKEYS VIA DASHBOARD UI

### Requirement
Allow user to change the three hotkey combinations (Dictado, Manos libres, Traducción) from the web dashboard. Changes persist across restarts and take effect without restarting the app.

### Architecture decision
Store hotkey config as JSON in `%APPDATA%\Vflow\hotkeys.json` (not in `.env` — complex structure). The `HotkeyListener` reads this config at start and on reload. Reload is triggered by stopping and restarting the pynput Listener with new config.

### Current hotkey hardcoded logic (READ before modifying)
`core/hotkey.py` — `HotkeyListener._on_press()`:
- Hold mode: `self._ctrl_held and self._alt_held` → `pressed.emit()` (Ctrl+Alt hold)
- Translate mode: same as hold + `self._shift_held` → `translate_pressed.emit()` (Ctrl+Shift+Alt)
- Hands-free: double-tap Ctrl within `DOUBLE_TAP_INTERVAL` (0.2s from `config.py`) → `pressed.emit()`
- Toggle pill: `self._alt_held and key.char == 'j'` → `toggle_pill.emit()` (Alt+J)

The listener is a pynput `keyboard.Listener` started as a daemon thread. It supports `stop()` and a new one can be created.

### Hotkey data format (JSON)
```json
{
  "hold": {"keys": ["ctrl", "alt"], "description": "Mantener presionado para dictar"},
  "hands_free": {"keys": ["ctrl", "ctrl"], "tap_count": 2, "interval_ms": 200, "description": "Doble tap para manos libres"},
  "translate": {"keys": ["ctrl", "shift", "alt"], "description": "Mantener para traducir al inglés"},
  "toggle_pill": {"keys": ["alt", "j"], "description": "Mostrar/ocultar pill"}
}
```

Default file content if not found:
```python
DEFAULT_HOTKEYS = {
    "hold": {"keys": ["ctrl", "alt"]},
    "hands_free": {"keys": ["ctrl"], "tap_count": 2, "interval_ms": 200},
    "translate": {"keys": ["ctrl", "shift", "alt"]},
    "toggle_pill": {"keys": ["alt", "j"]},
}
```

### New module to create: `core/hotkey_config.py`

```python
# core/hotkey_config.py
"""Load and save hotkey configuration from JSON."""
import json
import logging
import os
from config import APP_DATA_DIR

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(APP_DATA_DIR, "hotkeys.json")

DEFAULT_HOTKEYS = {
    "hold": {"keys": ["ctrl", "alt"]},
    "hands_free": {"keys": ["ctrl"], "tap_count": 2, "interval_ms": 200},
    "translate": {"keys": ["ctrl", "shift", "alt"]},
    "toggle_pill": {"keys": ["alt", "j"]},
}


def load_hotkeys() -> dict:
    """Load hotkey config from JSON. Returns defaults if file missing or invalid."""
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge with defaults to handle missing keys after updates
            merged = dict(DEFAULT_HOTKEYS)
            merged.update(data)
            return merged
    except Exception as e:
        logger.warning("Failed to load hotkeys.json: %s — using defaults", e)
    return dict(DEFAULT_HOTKEYS)


def save_hotkeys(config: dict) -> bool:
    """Save hotkey config to JSON. Returns True on success."""
    try:
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error("Failed to save hotkeys.json: %s", e)
        return False
```

### Changes to `core/hotkey.py`

**CRITICAL threading constraint:** The pynput `Listener` thread cannot be modified from outside safely. To reload hotkeys:
1. Call `self._listener.stop()` — pynput supports this
2. Create a new `keyboard.Listener` with new `on_press`/`on_release` callbacks
3. Set `self._listener.daemon = True` and `.start()`

The new HotkeyListener must support a `reload()` method callable from the Flask thread (via `os.environ` or a threading.Event signal). Use `threading.Event` to avoid polling:

```python
# In HotkeyListener.__init__:
self._reload_event = threading.Event()

# New public method:
def reload(self):
    """Signal hotkey listener to reload config and restart. Thread-safe."""
    self._reload_event.set()
```

The listener thread watches `_reload_event` in a wrapper loop:

```python
def start(self):
    """Start hotkey listener with config reload support."""
    import threading
    def _run():
        while True:
            self._reload_event.clear()
            cfg = load_hotkeys()
            self._apply_config(cfg)
            listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            listener.daemon = True
            self._listener = listener
            listener.start()
            # Block until reload is requested or listener dies
            self._reload_event.wait()
            listener.stop()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
```

`_apply_config(cfg)` updates internal state variables from the config dict (which keys to track for hold mode, translate mode, etc.).

**Key name mapping** — pynput uses `keyboard.Key.ctrl_l`, etc. Map JSON key names:
```python
KEY_MAP = {
    "ctrl":  (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r),
    "alt":   (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr, keyboard.Key.alt),
    "shift": (keyboard.Key.shift_l, keyboard.Key.shift_r, keyboard.Key.shift),
    "win":   (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r),
}
# Single char keys (e.g. "j") are matched via key.char == "j"
```

### Changes to `web/server.py`

**New endpoints:**
```python
@app.route("/api/hotkeys")
def get_hotkeys():
    from core.hotkey_config import load_hotkeys
    return jsonify(load_hotkeys())

@app.route("/api/hotkeys", methods=["POST"])
def update_hotkeys():
    from core.hotkey_config import save_hotkeys
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400
    ok = save_hotkeys(data)
    if ok:
        # Signal hotkey listener to reload (requires passing listener reference to server)
        # See: _hotkey_listener global or app.config["hotkey_listener"]
        _trigger_hotkey_reload()
    return jsonify({"ok": ok})
```

**Passing listener reference to Flask:**
In `main.py`, after creating `VflowApp`, add to Flask app config:
```python
from web.server import app as flask_app
flask_app.config["hotkey_listener"] = vflow.hotkey
```

In `web/server.py`, `_trigger_hotkey_reload()`:
```python
def _trigger_hotkey_reload():
    listener = app.config.get("hotkey_listener")
    if listener and hasattr(listener, "reload"):
        listener.reload()
```

**Dashboard UI — key capture widget:**
In the settings panel, add a "Hotkeys" section (separate from the existing 3-column grid, below it):

```html
<div class="mt-5 border-t border-white/5 pt-4">
    <div class="text-xs text-white/40 mb-3">Atajos de teclado</div>
    <div class="space-y-2" id="hotkey-rows"></div>
    <p class="text-xs text-white/20 mt-2">Haz clic en un atajo para reconfigurarlo. Presiona las teclas deseadas.</p>
</div>
```

**JavaScript key capture logic:**
```javascript
let capturingHotkey = null;  // name of hotkey being captured
let capturedKeys = new Set();

function startCapture(name, btn) {
    capturingHotkey = name;
    capturedKeys.clear();
    btn.textContent = '...presiona teclas...';
    btn.classList.add('capturing');
}

document.addEventListener('keydown', (e) => {
    if (!capturingHotkey) return;
    e.preventDefault();
    const key = mapKeyToName(e);
    if (key) capturedKeys.add(key);
});

document.addEventListener('keyup', (e) => {
    if (!capturingHotkey) return;
    if (capturedKeys.size > 0) {
        hotkeyConfig[capturingHotkey] = {keys: [...capturedKeys]};
        renderHotkeyRows();
        capturingHotkey = null;
        capturedKeys.clear();
    }
});

function mapKeyToName(e) {
    if (e.key === 'Control') return 'ctrl';
    if (e.key === 'Alt') return 'alt';
    if (e.key === 'Shift') return 'shift';
    if (e.key === 'Meta') return 'win';
    if (e.key.length === 1) return e.key.toLowerCase();
    return null;
}
```

**CSS for capturing state:**
```css
.hotkey-btn { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    border-radius: 6px; color: #e5e5e5; padding: 4px 10px; font-size: 12px;
    font-family: monospace; cursor: pointer; }
.hotkey-btn.capturing { border-color: rgba(140,80,220,0.6); color: rgba(140,80,220,0.9);
    animation: pulse 1s infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
```

### Verification
1. Open dashboard → ⚙ → hotkeys section shows current bindings
2. Click "Dictado" → press desired key combo → binding updates in UI
3. Click "Guardar" → `hotkeys.json` written to `%APPDATA%\Vflow\`
4. Verify new hotkey triggers recording without restarting the app
5. Restart app → new hotkeys still active (loaded from file)
6. Press a Windows-reserved combo (e.g. Win+D) → app does not crash, just ignores
7. Edge case: press same key for two bindings → no conflict handling needed for MVP, last-write wins

---

## PHASE 3 — SHARED IMPLEMENTATION NOTES

### Import order (follow existing style in all modified files)
stdlib → third-party → local. Each group alphabetically.

### Threading rules (CRITICAL — violations cause undefined behavior)
- Never touch Qt widgets from any thread other than main thread
- Never touch `self.pill` from a daemon thread
- Cross-thread communication: always use `pyqtSignal` with `Qt.ConnectionType.QueuedConnection`
- pynput callbacks run in pynput's thread → use signals, never direct calls to VflowApp methods
- Flask runs in a daemon thread → use `app.config` dict (thread-safe reads) to share references
- `os.environ` is thread-safe for reads; `_set_env_key()` already handles writes safely

### Pattern: adding a new env var
1. Add to `get_settings()` response in `server.py`
2. Add to `update_settings()` allowed dict in `server.py`
3. Add to `loadSettings()` JS in HTML_TEMPLATE
4. Add to `saveSettings()` JS data object in HTML_TEMPLATE
5. Add UI toggle/input in the settings panel grid in HTML_TEMPLATE
6. Read via `os.getenv("KEY", "default")` at point of use (never at import time)

### Pattern: adding a new Flask endpoint
Follow existing pattern exactly:
```python
@app.route("/api/new-endpoint")
def handler_name():
    # CSRF already handled by _csrf_check() before_request hook
    return jsonify({...})
```

### File paths
```python
# Bundle mode (.exe):  %APPDATA%\Vflow\  (writable)
#                      sys._MEIPASS\     (read-only assets)
# Dev mode:            project root\     (both)
APP_DATA_DIR = config.APP_DATA_DIR  # use this, do not recompute
```

### Error handling policy
- Network/API errors → `logger.error()` + `pill.STATE_ERROR` via signal
- File I/O errors → `logger.warning()` + graceful fallback (use defaults)
- COM errors (pycaw) → `logger.warning()` + return empty list (non-fatal)
- Never `sys.exit()` or `raise` from pynput thread or Flask thread

### Test commands
```bash
# Run in dev mode
cd "C:\Users\OswyDesktop.0\Antigravity proyectos\Sflow.Win"
venv\Scripts\activate
python main.py

# Run existing tests
python -m pytest tests/ -v
```

### Build after implementing
```bash
build.bat
# Output: dist\Vflow\Vflow.exe
```

---

## IMPLEMENTATION ORDER (recommended)

1. Feature A first (mute) — self-contained, low coupling, no UI complexity
   - Create `core/audio_session.py`
   - Add `pycaw` to `requirements.txt`
   - Wire into `main.py` (`_on_hotkey_pressed`, `_on_translate_pressed`, `_on_hotkey_released`)
   - Add toggle to dashboard (settings panel + endpoints)
   - Test with and without virtual audio devices

2. Feature B second (configurable hotkeys) — higher coupling, more moving parts
   - Create `core/hotkey_config.py`
   - Refactor `core/hotkey.py` to support `reload()` and config-driven key detection
   - Add endpoints to `web/server.py`
   - Add key capture UI to dashboard
   - Wire listener reference via `flask_app.config`
   - Test reload without app restart

**Do not implement both in the same session** — each is independently shippable.

---

## CONTEXT SNAPSHOT (state of codebase when this document was written)

- Git branch: `windows-variant`
- Last commit implementing Phase 2: after feat adding translate_pressed signal
- Phases 1+2 verified: transcriber.py uses os.getenv, recorder.py resolves device by name,
  main.py has _play_sound(), server.py has /api/settings + /api/microphones endpoints,
  hotkey.py has translate_pressed signal + _shift_held tracking,
  main.py has _on_translate_pressed() and translate flag in _transcribe_final()

- Known working Python version: 3.12+
- Known working PyInstaller: latest compatible with Python 3.12
- Groq SDK version: >=0.4.0 (supports both audio.transcriptions and audio.translations)
