<p align="center">
  <img src="logo.png" width="120" alt="SFlow Logo">
</p>

<h1 align="center">SFlow</h1>

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

SFlow is a **system-wide voice-to-text tool** for Windows. Hold a hotkey, speak, release — your words appear wherever your cursor is. Any app, any text field, any language.

Built as a replacement for [Wispr Flow](https://wispr.com) ($15/month). SFlow uses [Groq's Whisper API](https://console.groq.com/docs/speech-to-text) at **~$0.02/hour** — that's roughly **$0.60/month** with heavy daily use.

### Features

- **Windows native** — lives in the system tray, starts with Windows
- **System-wide dictation** — works in any app (VS Code, Chrome, Slack, Notepad, etc.)
- **Two recording modes** — hold Ctrl+Alt (push-to-talk) or double-tap Ctrl (hands-free)
- **Floating pill UI** — minimal overlay with real-time audio visualization bars
- **No focus stealing** — pill floats above everything without interrupting your work
- **Auto-paste** — text appears exactly where your cursor was
- **Web dashboard** — browse, search, and copy transcription history at `localhost:5678`
- **SQLite history** — every transcription saved locally with timestamp and duration
- **Multilingual** — supports all languages Whisper supports (English, Spanish, French, etc.)
- **First-run setup** — asks for your Groq API key on first launch, no config files to edit

---

## Quick Start

### Prerequisites

- Windows 10+
- Python 3.12+
- [Groq API key](https://console.groq.com/keys) (free tier available)

### Install (Dev Mode)

```bash
git clone https://github.com/Johann-valderrama/sflow.win.git
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
# Output: dist\SFlow\SFlow.exe
```

On first launch SFlow asks for your [Groq API key](https://console.groq.com/keys).

---

## Usage

| Action | Shortcut |
|--------|----------|
| **Push-to-talk** | Hold `Ctrl+Alt`, speak, release |
| **Hands-free** | Double-tap `Ctrl` to start, tap `Ctrl` to stop |
| **View history** | Click "Abrir Dashboard" in system tray, or `http://localhost:5678` |
| **Start with Windows** | Toggle in system tray → "Iniciar con Windows" |
| **Quit** | System tray → "Salir" (or `Ctrl+C` in dev mode) |

### Pill States

| State | Visual |
|-------|--------|
| Idle | Small pill with logo |
| Recording | Expanded pill with animated audio bars |
| Processing | Spinning dots |
| Done | Green checkmark (auto-dismisses) |
| Error | Red X (auto-dismisses) |

---

## Architecture

```
Hotkey (pynput) → Audio Capture (sounddevice) → Groq Whisper API → Auto-Paste (Win32 + pynput)
                        ↓                                                    ↓
                  Audio Bars (QPainter)                              SQLite Database
                        ↓                                                    ↓
                  Floating Pill (PyQt6)                             Web Dashboard (Flask)
```

Key technical decisions:
- **PyQt6 window flags** for floating pill that stays on top without stealing focus
- **Qt QueuedConnection** for thread-safe signals between pynput and UI
- **Win32 API** for saving/restoring foreground window + pynput for Ctrl+V paste
- **sounddevice + queue.Queue** for thread-safe audio visualization

---

## Customization

All configuration lives in `config.py`:

```python
# Hotkey
DOUBLE_TAP_INTERVAL = 0.4  # seconds for double-tap detection

# UI
PILL_WIDTH_IDLE = 34        # pill width when idle (logo only)
PILL_WIDTH_RECORDING = 100  # pill width during recording
PILL_HEIGHT = 34            # pill height

# Audio
SAMPLE_RATE = 16000         # 16kHz mono (optimal for speech)
NUM_BARS = 20               # number of visualizer bars
BAR_GAIN = 8.0              # bar sensitivity
BAR_DECAY = 0.85            # bar fall-off speed

# STT
GROQ_MODEL = "whisper-large-v3-turbo"  # fastest Groq model
```

---

## Cost Comparison

| | Wispr Flow | SFlow |
|---|---|---|
| Monthly cost | $15/month | ~$0.60/month* |
| Annual cost | $180/year | ~$7.20/year* |
| Data control | Third-party | Local |
| Customizable | No | Fully |
| Open source | No | Yes |

*\*Estimated for ~30 hours of transcription per month at $0.02/hour Groq pricing.*

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Pill doesn't appear | Try running as Administrator |
| Audio not captured | Check Microphone permissions in Windows Settings → Privacy |
| Paste goes to wrong app | Ensure the app was focused before recording started |
| Ctrl+C doesn't quit | Should work out of the box (SIGINT handler) |
| Dashboard not loading | Port auto-selects from 5678: `netstat -an \| findstr 5678` |

---

## License

MIT License. Do whatever you want with it.

---

<p align="center">
  Built with Claude Opus 4.6 in a single session.<br>
  <sub>Windows version by <a href="https://github.com/Johann-valderrama">Johann-valderrama</a> — <strong>S</strong><strong>f</strong>low</sub>
</p>
