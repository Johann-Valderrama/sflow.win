<p align="center">
  <img src="Vpoint%20Logo_v14.png" width="160" alt="Vflow">
</p>

<h1 align="center">Vflow</h1>

<p align="center">
  <strong>Open-source voice-to-text for Windows. Wispr Flow alternative at 99% lower cost.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Windows-10%2B-blue?style=flat-square" alt="Windows">
  <img src="https://img.shields.io/badge/Python-3.12%2B-green?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/STT-Groq%20Whisper-orange?style=flat-square" alt="Groq Whisper">
  <img src="https://img.shields.io/badge/Cost-%240.02%2Fhr-brightgreen?style=flat-square" alt="Cost">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=flat-square" alt="License">
</p>

---

Vflow is a **system-wide voice-to-text tool** for Windows. Hold a hotkey, speak, release — your words appear wherever your cursor is. Any app, any text field, any language.

Built as a replacement for [Wispr Flow](https://wispr.com) ($15/month). Vflow uses [Groq's Whisper API](https://console.groq.com/docs/speech-to-text) at **~$0.02/hour** — roughly **$0.60/month** with heavy daily use.

### Features

- **Windows native** — system tray, starts with Windows, builds to a standalone `.exe`
- **System-wide dictation** — works in any app (VS Code, Chrome, Slack, Notepad, etc.)
- **4 recording modes** — push-to-talk, hands-free toggle, translation hold, translation toggle
- **Real-time translation** — dictate in any language, paste in another (12 languages supported)
- **Floating pill UI** — minimal draggable overlay with real-time audio visualization; appears on current monitor and repositions if monitors change
- **No focus stealing** — pill floats above everything without interrupting your work
- **Audio feedback** — distinct beeps on recording start and transcription done (configurable volume)
- **Auto-paste** — text appears exactly where your cursor was
- **Chunked transcription** — records indefinitely; splits into 60-second chunks internally so nothing is lost
- **Web dashboard** — browse, search, edit, and manage transcription history at `localhost:5678`
- **Dashboard settings** — change language, microphone, translation target, and sound volume without editing any file
- **SQLite history** — every transcription saved locally with timestamp and duration
- **First-run setup** — asks for your Groq API key on first launch, no config files to edit
- **Encrypted API key** — GROQ_API_KEY encrypted with Windows DPAPI (only your user on this machine can decrypt)
- **Privacy controls** — disable history recording, set auto-deletion retention, no cloud sync
- **Robust audio handling** — automatic microphone disconnect detection (~2s timeout) stops recording gracefully and displays error notification
- **Accidental trigger prevention** — arming delay on hold-mode hotkeys prevents IDE shortcuts and other Ctrl+Alt combos from accidentally starting recording

---

## Quick Start

### Prerequisites

- Windows 10+
- Python 3.12+
- [Groq API key](https://console.groq.com/keys) (free tier available)

### Install (Dev Mode)

```bash
git clone https://github.com/Johann-Valderrama/sflow.win.git
cd sflow.win
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Edit .env and paste your GROQ_API_KEY
python main.py
```

### Build Executable

```bash
build.bat
# Output: dist\Vflow\Vflow.exe
# Validates Python venv activation; aborts if PyInstaller fails
```

On first launch Vflow checks for a Groq API key. If none is found, a setup dialog appears — paste your key and you're done. Builds use pinned dependency versions for reproducible binaries across machines.

---

## Usage

### Hotkeys

| Action | Shortcut |
|--------|----------|
| **Push-to-talk** (transcribe) | Hold `Ctrl+Alt`, speak, release |
| **Hands-free** (transcribe) | Triple-tap `Shift` to start, tap `Shift` once to stop |
| **Push-to-talk** (translate) | Hold `Ctrl+Shift+Alt`, speak, release |
| **Hands-free** (translate) | Press `AltGr+Space` to start, press again to stop |

> **Hands-free tip:** Triple-tap Shift starts recording. A single Shift tap stops it. Great for long dictations where holding a key gets tiring. Mode 4 (AltGr+Space) works similarly for translation.

### System Tray

| Option | Description |
|--------|-------------|
| **Vflow v1.0.0** | App version shown in menu and tooltip |
| **Abrir Dashboard (:5678)** | Opens the transcription history in your browser |
| **Iniciar con Windows** | Toggle auto-start on login (writes to Windows Registry with quoted exe path to handle folder spaces; auto-repairs if exe moved) |
| **Salir** | Quit (or `Ctrl+C` in dev mode) |

### Pill States

| State | Visual |
|-------|--------|
| Idle | Small pill with logo |
| Recording | Expanded pill with animated audio bars |
| Processing | Spinning dots |
| Done | Green checkmark (auto-dismisses after 0.8s) |
| Error | Red X (auto-dismisses after 1.2s) |

The pill is **draggable** — left-click and drag to reposition it anywhere on screen.

---

## Translation

Vflow can translate speech in real time:

- **Hold** `Ctrl+Shift+Alt` (or use the hands-free translate toggle `AltGr+Space`) to record in translation mode
- The transcribed audio is automatically translated to your target language
- For English output: uses Whisper's native `/audio/translations` endpoint (highest quality)
- For other target languages: transcribes first, then uses `llama-3.1-8b-instant` as an LLM translator

**Supported target languages:** Spanish, English, French, German, Italian, Portuguese, Japanese, Chinese, Korean, Russian, Arabic, Dutch

Configure the target language in the dashboard Settings panel or via the `TRANSLATE_TARGET_LANG` environment variable.

---

## Web Dashboard

Access at `http://localhost:5678` (or click **Abrir Dashboard** in the system tray).

### Transcription Table
- Full history with timestamp, duration, and text
- Click any row to expand the full text
- **Inline editing** — click the edit icon to correct a transcription in place
- **Copy to clipboard** — one click per row

### Search & Filter
- Live text search as you type

### Bulk Actions
- Multi-select rows with checkboxes
- Delete selection, or use the **Limpiar** dropdown to delete:
  - Today's transcriptions
  - Last 7 days
  - Last 30 days
  - By custom date
  - All transcriptions

### Settings Panel (⚙)
All settings persist to the `.env` file — no manual editing needed.

| Setting | Description |
|---------|-------------|
| **Idioma** | Whisper input language (`es`, `en`, `fr`, `de`, `it`, `pt`, `ja`, `zh`, `auto`) |
| **Micrófono** | Select input device from detected audio devices |
| **Traducción** | Target language for translation mode |
| **Sonidos** | Enable/disable audio feedback beeps |
| **Volumen** | Beep volume (1–10) |

---

## Architecture

```
Hotkey Press (pynput thread)
  → [QueuedConnection] → save_frontmost_app() + recorder.start()
  → pill.set_state(RECORDING) + beep 880 Hz

sounddevice callback → queue.Queue → QTimer → audio_visualizer (FFT bars, spring physics)

Hotkey Release
  → recorder.stop() → pill.set_state(PROCESSING)
  → Thread: transcriber.transcribe(wav_buffer)  ← Groq Whisper API
    (long recordings: 60s chunks processed in parallel with 1s overlap)
  → paste_text() [Win32 API + pynput Ctrl+V] + db.insert() + beep 660 Hz
  → pill.set_state(DONE)
```

Key technical decisions:
- **PyQt6 window flags** — pill stays on top without stealing focus (`FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowDoesNotAcceptFocus`)
- **Qt QueuedConnection** — all pynput→Qt signals cross the thread boundary safely
- **Win32 API** — `GetForegroundWindow` saves target app; `GlobalAlloc`/`GlobalLock`/`SetClipboardData` via ctypes writes UTF-16 text directly (no subprocess); `SetForegroundWindow` restores focus before Ctrl+V
- **FFT + spring physics** — visualizer uses frequency analysis and spring simulation for smooth, natural bar animations
- **Chunked recording** — recordings split every 60 seconds with 1-second overlap; chunks transcribed concurrently; final chunk stitched with prompt continuity to avoid cut-off words
- **Audio device caching** — microphone device is cached on first resolution; device list not re-scanned per-recording unless device name changes. Visualization queue cleared on stop to prevent memory bloat in long sessions.

---

## Customization

All tunable constants live in `config.py`:

```python
# Version
APP_VERSION = "1.0.0"       # displayed in system tray menu and tooltip

# Hotkey
DOUBLE_TAP_INTERVAL = 0.4   # seconds between taps for hands-free detection
ARMING_DELAY = 0.15         # seconds to hold Ctrl+Alt (or Ctrl+Shift+Alt) before recording starts

# UI
PILL_WIDTH_IDLE = 34          # width when idle (logo only)
PILL_WIDTH_RECORDING = 100    # width during recording
PILL_WIDTH_STATUS = 52        # width for checkmark/spinner/error
PILL_HEIGHT = 34
PILL_OPACITY = 0.90
PILL_CORNER_RADIUS = 17
PILL_MARGIN_BOTTOM = 14       # distance from bottom of screen

# Audio
SAMPLE_RATE = 16000           # 16kHz mono (optimal for speech)
NUM_BARS = 20                 # visualizer bar count
VIZ_FPS = 60
BAR_GAIN = 8.0                # bar sensitivity
BAR_DECAY = 0.85              # bar fall-off speed

# Chunked recording
CHUNK_SECONDS = 60            # split long recordings every N seconds
MAX_RECORDING_SECONDS = 600   # hard safety cutoff (auto-stop at 10 min)

# STT
GROQ_MODEL = "whisper-large-v3-turbo"   # fastest Groq model
```

Additional settings via environment variables (`.env` or dashboard):

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | — | Your Groq API key (`gsk_...`); encrypted with DPAPI on first use |
| `SAVE_HISTORY` | `true` | Set to `false` to disable recording transcriptions to database |
| `HISTORY_RETENTION_DAYS` | `0` | Auto-delete transcriptions older than N days; `0` = keep forever |
| `WHISPER_LANGUAGE` | `es` | Input language (`auto` for auto-detect) |
| `TRANSLATE_TARGET_LANG` | `en` | Translation output language |
| `AUDIO_DEVICE_NAME` | *(default mic)* | Substring of microphone name to use |
| `SOUNDS_ENABLED` | `true` | Enable/disable beep feedback |
| `BEEP_VOLUME_STEPS` | `2` | Beep volume 1–10 |
| `RESTORE_CLIPBOARD` | `false` | Restore clipboard content after paste |

---

## Cost Comparison

| | Wispr Flow | Vflow |
|---|---|---|
| Monthly cost | $15/month | ~$0.60/month* |
| Annual cost | $180/year | ~$7.20/year* |
| Translation | Included | Included |
| Data control | Third-party cloud | Local only |
| Customizable | No | Fully |
| Open source | No | Yes |

*\*Estimated for ~30 hours of transcription per month at $0.02/hour Groq pricing.*

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Pill doesn't appear | Try running as Administrator |
| Audio not captured | Check Microphone permissions in Windows Settings → Privacy |
| Paste goes to wrong app | Ensure the target app was focused before recording started |
| Hotkeys not working in some apps | Run as Administrator (required for elevated apps like Task Manager) |
| Dashboard not loading | Port auto-selects from 5678: `netstat -an \| findstr 5678` |
| Transcription hangs | Check your `GROQ_API_KEY` is valid; API timeout is 10 seconds |
| Wrong language transcribed | Set `WHISPER_LANGUAGE` in dashboard Settings (or use `auto` to detect) |
| Beeps too loud / quiet | Adjust **Volumen** in dashboard Settings (1–10) |
| Only one instance allowed | Single-instance mutex prevents multiple launches; second instance shows warning and exits |
| Text in clipboard but not pasted | If window verification fails, text stays in clipboard; paste manually with Ctrl+V |
| Failed recording saved for debugging | Check `%APPDATA%\Vflow\last_failed_recording.wav` if transcription API/network error occurs |
| API key plaintext in `.env` | On first launch, plaintext keys are automatically encrypted with DPAPI; no manual action needed |
| Disable history recording | Set `SAVE_HISTORY=false` in `.env` or toggle in dashboard Settings; transcriptions paste but won't be stored |
| Auto-delete old transcriptions | Set `HISTORY_RETENTION_DAYS=N` (e.g., `30`) to delete entries older than N days on startup |
| IDE hotkeys trigger recording | Increase `ARMING_DELAY` in `config.py` (default 0.15s) to require longer hold in Ctrl+Alt/Ctrl+Shift+Alt modes before recording starts |
| Microphone disconnected during recording | App detects silence within ~2s, stops recording, shows error on pill, and displays "Micrófono desconectado" tray notification |
| Pill off-screen after monitor changes | Pill auto-redetects current monitor and repositions. If still off-screen, restart app or drag pill with mouse to visible area |

---

## Privacy & Security

- **Your API key stays on your machine**: Encrypted with Windows DPAPI; only your user account can decrypt it
- **No cloud sync**: All transcriptions stored locally in SQLite; never uploaded
- **Offline capable**: Once you have your API key, you can disable internet after startup (API calls require internet)
- **Open source**: Audit the code yourself — no hidden telemetry or data collection
- **Privacy controls**: Disable history recording with `SAVE_HISTORY=false`, or set auto-deletion with `HISTORY_RETENTION_DAYS`

---

## License

MIT License. Do whatever you want with it.

---

<p align="center">
  Original macOS version by <a href="https://github.com/daniel-carreon/sflow">daniel-carreon</a><br>
  Windows version by <a href="https://github.com/Johann-Valderrama">Johann-Valderrama</a> — built with Claude Opus 4.6
</p>
