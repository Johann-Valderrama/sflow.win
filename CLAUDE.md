# CLAUDE.md — Vflow Development Instructions

## What is Vflow?

Vflow is a Windows voice-to-text desktop tool that replaces Wispr Flow ($15/month). It captures audio via global hotkeys, transcribes using Groq Whisper API (~$0.02/hour), and auto-pastes text wherever the cursor is. It includes a floating pill UI overlay, real-time audio visualization, SQLite history, and a web dashboard.

## Diccionario personal

El diccionario personal permite (a) añadir vocabulario propio para que Whisper lo reconozca correctamente (se inyecta al final del prompt de contexto, hasta ~480 caracteres) y (b) corregir transcripciones en tiempo real mediante pares "escucho X → escribo Y" con regex whole-word case-aware aplicado tras la transcripción. Se gestiona desde la tabla `dictionary` en la misma SQLite (independiente de `SAVE_HISTORY`), con UI en el panel "Diccionario" del dashboard y API REST en `/api/dictionary`. La lógica reside en `core/dictionary.py` (caché en memoria con swap atómico + invalidación perezosa cada 5 min); `core/transcriber.py` la invoca en el orden: backend → filtro de alucinaciones → `apply_replacements`.

### Diccionario v1.1 — Funcionalidades nuevas

- **Pin (★)**: cada entrada puede fijarse como prioritaria. Las entradas pinned aparecen primero en el orden de inclusión del vocabulario del prompt (pinned > hit_count > fecha). Toggle desde el botón ★ en cada fila del panel.
- **Presupuesto de vocabulario**: el panel muestra el % del espacio de prompt usado (y "X de Y términos (espacio lleno)" cuando ya no caben todos) con barra de progreso. Las entradas fuera del presupuesto (~480 chars) aparecen con opacidad reducida con tooltip explicativo; el reemplazo sigue activo aunque no quepan en el prompt.
- **hit_count**: cada vez que un par de reemplazo corrige texto, su contador se incrementa en background (thread daemon, fire-and-forget). Se muestra discretamente como "×N" en la fila. Afecta el orden de vocab en la próxima recompilación.
- **Export/Import CSV**: botones en la cabecera del panel. Export → `GET /api/dictionary/export` (CSV con replace_from, replace_to, pinned). Import → `POST /api/dictionary/import` (multipart o body raw; límite 1000 filas; filas inválidas son skipped).
- **Añadir desde historial**: seleccionar texto en cualquier transcripción de la tabla activa un botón flotante "📖 Añadir al diccionario" que abre el panel Diccionario con el texto seleccionado prellenado en "Cuando escuche…" y el foco en el campo "Palabra".
- **Columnas DB nuevas**: `pinned INTEGER DEFAULT 0`, `source TEXT DEFAULT 'manual'`, `hit_count INTEGER DEFAULT 0`. Migración automática idempotente via `ALTER TABLE ... ADD COLUMN` (captura "duplicate column").
- **API**: `GET /api/dictionary` devuelve `{entries: [...], budget: {included, total, included_ids}}`; `PATCH /api/dictionary/<id>` acepta `{pinned}` además de `{enabled}`; nuevos endpoints `/api/dictionary/export` y `/api/dictionary/import`.

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
- Python 3.12+ with venv (build.bat validates activation and aborts if PyInstaller fails — no silent build errors)
- PyInstaller (installed automatically by build.bat)
- Optional: Vflow.ico (256x256 icon file for the .exe)
- **Reproducible builds**: requirements.txt has pinned versions for consistent .exe output across machines

### Seguridad de dependencias

- **`requirements.in`** — fuente de verdad con las dependencias top-level (sin versiones fijas). Edita este archivo al añadir o quitar una dependencia.
- **`requirements.lock`** — lock file generado con pip-tools, incluye hashes SHA-256 para todas las dependencias (directas y transitivas). Commiteado en el repo.
- **Instalar desde el lock** (máxima seguridad, para CI o entornos limpios):
  ```bash
  pip install --require-hashes -r requirements.lock
  ```
- **Regenerar el lock** al añadir una dependencia:
  ```bash
  pip install pip-tools          # una sola vez
  pip-compile --generate-hashes --allow-unsafe --output-file requirements.lock requirements.in
  ```
  Actualiza también `requirements.txt` con la versión pinneada final si vas a distribuir dev-setup sin lock.
