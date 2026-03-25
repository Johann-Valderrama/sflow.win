# PRP-001: Vflow — Voice-to-Text Desktop Tool (Wispr Flow Alternative)

> **Status**: COMPLETED
> **Date**: 2026-03-04
> **Author**: Claude Opus 4.6
> **Project**: vflow

---

## Objective

Build a macOS desktop voice-to-text tool in Python that captures microphone audio via a global hotkey (press-and-hold or double-tap), transcribes it using Groq Whisper API (~$0.02/hour), and auto-pastes the text wherever the cursor is. Includes a floating "pill" overlay with real-time audio visualization, an SQLite database for transcription history, and a web dashboard at localhost:5000.

This is a fully functional replacement for Wispr Flow ($15/month) built from scratch in a single session.

---

## Why

| Current Problem | Proposed Solution |
|-----------------|-------------------|
| Wispr Flow costs $15/month ($180/year) | Groq Whisper API costs ~$0.60/month with heavy use |
| Dependency on a third-party service | Own app, local, no external dependencies beyond STT API |
| No control over data/privacy | Everything runs local, audio only sent to Groq for transcription |
| No transcription history | Local SQLite stores all transcriptions with timestamps |
| Limited to their UI/UX choices | Full control — custom pill UI, hotkeys, web dashboard |

**Value**: Eliminate $180/year recurring cost and gain full control over the tool.

---

## What

### Expected Behavior

1. Launch `vflow` — a small floating pill appears at the bottom-center of the screen (logo only)
2. **Hold Mode**: User holds **Ctrl+Shift** — pill expands showing animated audio bars
3. **Hands-Free Mode**: User double-taps **Ctrl** — recording starts, tap Ctrl again to stop
4. Audio bars react in real-time to microphone volume
5. User releases keys (or taps Ctrl in hands-free mode)
6. Pill shows a spinning indicator briefly while Groq processes
7. Transcribed text is copied to clipboard and auto-pasted (Cmd+V) where the cursor was
8. Pill shows a green checkmark briefly, then returns to idle
9. If an error occurs, pill shows a red X briefly
10. Transcription is saved to SQLite with timestamp and duration
11. Web dashboard at `http://localhost:5000` shows full history with search

### Success Criteria
- [x] Global hotkey works in any app (Cursor, Chrome, Slack, etc.)
- [x] Audio captured correctly from microphone
- [x] Audio bars visualize in real-time
- [x] Transcription via Groq Whisper returns correct text in Spanish and English
- [x] Text auto-pastes where the cursor is WITHOUT stealing focus
- [x] Transcriptions saved in SQLite with search
- [x] Fluid UI — doesn't block, looks professional (dark semi-transparent pill)
- [x] Web dashboard for transcription history
- [x] Double-tap Ctrl hands-free mode
- [x] Pill floats above all windows without stealing focus (native macOS APIs)

---

## Required Context

### Documentation & References
```yaml
- doc: https://console.groq.com/docs/speech-to-text
  critical: "whisper-large-v3-turbo is the fastest model. Max 25MB. Supports WAV."

- doc: https://python-sounddevice.readthedocs.io/en/latest/
  critical: "Use InputStream with callback. dtype=int16 for WAV. 16kHz mono."

- doc: https://pynput.readthedocs.io/en/latest/keyboard.html
  critical: "macOS requires Accessibility permission. Use Ctrl+Shift as global hotkey."

- doc: https://doc.qt.io/qtforpython-6/
  critical: "FramelessWindowHint + WindowStaysOnTopHint + Tool. WA_TranslucentBackground."

- doc: https://pyobjc.readthedocs.io/
  critical: "Use AppKit.NSFloatingWindowLevel + NSWindowStyleMaskNonactivatingPanel to float without stealing focus."
```

### Architecture
```
vflow/
├── main.py                 # Entry point — wires hotkey → recorder → transcriber → clipboard
├── config.py               # Centralized configuration (loads .env)
├── ui/
│   ├── __init__.py
│   ├── pill_widget.py      # Floating pill overlay (native macOS window via PyObjC)
│   └── audio_visualizer.py # Animated audio bars (QPainter + QTimer)
├── core/
│   ├── __init__.py
│   ├── recorder.py         # Audio capture with sounddevice InputStream
│   ├── transcriber.py      # Groq Whisper API client
│   ├── hotkey.py           # Global hotkey detection (Ctrl+Shift hold + double-tap Ctrl)
│   └── clipboard.py        # Focus save/restore + pbcopy + AppleScript paste
├── db/
│   ├── __init__.py
│   └── database.py         # SQLite CRUD for transcriptions
├── web/
│   ├── __init__.py
│   └── server.py           # Flask web dashboard (localhost:5000)
├── logo.png                # Vflow brand logo (full size)
├── logo_small.png          # Vflow brand logo (22x22 for pill)
├── requirements.txt
├── .env                    # GROQ_API_KEY (not committed)
├── .env.example
└── .gitignore
```

