# CLAUDE.md — Vflow Development Instructions

## What is Vflow?

Vflow is a Windows voice-to-text desktop tool that replaces Wispr Flow ($15/month). It captures audio via global hotkeys, transcribes using Groq Whisper API (~$0.02/hour), and auto-pastes text wherever the cursor is. It includes a floating pill UI overlay, real-time audio visualization, SQLite history, and a web dashboard.

## Quick Start (Dev Mode)

```bash
# 1. Create virtual environment
python -m venv venv
venv\Scripts\activate

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Set up environment
copy .env.example .env
# Edit .env and add your GROQ_API_KEY (get one at https://console.groq.com/keys)

# 4. Run
python main.py
```

## Build Desktop App (.exe)

```bash
# Build Vflow.exe (uses PyInstaller)
build.bat
```

The built app is in `dist\Vflow\Vflow.exe`. On first launch, if no API key exists in `%APPDATA%\Vflow\.env`, a dialog asks for it.

### Build Requirements
- Python 3.12+ with venv
- PyInstaller (installed automatically by build.bat)
- Optional: Vflow.ico (256x256 icon file for the .exe)

## Permissions Required

- **Administrator** (optional): May be needed for global hotkeys in some apps
- **Microphone**: Automatically requested on first use

## Project Structure

```
vflow/
├── main.py                 # Entry point — tray icon, first-run dialog, launch-at-login, app controller
├── config.py               # All configuration constants (UI, audio, paths, bundle detection)
├── vflow.spec              # PyInstaller spec for building .exe
├── build.bat               # One-shot build script for Windows
├── ui/
│   ├── pill_widget.py      # Floating pill overlay (PyQt6 window flags)
│   └── audio_visualizer.py # Real-time audio bars
├── core/
│   ├── recorder.py         # sounddevice audio capture
│   ├── transcriber.py      # Groq Whisper API client (lazy init, 10s timeout)
│   ├── hotkey.py           # Global hotkeys (4 modes: Ctrl+Alt, triple-tap Shift, Ctrl+Shift+Alt, AltGr+Space)
│   └── clipboard.py        # Focus save/restore + Ctrl+V paste via Win32 API (GlobalAlloc/SetClipboardData/ctypes)
├── db/
│   └── database.py         # SQLite CRUD
├── web/
│   └── server.py           # Flask dashboard at localhost:5678 (auto-finds free port)
├── logo.png                # Brand logo (full size)
├── logo_small.png          # Brand logo (22x22 for tray + pill)
├── requirements.txt
├── .env                    # GROQ_API_KEY (never committed)
└── .env.example
```

## Architecture & Data Flow

```
Hotkey Press (pynput thread)
  → [QueuedConnection] → save_frontmost_app() + recorder.start()
  → pill.set_state(RECORDING)
  → sounddevice callback → queue.Queue → QTimer → audio_visualizer paints bars

Hotkey Release (pynput thread)
  → [QueuedConnection] → recorder.stop()
  → pill.set_state(PROCESSING)
  → background Thread: transcriber.transcribe(wav_buffer)
    → Groq Whisper API returns text
    → [QueuedConnection] → paste_text() + db.insert() + pill.set_state(DONE)
```

## Critical Implementation Details

### 1. Qt Signal Threading (MUST use QueuedConnection)
pynput emits signals from its own thread. Both QObjects live in the main thread, so Qt's `AutoConnection` incorrectly chooses `DirectConnection`. But since `emit()` comes from pynput's thread, UI modifications happen on the wrong thread — undefined behavior. **Always use explicit `Qt.ConnectionType.QueuedConnection`.**

### 2. Floating Window (Qt Window Flags)
The pill uses `FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowDoesNotAcceptFocus` to float above all windows without stealing focus.

### 3. Auto-Paste (Win32 API + pynput)
- `save_frontmost_app()` saves the foreground window handle via `GetForegroundWindow()`
- `_set_clipboard_text()` copies text using Win32 API directly (GlobalAlloc, GlobalLock, SetClipboardData, CF_UNICODETEXT) via ctypes — no subprocess/PowerShell
- `SetForegroundWindow()` to restore focus
- pynput `Controller` to simulate Ctrl+V