- **Política de versiones**: no añadir paquetes con menos de 30 días en PyPI (riesgo de typosquatting / supply-chain). Verifica la fecha de publicación en https://pypi.org/project/<paquete>/#history antes de añadir una dependencia nueva.
- **Auditoría**: `build.bat` ejecuta `pip-audit` como paso previo (solo warning, no aborta). Para auditoría manual: `pip-audit -r requirements.lock`.

## Permissions Required

- **Administrator** (optional): May be needed for global hotkeys in some apps
- **Microphone**: Automatically requested on first use

## Project Structure

```
vflow/
├── main.py                 # Entry point — tray icon, first-run dialog, launch-at-login, app controller
├── config.py               # All configuration constants (UI, audio, paths, bundle detection); includes APP_VERSION
├── vflow.spec              # PyInstaller spec for building .exe
├── version_info.txt        # Version metadata for .exe (reduces SmartScreen false positives)
├── build.bat               # One-shot build script for Windows (validates venv activation, aborts on PyInstaller failure)
├── ui/
│   ├── pill_widget.py      # Floating pill overlay (PyQt6 window flags)
│   └── audio_visualizer.py # Real-time audio bars
├── core/
│   ├── recorder.py         # sounddevice audio capture
│   ├── transcriber.py      # Orquestador: delega al backend activo + filtro de alucinaciones
│   ├── backends/
│   │   ├── base.py         # ABC TranscriptionBackend (transcribe, translate, is_ready, warmup, release)
│   │   ├── groq_backend.py # Backend Groq Whisper API (requiere internet + GROQ_API_KEY)
│   │   ├── local_backend.py# Backend faster-whisper local (sin internet; solo traduce →en)
│   │   └── __init__.py     # Factory get_backend(); lee TRANSCRIPTION_BACKEND env
│   ├── hotkey.py           # Global hotkeys (4 modes: Ctrl+Alt, triple-tap Shift, Ctrl+Shift+Alt, AltGr+T)
│   ├── vad.py              # Silero VAD wrapper: recorta silencios antes de enviar a Groq (VAD_ENABLED)
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
The pill uses `FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowDoesNotAcceptFocus` to float above all windows without stealing focus. The pill automatically detects the monitor where the cursor is located and appears there; it can be dragged between monitors and will reposition itself if a monitor is disconnected or changes resolution.

### 3. Auto-Paste (Win32 API + pynput)
- `save_frontmost_app()` saves the foreground window handle via `GetForegroundWindow()`
- `_set_clipboard_text()` copies text using Win32 API directly (GlobalAlloc, GlobalLock, SetClipboardData, CF_UNICODETEXT) via ctypes — no subprocess/PowerShell
- `SetForegroundWindow()` to restore focus
- pynput `Controller` to simulate Ctrl+V

### 4. Audio Pipeline (thread-safe)
sounddevice callback runs in audio thread — NEVER touch Qt widgets from it. Use `queue.Queue` as bridge:
- Callback → puts audio chunks in queue
- QTimer on main thread → polls queue → updates visualizer
- **Microphone device caching**: Audio device resolution is cached on first access; device list is not re-scanned on each recording unless the configured device changes. This eliminates O(n) enumeration overhead per recording.
- **Queue memory cleanup**: Visualization queue is cleared when recording stops, preventing memory bloat from long sessions.

### 5. Short Recording Filter
Recordings under 0.3 seconds are accidental taps — skip transcription and return to idle.

### 6. Microphone Watchdog
If the microphone is disconnected during recording (e.g., Bluetooth headphones unplugged), the app detects silence after ~2 seconds with no audio data, automatically stops recording, shows an error state on the pill, and displays a system tray notification "Micrófono desconectado durante la grabación". This prevents the pill from hanging indefinitely. The watchdog monitors audio callback invocations; if no data arrives within the timeout window, recording terminates gracefully.

### 7. Bundle vs Dev Mode (config.py)
`config.py` detects `sys.frozen` to switch between dev and .exe bundle:
- **Dev mode**: assets and data live in the project root directory
- **Bundle mode**: read-only assets (logo) come from `sys._MEIPASS`, writable data (DB, .env) goes to `%APPDATA%\Vflow\`

### 8. Desktop App Features (main.py)
- **System Tray**: QSystemTrayIcon with dashboard link, "Iniciar con Windows" toggle, quit; tray icon tooltip shows app version (from `APP_VERSION` in config.py)
- **Version Display**: Current app version (e.g., "1.0.0") displayed in tray menu and tooltip
- **First-Run Dialog**: If GROQ_API_KEY is empty, shows a QDialog to enter it (saves to %APPDATA%\Vflow)
- **Launch at Login**: Uses Windows Registry (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`); .exe path is quoted to handle spaces in folder names. Registry entry auto-repairs on startup if exe was moved.