### Data Model (SQLite)
```sql
CREATE TABLE IF NOT EXISTS transcriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    language TEXT,
    duration_seconds REAL,
    model TEXT DEFAULT 'whisper-large-v3-turbo',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_transcriptions_created_at ON transcriptions(created_at);
```

---

## Implementation Blueprint

### Phase 0: Project Setup
**Objective**: Install system dependencies and create project structure

- [x] Install portaudio via Homebrew (`brew install portaudio`)
- [x] Create Python virtual environment (`python3 -m venv venv`)
- [x] Create requirements.txt with all dependencies
- [x] `pip install -r requirements.txt`
- [x] Create directory structure (`ui/`, `core/`, `db/`, `web/`)
- [x] Create `.env` with `GROQ_API_KEY`
- [x] Create `.env.example` and `.gitignore`

**Validation**: `python3 -c "import PyQt6, sounddevice, pynput, groq, flask; print('OK')"`

### Phase 1: Config & Database
**Objective**: Centralized configuration and SQLite working

- [x] Create `config.py` — loads `.env`, defines all constants (UI dimensions, audio params, paths)
- [x] Create `db/database.py` — init DB, insert, get_recent, search, count
- [x] Validate DB creates and can insert/read

**Validation**: Test insert + query on SQLite

### Phase 2: Audio Capture
**Objective**: Capture microphone audio with sounddevice

- [x] Create `core/recorder.py` — InputStream with callback
- [x] Callback feeds `queue.Queue` (for visualization) AND `frames` list (for WAV)
- [x] Implement `get_wav_buffer()` — converts frames to in-memory WAV (BytesIO)
- [x] Implement `get_duration()` — calculates recording duration from frame count

**Validation**: Record 3 seconds, save WAV, verify it sounds correct

### Phase 3: Groq Transcription
**Objective**: Send audio to Groq Whisper and receive text

- [x] Create `core/transcriber.py` — sends WAV buffer to Groq API
- [x] Handle API errors gracefully
- [x] Validate transcription in Spanish and English

**Validation**: Record audio → transcribe → verify correct text

### Phase 4: Clipboard + Auto-Paste
**Objective**: Copy text to clipboard and auto-paste where cursor is

- [x] Create `core/clipboard.py`
- [x] `save_frontmost_app()` — saves active app via AppleScript before recording
- [x] `paste_text()` — pbcopy + restore focus to saved app + AppleScript `keystroke "v" using command down`
- [x] Handles focus restoration with 120ms delay for app switch

**Key insight**: pyautogui/pyperclip are unreliable on macOS with modifier keys. Native pbcopy + AppleScript is the correct approach.

**Validation**: Copy text, open TextEdit, verify it pastes correctly

### Phase 5: UI — Floating Pill
**Objective**: Frameless floating window that NEVER steals focus

- [x] Create `ui/pill_widget.py` — frameless, transparent, always-on-top
- [x] States: idle (logo only, 34px), recording (expanded 120px with bars), processing (spinner), done (checkmark), error (X)
- [x] Smooth width animation between states (lerp at 0.22 factor)
- [x] Draggable (mouse press+move)
- [x] **Native macOS floating**: Uses PyObjC/AppKit to set `NSFloatingWindowLevel` + `NSWindowStyleMaskNonactivatingPanel` — same technique as Spotlight and Wispr Flow
- [x] Visible on all Spaces/desktops, doesn't hide when app loses focus

**Critical macOS fix**: Qt's `WindowDoesNotAcceptFocus` flag alone doesn't work on macOS. Must use native Cocoa APIs via PyObjC in `showEvent()`:
```python
ns_window.setLevel_(AppKit.NSFloatingWindowLevel)
ns_window.setStyleMask_(ns_window.styleMask() | AppKit.NSWindowStyleMaskNonactivatingPanel)
ns_window.setHidesOnDeactivate_(False)
ns_window.setCollectionBehavior_(
    AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
    | AppKit.NSWindowCollectionBehaviorStationary
    | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
)
```

**Validation**: Pill appears, can be dragged, transitions between states, does NOT steal focus

### Phase 6: Audio Visualizer
**Objective**: Animated bars that react to audio in real-time

- [x] Create `ui/audio_visualizer.py` — QPainter + QTimer at 30 FPS
- [x] 8 bars, monochrome white, opacity scales with amplitude
- [x] Smooth decay (0.80 factor) for elegant fall-off
- [x] Reads from recorder's `audio_queue` (thread-safe via `queue.Queue`)

**Validation**: Bars move when speaking

### Phase 7: Global Hotkey
**Objective**: Detect Ctrl+Shift (hold) and double-tap Ctrl (hands-free)

- [x] Create `core/hotkey.py` — pynput Listener
- [x] **Hold mode**: Ctrl+Shift held → `pressed` signal, either released → `released` signal
- [x] **Hands-free mode**: Double-tap Ctrl within 400ms → `pressed` signal, next single Ctrl press → `released` signal
- [x] Emits Qt signals (`pressed`, `released`) for main thread connection

**Validation**: Both hotkey modes detected in any app

### Phase 8: Integration (main.py)
**Objective**: Wire all modules together