### 4. Audio Pipeline (thread-safe)
sounddevice callback runs in audio thread — NEVER touch Qt widgets from it. Use `queue.Queue` as bridge:
- Callback → puts audio chunks in queue
- QTimer on main thread → polls queue → updates visualizer

### 5. Short Recording Filter
Recordings under 0.3 seconds are accidental taps — skip transcription and return to idle.

### 6. Bundle vs Dev Mode (config.py)
`config.py` detects `sys.frozen` to switch between dev and .exe bundle:
- **Dev mode**: assets and data live in the project root directory
- **Bundle mode**: read-only assets (logo) come from `sys._MEIPASS`, writable data (DB, .env) goes to `%APPDATA%\Vflow\`

### 7. Desktop App Features (main.py)
- **System Tray**: QSystemTrayIcon with dashboard link, "Iniciar con Windows" toggle, quit
- **First-Run Dialog**: If GROQ_API_KEY is empty, shows a QDialog to enter it (saves to %APPDATA%\Vflow)
- **Launch at Login**: Uses Windows Registry (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`)

### 8. Port Selection (web/server.py)
Default port is 5678. Auto-scans for free port if occupied.

## Customization

### Hotkeys
Edit `core/hotkey.py`:
- **Mode 1 (Ctrl+Alt hold)**: Press and hold Ctrl+Alt to transcribe; release to stop. Transcribed text auto-pastes.
- **Mode 2 (Triple-tap Shift)**: Press Shift three times within 400ms to start hands-free transcription; press Shift once to stop.
- **Mode 3 (Ctrl+Shift+Alt hold)**: Press and hold Ctrl+Shift+Alt (Shift before Alt) to translate from any language to target language.
- **Mode 4 (AltGr+Space toggle)**: Press AltGr+Space once to start translation hands-free; press again to stop.
- To customize intervals, edit `DOUBLE_TAP_INTERVAL` in `config.py`.

### UI Dimensions
Edit `config.py`:
- `PILL_WIDTH_IDLE` (34) — width when just showing logo
- `PILL_WIDTH_RECORDING` (100) — width during recording with bars
- `PILL_WIDTH_STATUS` (52) — width for checkmark/spinner/error
- `PILL_HEIGHT` (34) — height of pill
- `PILL_MARGIN_BOTTOM` (14) — distance from bottom of screen

### Audio
Edit `config.py`:
- `SAMPLE_RATE` (16000) — 16kHz is optimal for speech
- `NUM_BARS` (20) — number of visualizer bars
- `BAR_GAIN` (8.0) — sensitivity of bars
- `BAR_DECAY` (0.85) — how quickly bars fall

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Pill doesn't appear | Try running as Administrator |
| Audio not captured | Check Microphone permissions in Windows Settings → Privacy |
| Paste doesn't work | Run as Administrator for global hotkey/paste access |
| Ctrl+C doesn't kill the process | This is handled by `signal.signal(signal.SIGINT, signal.SIG_DFL)` in main.py |
| Short taps trigger transcription | Adjust the 0.3s threshold in `main.py` `_on_hotkey_released` |
| Web dashboard not loading | Port auto-selects from 5678. Check: `netstat -an \| findstr 5678` |
| Transcription hangs forever | API timeout is 10s. Check your GROQ_API_KEY is valid |
| Only one instance should run | Single-instance mutex (Win32 `Local\VflowSingleInstance`) prevents multiple launches; second instance shows warning and exits |
| Text doesn't paste after recording | If window verification fails, text is left in clipboard with tray notification; paste manually via Ctrl+V |
| Transcription failed — audio saved | Failed recordings saved to `%APPDATA%\Vflow\last_failed_recording.wav` for debugging (API/network errors) |
| Logs not showing up | Dev mode logs to project directory; bundled app logs to `%APPDATA%\Vflow\vflow.log` (RotatingFileHandler, 5 MB per file) |
| Clipboard content changed after paste | By default, clipboard is not restored. Set `RESTORE_CLIPBOARD=true` in `.env` to keep original clipboard content |