### 9. Port Selection (web/server.py)
Default port is 5678. Auto-scans for free port if occupied.

### 10. Dashboard Panels (web/server.py)
The header contains three icon-button panels: **Configuración** (⚙), **Diccionario** (📖), and **Atajos de teclado** (⌨, `id="shortcuts-panel"`). The shortcuts panel is static HTML — no API endpoint required — and renders all four hotkey modes as `<kbd>`-styled cards.

## Security & Privacy

### 1. API Key Encryption (DPAPI)
The GROQ_API_KEY is encrypted with Windows Data Protection API (DPAPI) via crypt32.dll and stored as base64 in `%APPDATA%\Vflow\GROQ_API_KEY_ENC`. Only the same Windows user on the same machine can decrypt it. Plaintext keys are automatically migrated to encrypted format on startup. This replaces the ineffective `os.chmod()` approach (which doesn't work on Windows).

### 2. History Privacy Mode
Configure history saving with `SAVE_HISTORY` in `.env` (default: `true`). Set to `false` to disable database recording — transcriptions paste normally but are never stored. Togglable from the dashboard Settings panel without restarting.

### 3. Automatic History Retention
Set `HISTORY_RETENTION_DAYS` in `.env` (default: `0` = keep forever). If > 0, the app automatically deletes transcriptions older than N days on startup. Configurable from the dashboard without code changes.

### 4. CSRF Hardening
The dashboard validates exact Origin/Referer hosts (`localhost`, `127.0.0.1`, or `::1` only) instead of prefix matching. This closes bypasses like `localhost.evil.com`.

## Customization

### App Version
Edit `config.py`:
- `APP_VERSION` (default: "1.0.0") — Version string displayed in system tray menu and tooltip

### Hotkeys
Edit `core/hotkey.py`:
- **Mode 1 (Ctrl+Alt hold)**: Press and hold Ctrl+Alt to transcribe; release to stop. Transcribed text auto-pastes.
- **Mode 2 (Triple-tap Shift)**: Press Shift three times within 400ms to start hands-free transcription; press Shift once to stop.
- **Mode 3 (Ctrl+Shift+Alt hold)**: Press and hold Ctrl+Shift+Alt (Shift before Alt) to translate from any language to target language.
- **Mode 4 (AltGr+T toggle)**: Press AltGr+T once to start translation hands-free; press again to stop.
- To customize intervals, edit `DOUBLE_TAP_INTERVAL` in `config.py`.
- **Arming Delay** — Edit `ARMING_DELAY` in `config.py` (default: 0.15s). Modes 1 and 3 (hold keys) require the hotkey combination to be pressed for this duration *without other keys* before recording starts. This prevents accidental triggers when using IDE shortcuts like Ctrl+Alt+L. Set to 0 for immediate activation (at the cost of possible misfires).

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

### Environment Variables (`.env`)
- `GROQ_API_KEY` — Your Groq API key (automatically encrypted)
- `SAVE_HISTORY` (default: `true`) — Set to `false` to disable recording transcriptions to database
- `HISTORY_RETENTION_DAYS` (default: `0`) — Auto-delete transcriptions older than N days; `0` keeps forever
- `WHISPER_LANGUAGE` (default: `es`) — Input language for transcription
- `TRANSLATE_TARGET_LANG` (default: `en`) — Target language for translation mode
- `AUDIO_DEVICE_NAME` — Substring of microphone name to use (defaults to system default)
- `SOUNDS_ENABLED` (default: `true`) — Enable/disable audio feedback beeps
- `BEEP_VOLUME_STEPS` (default: `2`) — Beep volume (1–10)
- `RESTORE_CLIPBOARD` (default: `false`) — Restore clipboard content after paste
- `TRANSCRIPTION_BACKEND` (default: `groq`) — Backend activo: `"groq"` (API Groq) o `"local"` (faster-whisper sin internet)
- `LOCAL_WHISPER_MODEL` (default: `small`) — Modelo local: `"small"` (~466 MB, rápido) o `"medium"` (~1.5 GB, más preciso)
- `LOCAL_MODEL_IDLE_MINUTES` (default: `10`) — Minutos de inactividad antes de liberar el modelo de RAM; `0` = nunca liberar
- `GROQ_FALLBACK` (default: `false`) — Si `true`, cuando el backend local falla la app reintenta con Groq (requiere `GROQ_API_KEY`). Apagado por defecto; activar desde dashboard Settings (checkbox visible solo cuando backend=local).
- `VAD_ENABLED` (default: `true`) — Aplica Silero VAD al audio antes de enviarlo a Groq para recortar silencios (reduce costo y alucinaciones). El backend local usa su propio VAD interno; esta opción solo afecta a Groq. Apagar si hay problemas (fail-open: el audio se envía sin modificar).
- `AUDIO_SOURCE` (default: `mic`) — Fuente de captura: `"mic"` (micrófono) o `"system"` (audio del sistema vía WASAPI loopback con pyaudiowpatch, para transcribir videos/cursos que suenan en el PC). Cambiable desde el tray ("Fuente: …") o el dashboard sin reiniciar: se relee al inicio de cada grabación. En modo `system`: no hay auto-pegado (el texto va al portapapeles + notificación del tray + historial), el watchdog de micrófono se desactiva (WASAPI loopback no entrega buffers en silencio total), y `LoopbackSource` (core/recorder.py) captura a la frecuencia nativa del dispositivo de salida con downmix a mono y resample a 16kHz, por lo que el resto del pipeline no cambia. Cada transcripción guarda su fuente en la columna `source` de la DB (migración idempotente). Script de diagnóstico: `test_loopback.py` en la raíz.

### Backend Local (faster-whisper)
- Los modelos se descargan desde Hugging Face en `%APPDATA%\Vflow\models\` (bundle) o `<proyecto>/models/` (dev).
- **Limitación de traducción**: el backend local solo traduce a inglés (task="translate" nativa de Whisper). Para traducir a otros idiomas, usa el backend Groq.
- **Sin fallback a internet**: si el modelo no está descargado y se intenta transcribir, la app muestra un mensaje accionable ("ábrelo desde el dashboard") en lugar de llamar a Groq.
- **Descarga desde el dashboard**: panel Configuración → "Backend de transcripción" → "Local sin internet" → botón "Descargar modelo" con barra de progreso.

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
| Accidental recording with IDE shortcuts | Increase `ARMING_DELAY` in `config.py` (default 0.15s) to require longer hold before recording starts in Ctrl+Alt/Ctrl+Shift+Alt modes |
| Microphone disconnected mid-recording | App detects silence within ~2s, stops recording, shows error state on pill, and displays tray notification. Audio watchdog prevents hanging. |
| Pill appears on wrong monitor | Pill auto-detects current monitor based on cursor position. If dragged between monitors and one disconnects, pill repositions to nearest active monitor. Check monitor layout in Windows Display Settings. |
| "Modelo local no descargado" en la notificación | El backend está en "local" pero el modelo no se ha descargado. Abre el dashboard → Configuración → activa "Local sin internet" → pulsa "Descargar modelo". |
| El modelo local traduce a un idioma distinto del inglés | El backend local solo soporta traducción a inglés. Para otros idiomas de destino, cambia el backend a Groq en el dashboard. |
| La primera transcripción con backend local es lenta | CTranslate2 hace lazy-alloc en la primera inferencia. El warmup automático (al activar el backend) mitiga esto; si no se lanzó, espera unos segundos en la primera transcripción. |
| Backend local falla y no quiero perder el dictado | Activa `GROQ_FALLBACK=true` (dashboard → Configuración → checkbox "Permitir Groq como respaldo"). Requiere `GROQ_API_KEY`. Advertencia: el audio saldrá a internet cuando el local falle. |
| VAD recorta palabras al inicio o final | Aumenta `speech_pad_ms` en `core/vad.py` (default 400 ms) o desactiva con `VAD_ENABLED=false` en `.env`. |