- [x] Create `main.py` — orchestrates hotkey → recorder → transcriber → clipboard → DB
- [x] **Qt signal threading**: ALL cross-thread signals use explicit `Qt.ConnectionType.QueuedConnection`
- [x] Transcription runs in background `threading.Thread` (daemon)
- [x] `signal.signal(signal.SIGINT, signal.SIG_DFL)` for clean Ctrl+C exit
- [x] Starts Flask web server on port 5000 in daemon thread
- [x] Saves frontmost app on hotkey press, restores on paste

**Critical threading fix**: pynput emits signals from its own thread. Both QObjects live in the main thread, so Qt's `AutoConnection` incorrectly chooses `DirectConnection`. This causes UI modifications from the wrong thread (undefined behavior on macOS). Solution: explicit `QueuedConnection`.

**Validation**: Full E2E flow works — hotkey → record → transcribe → paste → save

### Phase 9: Web Dashboard
**Objective**: Browser-based transcription history viewer

- [x] Create `web/server.py` — Flask app with Tailwind CSS
- [x] Dark theme, Vflow branding, glass morphism design
- [x] Table view with time, text, duration, copy button
- [x] Click-to-expand for long transcriptions
- [x] Search filter
- [x] Auto-refresh every 5 seconds
- [x] Runs in daemon thread on `localhost:5000`

**Validation**: Open browser to localhost:5000, see transcription history

---

## Validation Loop

### Level 1: Imports
```bash
source venv/bin/activate
python3 -c "import PyQt6, sounddevice, pynput, groq, flask, AppKit; print('All OK')"
```

### Level 2: Each Module
```bash
python3 -c "from db.database import TranscriptionDB; db = TranscriptionDB(); print('DB OK')"
python3 -c "from core.recorder import AudioRecorder; print('Recorder OK')"
python3 -c "from core.transcriber import Transcriber; print('Transcriber OK')"
python3 -c "from core.hotkey import HotkeyListener; print('Hotkey OK')"
python3 -c "from ui.pill_widget import PillWidget; print('Pill OK')"
```

### Level 3: Integration
```bash
python3 main.py
# Verify: pill appears, hotkey works, audio records, text pastes, dashboard at :5000
```

---

## Known Gotchas

```python
# CRITICAL: macOS requires Accessibility permission for pynput
# System Settings → Privacy & Security → Accessibility → add Terminal/IDE

# CRITICAL: macOS requires Microphone permission for sounddevice
# Requested automatically on first use

# CRITICAL: Qt signal threading — pynput emits from its own thread
# MUST use Qt.ConnectionType.QueuedConnection, NOT AutoConnection
# AutoConnection chooses DirectConnection when both QObjects are in main thread
# but the emit() comes from pynput's thread → undefined behavior on macOS

# CRITICAL: macOS floating window without focus steal
# Qt flags alone (WindowDoesNotAcceptFocus) don't work on macOS
# Must use native Cocoa APIs via PyObjC: NSFloatingWindowLevel + NSNonactivatingPanel
# Applied in showEvent() after the native window is created

# CRITICAL: Auto-paste — don't use pyautogui on macOS
# Modifier key state can interfere. Use pbcopy + AppleScript keystroke "v"
# Must save/restore frontmost app because recording UI can steal focus

# CRITICAL: portaudio must be installed BEFORE pip install sounddevice
# brew install portaudio

# CRITICAL: Python 3.14+ on Homebrew is externally-managed
# Must use: python3 -m venv venv && source venv/bin/activate

# IMPORTANT: Short recordings (<0.3s) are accidental taps — skip transcription

# IMPORTANT: Double-tap Ctrl detection uses 400ms window
# Too short = hard to trigger, too long = false positives
```

---

## Anti-Patterns to Avoid

- DO NOT use toggle for hold-mode hotkey — must be press-and-hold
- DO NOT process audio on the main thread (blocks UI)
- DO NOT call Groq API on the main thread (blocks UI)
- DO NOT touch Qt widgets from secondary threads
- DO NOT use pyautogui/pyperclip for paste on macOS — use native AppleScript
- DO NOT use Qt's AutoConnection for cross-thread signals — use QueuedConnection
- DO NOT rely on Qt window flags alone for macOS floating windows — use PyObjC
- DO NOT hardcode the API key in source code
- DO NOT record in float32 and send directly to WAV — use int16

---

## Dependencies

```bash
# System (Homebrew)
brew install portaudio

# Python (in virtual environment)
pip install PyQt6 sounddevice numpy pynput groq python-dotenv flask pyobjc-framework-Cocoa
```

---

## Environment Variables

```env
# .env
GROQ_API_KEY=your_groq_api_key_here
```

Get your free API key at: https://console.groq.com/keys

---

## Environment

- **Python**: 3.12+ (tested on 3.14)
- **macOS**: 15+ (requires Accessibility permissions)
- **Stack**: Python + PyQt6 + sounddevice + Groq Whisper + SQLite + Flask + PyObjC
- **Cost**: ~$0.02/hour of transcription (vs $15/month for Wispr Flow)
