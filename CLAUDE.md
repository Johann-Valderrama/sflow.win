# CLAUDE.md — Vflow Development Instructions

## ## Contexto OPS (padre)

@C:/OPS/_CONTEXTO-OPS.md

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

### Testing

Comando canónico de la suite (desde la raíz del repo):

```bash
venv\Scripts\python.exe -m pytest
```

`pytest.ini` fija `testpaths = tests`, así que `pytest` a secas (con o sin `-q`) SIEMPRE
colecta solo `tests/*.py` — nunca los scripts sueltos de la raíz. `test_loopback.py` y
`test_dual_capture.py` son diagnósticos de **hardware real** (micrófono + loopback del
sistema); se corren a mano cuando hace falta depurar audio, nunca como parte de la suite
automatizada:

```bash
venv\Scripts\python.exe test_loopback.py
venv\Scripts\python.exe test_dual_capture.py
```

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
│   └── database.py         # SQLite CRUD (WAL + busy_timeout; modo read_only para lectores externos)
├── mcp_server/
│   ├── __main__.py         # python -m mcp_server (stdio)
│   └── server.py           # Servidor MCP local read-only: search_meetings, get_minutes, get_transcript
├── web/
│   ├── server.py           # Flask dashboard at localhost:5678 (auto-finds free port)
│   ├── templates/          # Templates Jinja2: dashboard.html (ruta /) y reunion.html (/reunion)
│   └── static/vendor/      # Assets auto-hospedados (tailwind.js Play + inter-variable.woff2) → dashboard offline, sin CDN
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

> Las transformaciones de TEXTO que corren entre la respuesta del backend y el pegado (filtro de
> alucinaciones, diccionario, smart commands, snippets, reformateo LLM) tienen un contrato propio
> con tres ejes de obligado cumplimiento: **sección 19, "Contrato del pipeline de texto"**. Antes
> de agregar o mover una pasada de texto, léelo.

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

### 10. Dashboard Shell (web/server.py)

The dashboard is a hash-routed SPA (rediseño jul 2026): a fixed left **sidebar** (icon+label nav, collapses to icons under 1100px, footer shows live system status: backend + audio source) navigates between **views** — `#/dictados` (home: usage metric cards via `GET /api/stats` + transcription table with per-source badges mic/system/youtube), `#/reunion` (panel de reunión activa — ver "Panel en vivo push→pull" abajo), `#/diccionario`, `#/url` (`id="url-queue-panel"`), `#/atajos` (`id="shortcuts-panel"`, static HTML), `#/ajustes` (settings accordion). The hash router is the single source of truth for visibility (replaces the old toggle-panel accordion; legacy `toggle*()` functions are `navigate()` wrappers); polls (URL queue, meeting) start/stop on view enter/leave. **Ctrl+K** opens a command palette (view jumps + full-history search via `GET /api/transcriptions/search`; selecting a transcription copies it). Design tokens + component classes (`.btn-*`, `.card`, `.badge-*`, `.metric-card`) live at the top of the inline `<style>` block. Contract: the selection→dictionary flow stores pending text in JS state and prefills on view mount (table and dictionary no longer coexist in the visible DOM).

**Panel en vivo push→pull (`/reunion`, jul 2026):** dos pestañas — "En vivo" (timer + VU por canal Yo/Ellos con levels RMS del status, transcript con badges, campo de nota rápida, barra de acciones ⭐ highlight / 📝 nota / ⏸ pausar-reanudar / ⏹ terminar) y "Preguntar" (chat en vivo con chips fijos "¿Puntos clave hasta ahora?" / "¿Qué me falta preguntar?" / "Pendientes y responsables"). En vivo ya NO se muestran temas/propuestas (van al acta al cerrar); el único push son tarjetas de pendientes con ✓/✗, keyeadas por hash de texto normalizado, caducidad ~3 min, máx 3 simultáneas — el feedback del usuario se persiste en `feedback_json` (tabla `meetings`) para el bucle de mejora de prompts. El panel embebido en el dashboard (fuera de `/reunion`) muestra solo pendientes + link a la vista completa. Pausa congela el reloj: `MEETING.pause()`/`resume()` — el tiempo pausado no cuenta en `elapsed` ni en la duración persistida, y los callbacks de audio no acumulan frames en pausa. Notas rápidas via `MEETING.add_note()`, persistidas en `notes_json` (tabla `meetings`). Endpoints: `POST /api/meeting/{highlight,note,pause,resume,feedback}`; `POST /api/meetings/chat` acepta flag `live` (vivo automático si hay reunión activa y no se pasa `meeting_id`). El pill flotante durante una reunión muestra anillo ámbar (matiz distinto con `AUDIO_SOURCE=system`), timer mm:ss, ⏸ parpadeante en pausa e indicador cian de actividad del canal "Ellos" (`PillWidget.set_meeting_state()`); clic corto (<6px) abre `/reunion`.

**Verificación de UI (obligatoria tras tocar templates):** visibilidad por estado computado (`getComputedStyle`, nunca solo `classList`), screenshot/snapshot de cada vista tocada + el shell completo antes de dar por buena la unidad, y overlays (paleta, modales, toasts) con trío oculto-al-cargar/abre/cierra. Lección del bug de la paleta Ctrl+K (jul 2026): una regla CSS por `#id` le gana a `.hidden` de Tailwind — estilar visibilidad por ID sin la regla `#id.hidden{display:none}` es un bug latente. Detalle: feedback OPS `verificar-ui-como-el-usuario`.

**DOS documentos HTML separados (lección unidad 3.1, jul 2026):** `web/server.py` sirve dos documentos independientes — `web/templates/dashboard.html` (el SPA del dashboard, ruta `/`) y `web/templates/reunion.html` (la página `/reunion`), extraídos del inline en la unidad 4.1. NO comparten scope de JS: una función definida en uno NO existe en el otro (un helper usado en ambos pero definido en uno solo mata la otra página con `ReferenceError` en runtime, invisible para los tests del endpoint). JS compartido se define UNA vez como constante Python (patrón `_MT_INCREMENTAL_JS`) y se inyecta como variable de contexto Jinja `{{ mt_js|safe }}` en ambos templates; el test guardián `tests/test_meeting_incremental.py::TestHelperPresentInBothDocuments` verifica que todo documento que invoque el helper también lo defina. El polling de reunión es incremental: `GET /api/meeting?since=N` devuelve solo el delta + `gen` (token de generación) para invalidar el cursor al cambiar de reunión; sin `since` la respuesta completa legada se mantiene.

### 11. Transcribir desde URL — motor unificado + cola bulk (`core/url_transcribe.py`, `web/server.py`)

Permite transcribir YouTube / TikTok / Instagram (individual o en lote) sin grabar audio en tiempo real. El audio del sistema (WASAPI loopback, `AUDIO_SOURCE=system`) se conserva como **fallback** para cuando yt-dlp se rompa (las plataformas pelean contra los extractores; `yt-dlp` pinneado se vuelve obsoleto — requiere `pip install -U yt-dlp` periódico) y para **directos en vivo** que no se pueden descargar.

- **Motor** `core/url_transcribe.py`: `detect_platform(url)` y `transcribe_url(url, *, allow_instagram=False, on_progress=None) -> dict` (claves: `ok, title, source, method, language, text, duration, error, error_kind`). Decide subtítulos-o-audio: YouTube con subtítulos → fast-path **gratis** (subtítulos vía yt-dlp, VTT→texto con dedup de cues rolling, `dictionary.apply_replacements`); cualquier otra cosa → **descarga audio-only con yt-dlp SIN ffmpeg** (`format="bestaudio/best"`, sin postprocessors) → decodifica con **PyAV** (`av`, ya incluido con faster-whisper) y `av.AudioResampler` a WAV 16k mono → chunking de ~240s con solape y carryover de prompt → `Transcriber` (filtro de alucinaciones + diccionario). **No requiere binario ffmpeg de sistema** (PyAV trae las libs embebidas), lo que mantiene la portabilidad a Mac/Linux. Instagram es experimental (`allow_instagram=True` usa `cookiesfrombrowser`; sin cookies → `error_kind="needs_auth"`).
- **Endpoint individual**: `POST /api/youtube-transcript` delega en `transcribe_url` y persiste. Mapea `error_kind` a HTTP (invalid_url→400, no_subtitles→404, needs_auth→401, network→502).
- **Cola bulk**: tabla `url_queue` (id, url, platform, status pending/processing/done/error, stage, title, error, allow_instagram, created_at; migración idempotente). Un **worker serial en background** (thread daemon, un solo item a la vez con pausa ~1.5s anti-baneo, repara items `processing` huérfanos al arrancar) procesa la cola, inserta cada éxito en `transcriptions` (`source` real, respeta `SAVE_HISTORY`). Endpoints: `POST /api/url-queue` (`{text}` multilínea o `{urls:[]}` + `allow_instagram`; devuelve `{enqueued, rejected}`), `GET /api/url-queue` (lista + summary, la UI hace polling), `POST /api/url-queue/clear`, `POST /api/url-queue/cancel-pending`.
- **UI**: panel "Transcribir desde URL" con campo único + textarea bulk + checkbox Instagram experimental + botón "Sincronizar cookies de Instagram" + lista de progreso en vivo. La cola usa el backend de transcripción actual (nota en UI: usar backend `local` para que el lote sea gratis). History rows muestran badge de fuente (▶ youtube/url, 🔊 system, none para mic).

#### Cookies de Instagram (autenticación)

Instagram requiere la sesión del usuario. yt-dlp lee las cookies del navegador con `cookiesfrombrowser` (barrido Opera→chrome→edge→brave→firefox→vivaldi; **Opera es el más compatible en Windows** — Chrome bloquea su base abierta, Edge/Brave usan App-Bound Encryption que no se descifra). **Bug crítico resuelto**: `core/secrets.py` fija `CryptUnprotectData.argtypes` en el crypt32 global del proceso (para cifrar la API key con DPAPI), lo que rompía la extracción de cookies de yt-dlp (que pasa su propia `DATA_BLOB`) con "expected LP__DATA_BLOB...". El context manager `_clean_crypt32_argtypes()` limpia esos argtypes mientras yt-dlp lee cookies y los restaura al salir. Por defecto Instagram funciona leyendo el navegador **en vivo** (sin archivo). Como **fallback duradero**, el botón del dashboard (`POST /api/instagram-cookies/sync` → `sync_instagram_cookies()`) extrae solo las cookies de instagram y las guarda **cifradas con DPAPI** en `instagram_cookies.dat` (mismo mecanismo que la API key; nunca texto plano en reposo); en la descarga, `_resolve_cookiefile()` las descifra a un temporal efímero dentro del tempdir de la descarga. `*_cookies.txt` y `*_cookies.dat` están en `.gitignore` (sesión privada).

### 12. Conversation intelligence Yo/Ellos (`core/meeting_metrics.py`, `core/vad.py`, jul 2026)

Métricas de conversación sin ML de diarización: los 2 canales físicos (mic=Yo, loopback=Ellos)
ya alcanzan. Al terminar cada ventana procesada, Silero VAD corre por canal (modelo cacheado a
nivel módulo en `core/vad.py`, función `speech_timestamps`, inferencia bajo lock propio) con
anclaje de timestamps por **muestras acumuladas por canal** (no reloj de pared: el loopback se
salta silencios y los ejes divergen). Al cerrar la reunión, `compute_metrics` (funciones puras
en `core/meeting_metrics.py`) vuelca `metrics_json` en la tabla `meetings` (migración idempotente):
`talk_yo_s/talk_ellos_s`, `pct_*`, `talk_to_listen`, `longest_monologue_*_s` (flag ≥90s),
`turns_approx` (alternancia de speaker en texto), `questions_*`, `wpm_*` (null si <1s), `duration_s`.
**Interrupciones (solape) no existe en v1**: no es computable sin un eje temporal común entre
canales (el loopback se salta silencios). Expuesto en `GET /api/meetings`, `GET /api/meetings/<id>`,
MCP `get_minutes` y `MEETING.get_last_metrics()`. UI: mini-dona + "Yo N% · Ellos N%" + T:L en la
tarjeta del historial de `/reunion`, y sección "Estadísticas" en el visor de detalle (dona grande,
talk-to-listen con benchmark 43/57, monólogos con badge "largo" ≥90s, turnos aprox., preguntas,
WPM con nota 140-160); tolera `metrics` null en reuniones viejas. `pause()` reordenado: `_paused`
se marca antes del flush para que los frames pre-pausa no se cuelen en la ventana post-pausa.

### 13. Servidor MCP local de reuniones (`mcp_server/`)

Expone la memoria de reuniones a agentes locales (Claude Code, Levy) por MCP stdio, sin abrir
el dashboard. Feature dev/local: corre con el venv del proyecto (`python -m mcp_server` desde la
raíz), autodescubierto por Claude Code vía `.mcp.json`; NO se bundlea en el .exe.

- **Tools (read-only)**: `search_meetings(query, limit)` (FTS5 `match="or"` + bm25, o recientes
  si query vacía; coincidencias del snippet entre «»), `get_minutes(meeting_id)` (acta completa
  passthrough de `minutes_json` + capítulos de `chapters_json`), `get_transcript(meeting_id,
  offset, max_chars)` (paginado por segmentos `{t, time, speaker, text}`; `next_offset` para
  continuar).
- **Read-only garantizado a nivel SQLite**: `TranscriptionDB(read_only=True)` abre TODAS sus
  conexiones con URI `mode=ro` y se salta DDL/migraciones/backfill. Si la DB no existe aún, las
  tools devuelven error accionable (no crashea).
- **Concurrencia**: la DB principal corre en `journal_mode=WAL` (activado incondicionalmente en
  `_init_db` en cada arranque) + `busy_timeout` 5s en todas las conexiones (helper `_connect()`),
  así el MCP lee mientras la app escribe sin "database is locked".
- **Limitación documentada**: el índice FTS lo mantiene el proceso escritor; el lector RO nunca
  hace backfill. stdout es del protocolo: nada de `print()` en `mcp_server/`.
- **Momentos destacados**: `AltGr+H` durante una reunión activa persiste el timestamp en
  `highlights_json` (tabla `meetings`, migración idempotente); el acta al cerrar incluye la
  sección `momentos_destacados` ({"time", "texto"}, regla callar>inventar), visible en dashboard
  (vivo/historial), export markdown, Asistente de reuniones y `get_minutes` (passthrough).
- **Columnas de `meetings` (Ola 2)**: además de `minutes_json`/`chapters_json`/`highlights_json`,
  se sumaron `notes_json` (notas rápidas del usuario en vivo) y `feedback_json` (✓/✗ del usuario
  sobre las tarjetas de pendientes, para el bucle de mejora de prompts); migración idempotente.
- **Chat "Esta reunión" en vivo** (`core/assistant.py` `build_context_live`/`answer_live`): responde
  sobre `MEETING.snapshot()` (foto atómica del estado en RAM), recorta el transcript por el
  principio con aviso si es muy largo, cita timestamps mm:ss (sin IDs) y no llama al LLM si el
  transcript está vacío.

### 14. Patrón Granola — presupuesto, notas, trazabilidad y plantillas (Ola 4, jul 2026)

- **Presupuesto de contexto del acta**: `generate_minutes`/`generate_chapters` truncan el
  transcript **por el principio** (conservan el final, más relevante) cuando excede el límite del
  backend batch activo, con aviso explícito en el acta ("[transcript truncado...: faltan los
  primeros X minutos]"). Límite por backend: `endpoint` 18KB / `claude-cli` 40KB / resto 80KB.
  Helper compartido `insights.budget_chars(task)`.
- **Fusión de notas del usuario**: las notas en vivo (📝) entran al acta bajo la clave
  `notas_usuario` (`[{time, nota, contexto}]`); `nota` es el texto LITERAL del usuario (protegido
  por post-proceso, nunca parafraseado por el LLM) y `contexto` es la interpretación de la IA. Los
  temas anotados reciben prioridad en el resumen (instrucción condicional al prompt, solo si hay
  notas). Se renderiza en dashboard, export markdown, Asistente de reuniones y MCP `get_minutes`.
- **Trazabilidad (lupa)**: `decisiones` pasó a `[{texto, t?}]` y `pendientes` gana `t` opcional; el
  LLM da timestamp `mm:ss` y Python lo snapea al segmento del transcript más cercano. En el visor
  de `/reunion`, cada bullet con `t` es un chip `mm:ss` clicable que salta al segmento y lo
  flashea. Actas viejas (bullets como string plano) siguen renderizando sin romper.
- **Plantillas por tipo de reunión** (`core/meeting_templates.py`): 4 plantillas — general, ventas,
  1:1, clase. Selector en `/reunion` junto a "Iniciar"; el atajo global AltGr+R usa la misma
  plantilla activa. Se persiste por reunión (columna `template` en `meetings`, migración
  idempotente) y moldea tres cosas: énfasis del acta (ventas añade la clave condicional `"bant"`
  solo cuando hay evidencia explícita de Budget/Authority/Need/Timeline en el transcript, nunca
  inventada), los chips del chat en vivo, y el rol/tono del Asistente de reuniones. Endpoint
  `POST /api/meeting/template`.

### 15. Proactivo v2 — gating, HUD y memoria cruzada (Ola 5, jul 2026)

Panel proactivo rediseñado sobre el principio "la barra no es esto es interesante sino esto
cambia lo que el humano hará en los próximos 2 minutos": pocas clases de alerta, timing en
lulls, caducidad. Tres unidades del mismo objetivo, mismo módulo `core/proactive.py` (puro,
sin Qt/I-O, testeable) + instancia única `PROACTIVE`.

- **5.3 — Gating + HUD flotante** (`core/proactive.py`, `ui/hud_widget.py`): `ProactiveGate`
  decide SI y CUÁNDO empujar una tarjeta (no el contenido). Tres modos vía `PROACTIVE_MODE`
  (default `copilot`): `silent` (solo pendientes), `copilot` (+ detecciones + memoria cruzada),
  `trainer` (+ coaching de monólogo, `MonologueWatch`: ~90 ticks de 1s hablando yo sin que
  "Ellos" hable activa nudge; ceder la palabra ≥5 ticks reinicia el contador). Presupuesto
  ~1 push no-pendiente cada 5 min (`_PUSH_BUDGET_SECONDS`); los pendientes SIEMPRE se muestran
  (contrato desde la unidad 2.2). Cola de lull: una tarjeta espera hasta 60s una pausa natural
  (RMS por canal, mismo umbral que `MEETING_SILENCE_RMS`) antes de forzarse; expira sin
  mostrarse a los 180s si nadie atiende. El **HUD** es una ventana Qt nativa frameless
  always-on-top que no roba foco (`WindowDoesNotAcceptFocus` salvo cuando el usuario interactúa
  deliberadamente con el mini-input; Esc devuelve el foco), desplegable con **AltGr+A** o clic
  derecho del pill; badge de 1 palabra en el pill (`ui/pill_widget.py`) avisa sin abrir el HUD.
  **AltGr+M ("me perdí")** dispara `answer_live` en un worker thread para un resumen instantáneo
  de los últimos ~2 minutos. El panel web (`web/server.py`) tiene un renderer genérico de
  tarjetas por clase con el mismo gating por modo; setting `proactive_mode` en Ajustes.
- **5.1 — Detecciones proactivas** (`core/insights.py`, `core/meeting.py`): tres clases
  (pregunta sin responder, compromiso adquirido, acuerdo vago sin fecha/dueño) detectadas como
  **segunda intención de la MISMA llamada** del insight stream — cero cuota LLM extra. Kill-switch
  por clase vía `PROACTIVE_DETECT_PREGUNTAS` / `PROACTIVE_DETECT_COMPROMISOS` /
  `PROACTIVE_DETECT_ACUERDOS` (default `true`, lectura perezosa — se apagan sin reiniciar).
  Dedup LLM+local, gating por modo/flag/presupuesto (vía `PROACTIVE`), rastro persistido en
  `detections_json` (tabla `meetings`, migración idempotente). Calibrado contra 12 ventanas de
  transcripts reales: 0 falsos positivos.
- **5.2 — Memoria cruzada en vivo** (`core/meeting.py` `_cross_memory_check()`): retrieval
  **puro, cero LLM**, enganchado a cada consolidación (~240s) del insight stream (best-effort:
  un fallo aquí jamás rompe la consolidación). Pipeline: temas del rolling state → términos de
  búsqueda → `meetings_search` (FTS, ahora proyecta `bm25 AS score`, campo aditivo solo en la
  ruta FTS) contra actas pasadas → gate por overlap real ≥2 tokens significativos con un tema →
  tarjeta tipo `"cruzada"` **"El dd/mm se acordó: …"** vía `PROACTIVE` → dedup máx. 1 tarjeta por
  reunión pasada por sesión (`_cross_emitted`). Excluye siempre la reunión activa y reuniones sin
  acta; tolera actas viejas (bullets string) y nuevas (`{texto, t}`). Kill-switch:
  `PROACTIVE_DETECT_CRUZADA` (default `true`). El estilo de la tarjeta (`CARD_STYLES`, web + HUD)
  ya existía de la unidad 5.3.

**Invariantes clave**: (a) lo único que se empuja sin que el usuario lo pida es
pendientes + detecciones 5.1 + memoria cruzada 5.2, todo con caducidad y bajo el mismo
presupuesto de `ProactiveGate`; (b) las detecciones 5.1 no cuestan una llamada LLM adicional
(viven dentro del insight stream existente); (c) la memoria cruzada 5.2 es retrieval puro —
si algún día se quisiera resumir/comparar con LLM, sería una unidad nueva, no una modificación
de esta. Fuera de v1 (backlog, no implementado): interrupciones/solape (Ola 3, no computable sin
eje temporal común entre canales), agenda con reloj, bandeja pasiva de datos duros, diff
"qué viste tú vs qué vio la IA" post-reunión, y exportar pendientes al ecosistema OPS.

### 16. Plataforma y dictado — webhook, crudo/Undo, modos por app (Ola 6, jul 2026)

Tres unidades independientes que cierran el backlog de integración externa y los patrones de
dictado de superwhisper/Wispr Flow (ver `docs/PENDIENTES.md`).

- **6.1 — Webhook saliente firmado** (`core/webhook.py`): al cerrar una reunión PERSISTIDA con
  acta, POST JSON (patrón Fireflies) a una URL configurable con firma HMAC-SHA256 del body
  (`X-Vflow-Signature: sha256=...`, `X-Vflow-Event: meeting.minutes`). Opt-in, apagado por
  default. Payload minimizado por `WEBHOOK_SCOPE`: `pendientes` (metadatos + pendientes) o
  `acta` (+ `minutes_json` + capítulos); el transcript crudo NUNCA se envía. Anti-SSRF: resuelve
  DNS y rechaza loopback/privadas/link-local/`::1` salvo `WEBHOOK_ALLOW_LOCAL=true`; exige https
  por default; re-valida en cada reintento (mitiga rebinding básico, sin pinning de IP al socket,
  limitación documentada). Envío fire-and-forget en hilo daemon (timeout 10s + 3 reintentos con
  backoff exponencial); un fallo JAMÁS propaga a `stop()`. Con `meeting_id` `None` (historial
  apagado o insert fallido) no se envía nada. **Dead-drop local** (`PENDING_EXPORT_DIR`): desde la
  unidad 5.4 (jul 2026) escribe el **contrato de tarea v1** (`docs/CONTRATO-MACROSISTEMA.md` Parte
  A): YAML `schema_version: 1` + vista humana, SOLO si la reunión tiene ≥1 pendiente (gate O7),
  naming create-only `vflow-pendientes-<instalacion>-<meeting_id>-<hash8>.md` (`instalacion` =
  machine_id 8-hex persistido en el data dir; acta cambiada → hash distinto → archivo nuevo, el
  viejo nunca se toca). Buzón sugerido para el consumidor OPS: `C:\OPS\_inbox-vflow\`. El secreto
  `WEBHOOK_SECRET` se cifra con DPAPI (mismo mecanismo que `GROQ_API_KEY`), write-only (el GET de
  settings solo expone `has_webhook_secret`).
- **6.2 — Crudo/Undo + diccionario sugerido**: columna `raw_text` en `transcriptions` (`NULL`
  si coincide con el texto final; migración idempotente). `Transcriber.transcribe`/`translate`
  ganan kwarg `return_raw=False` (con `True` devuelven `(text, raw)`; la firma `str` por defecto
  no cambia, así que `main.py`/`core/meeting.py`/`core/url_transcribe.py` quedan intactos). Solo
  el flujo de dictado captura crudo (`url_transcribe` y traducción quedan sin `raw_text` en v1:
  el diccionario aplica al idioma dictado). UI del historial: toggle "Ver crudo" + botón
  "Deshacer edición IA" (reusa el `PUT` existente), visibles solo en filas con `raw_text`.
  **Diccionario sugerido**: `suggest_dictionary_pairs` (`core/dictionary.py`, difflib stdlib)
  detecta sustituciones 1-a-1/2-a-2 cuando el usuario edita una transcripción en el dashboard
  (máx. 3 bloques de diff; reescrituras amplias no sugieren nada). Las sugerencias nacen
  `source='suggested'`, `enabled=0` — nunca se auto-aplican. Bandeja "Sugeridas" en el panel
  Diccionario con Aceptar (→ `manual`, `enabled=1`) / Descartar.
- **6.3, modos de dictado por app activa** (`core/dictation_modes.py`): reformateo post-dictado
  por preset, sin builder de modos custom. `DICTATION_MODES_ENABLED` (default `false`) +
  `DICTATION_MODE_MAP` (mapa configurable `exe:preset`). Captura del `.exe` en foco vía ctypes
  puro en `save_frontmost_app()` (`core/clipboard.py`: `GetWindowThreadProcessId` +
  `OpenProcess` + `QueryFullProcessImageNameW`, best-effort). Si el backend batch resuelto es
  `claude-cli` el reformateo se **salta** (latencia de arranque 5-15s inaceptable en el hot-path
  del dictado): el fallback automático de insights (groq/openrouter) sigue disponible si
  `INSIGHTS_FALLBACK` está activo. Se dispara en el hilo background existente
  (`_transcribe_final`), nunca en el hot-path síncrono. Excluido en modo traducción y
  `AUDIO_SOURCE=system`. El `raw_text` conserva el texto MÁS crudo cuando diccionario y
  reformateo cambian ambos (el Undo de 6.2 revierte todo). **Ampliado en la Ola 2 de
  `docs/PLAN-DICTADO-2026-07-31.md` a 5 presets, elección manual y panel descubrible en
  Ajustes: ver la sección 21, que es la que manda hoy sobre cuántos presets hay y cómo se
  eligen.**

**Fuera de v1 (decidido, no pendiente)**: raw_text en `url_transcribe`/traducción (el diccionario
ya cubre esos flujos); builder visual de modos de dictado custom (un conjunto FIJO y CURADO de
presets evita el error de settings infinitos de superwhisper, la invariante nunca fue "exactamente
3", ver sección 21); pinning de IP al socket del webhook (rebinding avanzado, riesgo residual
documentado y aceptado).

### 17. Copiloto con contexto OPS — briefing v1 (Ola 7, jul 2026)

Permite que el chat en vivo "Preguntar" (`core/assistant.py` `answer_live`/`build_context_live`)
conecte lo hablado en la reunión con el contexto compartible del usuario (proyectos activos,
compromisos, metas), curado en un `.md` externo. **Opt-in, apagado por default.**

- **Setting `OPS_BRIEFING_PATH`** (default `""`): ruta a un `.md` curado por el usuario. Se
  gestiona desde el dashboard (Ajustes → "Copiloto con contexto OPS") y desde `.env` (flag de
  lectura perezosa, documentado en `config.py`). Vacío = feature apagada, sin efecto ni logs.
- **Módulo `core/ops_briefing.py`** (`get_briefing() -> str`, `invalidate() -> None`): caché en
  memoria bajo lock con TTL de **60s** por `time.monotonic()` (nunca mtime: resolución NTFS ~1s +
  TOCTOU + carreras); la I/O de disco ocurre **fuera** del lock (el archivo puede vivir en una
  carpeta de red). Guard de tamaño de **8192 bytes** sobre el contenido REALMENTE leído (no
  `st_size`); decodifica `utf-8-sig` (limpia BOM de Notepad). **Fail-open total**: ausente,
  directorio, symlink roto, permiso, no-UTF8 o >8KB → `""` sin propagar excepción. **Privacidad
  dura**: el módulo nunca loguea el CONTENIDO del briefing, solo el path y metadatos (tamaño).
  El dashboard llama `invalidate()` al guardar un cambio de `ops_briefing_path` para que se vea
  sin esperar el TTL.
- **Inyección SOLO en el chat pull** (`build_context_live`): el bloque del briefing entra al
  `prefix` fijo (junto a `ins_block`) con delimitadores explícitos (`<<<BRIEFING ... >>>`) y
  **válvula de sacrificio**: si el presupuesto es tan chico que incluirlo se comería el transcript
  reciente, se descarta (prioridad `transcript > briefing > análisis en vivo`; `avail` nunca queda
  negativo por su culpa). **Gate de privacidad**: no se inyecta si `proactive.get_mode() ==
  "silent"` (pantalla compartida), aunque el archivo exista y sea válido. Cuando el briefing SÍ
  se incluyó (`meta["briefing_included"]`), `answer_live` añade una línea condicional al system
  prompt indicando conectar sin inventar hechos; si no se incluyó (apagado/silent/sacrificado),
  la línea no se añade. La rama de transcript vacío (`meta["empty"]`) sigue respondiendo sin
  llamar al LLM, sin que el briefing la fuerce.
- **Backlog v1.1 (fuera de alcance, no implementado)**: inyectar el briefing en el Insight Stream
  (`core/insights.py`, `update_state`) y tarjetas proactivas "🧭 Contexto" (memoria cruzada con el
  briefing). v1 es deliberadamente solo-pull: decisión de un debate adversarial ya cerrado.

### 18. Producto — Ola 5 del PLAN-MEJORAS (jul 2026)

Cinco unidades opt-in elegidas por decisión D4 + pedido explícito (5.5), todas con debate
adversarial previo (reconciliación en `PROGRESS.md`, sección Decisiones PLAN-MEJORAS):

- **5.2 — Contexto personal en prompts** (`USER_NAME`/`USER_ROLE`/`USER_DOMAIN`): helper único
  `insights.user_identity_line()` con coletilla anti-atribución; en vivo respeta el gate `silent`
  del modo proactivo (mismo criterio que el briefing OPS); acta y Asistente la llevan siempre.
- **5.4 — Pendientes → OPS**: el dead-drop implementa el contrato de tarea v1
  (`docs/CONTRATO-MACROSISTEMA.md` Parte A, gate G1 aprobado) — ver "Dead-drop local" en la
  sección 16 y `PENDING_EXPORT_DIR` en Environment Variables.
- **5.1 — Auto-highlights** (`AUTO_HIGHLIGHTS_ENABLED`): candidatos del insight stream
  (`momentos_out` en `update_state`, `max_tokens` live 1200→1800 con test guardián de
  no-truncado) → `self._auto_highlights` → `highlights_json` con `source:"auto"`. Invariante
  dura: `generate_minutes(highlights=)` recibe SOLO manuales (gate F12 intacto, test pineado).
  Dedup: <2s vs manual (el manual manda), <5s entre autos. Calibrado contra 4 reuniones reales
  (15 ventanas, 3 candidatos grounded, 0 spam) → default `true`. Visor de `/reunion`: sección
  "⭐ Momentos" con badge `auto`; retrocompat con entradas viejas sin `source` (= manual).
- **5.3 — Chat cross-reunión + entregables**: `assistant.answer_multi` (ids explícitos > FTS,
  cap duro 12 actas por recencia ANTES de cargar, contexto = actas nunca transcripts, presupuesto
  `insights.budget_chars`, exclusiones declaradas, regla de citar fecha+reunión). Endpoints
  `POST /api/meetings/chat-multi` y `POST /api/meetings/deliverable-export`. 3 plantillas
  (email_seguimiento / informe / resumen_acuerdos). UI en `/reunion`: selección por checkbox +
  "por tema" con overlay (prefijo JS `mm`).
- **5.5 — Fallback simétrico online↔local** (`TRANSCRIPTION_FALLBACK`): ver Environment
  Variables. Scope SOLO dictado vía kwarg `net_fallback=False` en `Transcriber.transcribe/
  translate` (patrón `return_raw`; reunión y URL intactas). `_is_network_error` clasifica
  red-vs-API; breaker + warmup fire-and-forget al abrirse; notificación de tray vía callback
  plano → señal Qt (core/ sin Qt).

Fuera de este run (pregunta opt-in a Johann en PROGRESS.md): 5.6 notas híbridas estilo Granola
y 5.7 panel de privacidad verificable.

### 19. Contrato del pipeline de texto (Ola 0 de PLAN-DICTADO, 2026-07-31)

Entre "el backend devuelve texto" y "el texto se pega" corren varias pasadas que EDITAN ese texto.
Hasta ahora eran dos y su interacción nunca se escribió; el plan de dictado agrega dos más y vuelve
descubrible una quinta. Este contrato tiene **tres ejes** y ninguno es opcional: **orden**, **alcance**
y **presupuesto de latencia**. Escribir solo el orden fue lo que dejó pasar un defecto grave
(ver "Por qué el eje de ALCANCE existe" abajo).

Fuente de la decisión: `docs/PLAN-DICTADO-2026-07-31.md`, Ola 0, unidad `0a`. Test que lo hace
ejecutable: `tests/test_pipeline_texto.py` (unidad `0b`).

#### Eje 1: ORDEN canónico de las pasadas

| # | Pasada | Dónde vive | Naturaleza |
|---|---|---|---|
| 1 | Filtro de alucinaciones | `core/transcriber.py:316` (`_is_hallucination`) | local, por chunk |
| 2 | Diccionario personal | `core/transcriber.py:318` (`dictionary.apply_replacements`) | local, por chunk |
| · | *(frontera)* **ensamblado de chunks** | `main.py:882` | de aquí en adelante el texto es UNO |
| 3 | Smart commands (voz → puntuación) | `main.py`, antes del bloque de `dictation_modes` | local, sobre texto completo |
| 4 | Snippets (disparador → texto guardado) | `main.py`, justo DESPUÉS de smart commands | local, sobre texto completo |
| 5 | Reformateo LLM por preset (opt-in) | `main.py:909` (`dictation_modes.reformat_text`) | remoto, con timeout |

Reglas duras del orden:

- **1 antes que 2** (invariante preexistente): el filtro decide sobre el texto tal cual lo devolvió
  el backend; si el diccionario corriera primero podría convertir una alucinación conocida en una
  cadena que el filtro ya no reconoce.
- **Las pasadas 3, 4 y 5 corren DESPUÉS del ensamblado**, nunca por chunk. Motivo medido: con
  `CHUNK_SECONDS=60` una frase-comando dicha en el borde del minuto queda partida entre dos chunks
  y no coincidiría con ninguna regla. Sobre el texto ensamblado el problema no existe.
- **3 antes que 4.** El texto que expande un snippet es texto que el usuario ESCRIBIÓ y ya viene
  puntuado; si los snippets corrieran primero, la pasada 3 volvería a escanear ese texto guardado y
  mutilaría cualquier palabra literal que contenga (por ejemplo un snippet que diga "signo coma").
  Con este orden, lo que inserta un snippet no lo vuelve a tocar nadie.
- **El reformateo LLM (5) RESPETA los saltos de línea explícitos** que vengan de la pasada 3. Un
  `\n` que el usuario pidió con la voz no es un accidente de formato que el preset pueda re-fluir.
  Esto se implementa en los PROMPTS de los presets, así que es obligación de la Ola 2 al tocarlos y
  aplica también a los tres presets que ya existen (`email`, `chat`, `codigo`).
- **Ninguna pasada nueva se cuela entre 1 y 2**, ni dentro de `Transcriber`. Ver eje 2.

**Qué guarda `raw_text` (decisión, no re-litigar).** `raw_text` es **el texto tal como lo devolvió el
backend de transcripción, antes de TODA pasada local**, y sigue siendo `NULL` cuando ninguna pasada
lo cambió. Las pasadas 3 y 4 se suman al patrón que ya usa el reformateo en `main.py:918`: si
cambian el texto y `raw_full` todavía es `None`, guardan ahí el texto pre-cambio. **Tradeoff
aceptado, escrito para que nadie lo descubra con sorpresa:** eso significa que "Deshacer edición IA"
también revierte la puntuación que el usuario pidió por voz, y esa etiqueta se queda corta. Se
prefiere que `raw_text` tenga UN solo significado ("lo que dijo el backend") antes que preservar la
precisión de un rótulo de botón; la alternativa obligaba a aplicar smart commands también sobre
`raw_full` cada vez que el diccionario ya había cambiado algo, que es más código y más formas de
fallar. Lo que revisaría esta decisión: que aparezcan falsos positivos reales de smart commands en
uso diario, porque entonces el Undo sí sería la vía de escape natural.

#### Eje 2: ALCANCE (qué llamadores ejecutan cada pasada)

`Transcriber.transcribe`/`translate` tienen **tres consumidores vivos**, medidos 2026-07-31:

| Flujo | Call site |
|---|---|
| Dictado | `main.py:777` (worker de chunk), `main.py:854` (tramo final), `main.py:846` (traducción) |
| Reunión | `core/meeting.py:961` |
| URL (YouTube/TikTok/Instagram) | `core/url_transcribe.py:549` y `:553` (reintento) |

| Pasada | Dictado | Traducción | Reunión | URL |
|---|---|---|---|---|
| 1. Filtro de alucinaciones | sí | sí | sí | sí |
| 2. Diccionario personal | sí | sí | sí | sí |
| 3. Smart commands | **sí** | **no** | **no** | **no** |
| 4. Snippets | **sí** | **no** | **no** | **no** |
| 5. Reformateo LLM | sí (opt-in) | no | no | no |

**La regla durable, que es lo que hay que recordar cuando nazca un cuarto flujo y esta tabla
envejezca:** una pasada que edita el texto según lo que el hablante QUISO ESCRIBIR solo puede correr
donde el hablante es el usuario y el destino es la ventana en foco. Reunión y URL contienen habla de
OTRAS personas, que nadie autorizó a reinterpretar.

Consecuencia mecánica y no negociable: **las pasadas 3 y 4 se cablean en `main.py`, bajo los mismos
gates que ya usa `dictation_modes` (`not translate` y `recorder.source != "system"`), y NUNCA en
`core/transcriber.py`.** Ahí las heredarían los otros dos flujos por construcción.

**Por qué el eje de ALCANCE existe** (la primera redacción de esta ola solo tenía el orden, y por eso
casi entra el defecto): cablear smart commands en `core/transcriber.py` habría (a) metido puntuación
inventada en el habla de terceros dentro de las actas, (b) roto el contrato de una-línea-por-turno
del buffer de insights (`core/meeting.py:976`), (c) subestimado el WPM de las métricas de conversación,
porque el numerador encoge mientras el denominador sale del VAD del audio original, y (d) roto el
significado de `raw_text`. Tres de esos cuatro daños **no dependen de que ningún disparador coincida**:
ocurren igual con un usuario que nunca diga "signo coma".

#### Eje 3: PRESUPUESTO de latencia del hot-path

El hot-path es **soltar el atajo → texto pegado en la ventana**. Aquí ya hay historia pagada: el
"timeout duro de 8 s" del reformateo fue ilusorio durante un tiempo porque `ThreadPoolExecutor`
bloqueaba en su `__exit__`, y con la red colgada el pegado tardaba decenas de segundos
(`AUDITORIA-FABLE-2026-07-06.md`, hallazgo F11; **ya corregido** con un hilo daemon propio, ver el
docstring de `core/dictation_modes.py:106`). Sin un número escrito, nadie gobierna esto.

Topes vigentes, medidos en el código 2026-07-31 (cada fila con su archivo al lado; si se re-lee este
contrato dentro de meses, se re-comprueban, no se copian):

| Tramo | Tope | Dónde está el número |
|---|---|---|
| Transcripción del tramo final (Groq) | 10 s | `core/backends/groq_backend.py:183` |
| Gracia de join de chunks en vuelo | 12 s | `main.py:165` (`_CHUNK_JOIN_GRACE_SECONDS`); solo dictados largos con un chunk lento |
| Reformateo LLM por preset (opt-in) | 8 s | `core/dictation_modes.py:106` |
| **Smart commands (Ola 1)** | **5 ms** | presupuesto NUEVO, lo afirma `tests/test_pipeline_texto.py` |
| **Snippets (Ola 4)** | **15 ms** | presupuesto NUEVO, lo afirma `tests/test_pipeline_texto.py` |

- **Techo de las pasadas locales nuevas: 50 ms** para 3+4 juntas, en el peor caso de un dictado
  largo (~5.000 caracteres). Con el peor caso actual del hot-path en ~30 s (10 + 12 + 8), esto es
  menos del 0,2%: el presupuesto no está para optimizar, está para que una pasada local no se
  convierta nunca en una llamada cara sin que nadie lo note.
- **Ninguna pasada local del hot-path llama a la red ni a un LLM.** Si algún día hiciera falta, va
  detrás de un flag opt-in apagado por defecto y con timeout duro, como `dictation_modes`, no
  añadida al camino que corre siempre.
- **Los snippets leen su tabla desde caché en memoria** (patrón de `core/dictionary.py`: caché con
  swap atómico e invalidación perezosa), nunca una consulta SQLite por dictado dentro del hot-path.

### 20. Smart commands: voz a puntuación (Ola 1 de PLAN-DICTADO, 2026-07-31)

Reglas locales que convierten lo que dictas en signos. **Sin LLM, sin red, sin disco, sin
interfaz.** Es la pasada 3 del contrato de la sección 19: corre en `main.py` sobre el texto ya
ensamblado, solo en el dictado, y nunca en `core/transcriber.py`.

> **CAMBIO DE COMPORTAMIENTO para quien ya dictaba, y por eso se anuncia aquí en vez de colarse.**
> Desde esta versión, decir `"signo coma"` deja de escribir esas dos palabras y pone una `,`. Si
> alguien dicta esas frases de forma literal a menudo, se apaga con `SMART_COMMANDS_ENABLED=false`,
> que se relee en caliente y no exige reiniciar. Va con default ON porque la pasada no manda nada a
> ninguna parte y el error se ve dictando, en el acto.

**Los disparadores llevan PREFIJO obligatorio** (`signo` o `signos` en español, `symbol` en inglés).
Se dice `"signo coma"`, nunca `"coma"` pelada. Esto es una mejora deliberada sobre el upstream
`daniel-carreon/sflow`, no una copia: sus reglas disparan con la palabra suelta y atrapan habla
normal en español, así que `"hay dos puntos importantes"` se convertía en `"hay: importantes"` y
`"entró en coma profundo"` en `"entró en, profundo"`. El prefijo casi elimina esos falsos positivos
y, de regalo, deja dictar esas palabras literalmente, cosa que el diseño del upstream no permite.

Se descartó `puntuación` como prefijo alternativo por la misma lógica: es una palabra con
significado genérico real (*"revisemos la puntuación coma por coma"*), así que reintroducía el
problema que el prefijo existe para matar. Quitarlo no cuesta ninguna capacidad, porque todo lo que
se diría con `puntuación X` se dice con `signo X`. Queda como test negativo en
`tests/test_smart_commands.py`.

| Se dicta (tras el prefijo) | Produce |
|---|---|
| `punto` | `. ` |
| `coma` | `, ` |
| `dos puntos` | `: ` |
| `punto y coma` | `; ` |
| `puntos suspensivos` | `… ` |
| `punto y aparte` | `.` + párrafo nuevo |
| `nuevo párrafo` / `nuevo parrafo` | párrafo nuevo |
| `nueva línea` / `salto de línea` (y sus variantes sin tilde) | salto de línea |

En inglés, tras `symbol`: `period` / `full stop`, `comma`, `colon`, `semicolon`, `ellipsis`,
`new line`, `new paragraph`.

Detalles que importan al tocar esto:

- **Un solo regex combinado**, no una pasada por regla. Con 20 `re.sub` secuenciales el presupuesto
  de 5 ms del Eje 3 quedaba al límite (~4,8 ms medianos con picos por encima) sobre 5.000
  caracteres; con una sola alternancia el texto se recorre una vez y baja a ~0,5 ms.
- **El orden de la tabla es por cantidad de palabras descendente**, porque `re` con alternancia toma
  la primera que coincide y no la más larga: sin eso, `punto` se comería `punto y aparte` y dejaría
  suelto un `" y aparte"`.
- **`raw_text`** conserva el texto pre-comandos si no había crudo previo (contrato de la sección 19),
  así que el "Deshacer edición IA" del historial revierte también la puntuación por voz.
- **Fuera de v1, decidido y no omitido en silencio:** interrogación y exclamación (en español
  necesitan apertura y cierre, y decidir dónde va la apertura es un diseño propio) y capitalizar la
  letra siguiente tras un punto (Whisper ya capitaliza; una segunda pasada adivinando hace más daño
  que bien). Las dos razones están en el docstring de `core/smart_commands.py`.
- **Límite conocido y aceptado:** el prefijo en español y el comando en inglés se pueden mezclar
  (`"signo comma"` dispara). Es inofensivo y bloquearlo no aporta nada.

### 21. Presets a la carta: 5 presets, elección manual y panel descubrible (Ola 2 de PLAN-DICTADO, 2026-08-01)

Completa el motor de reformateo de la unidad 6.3 (sección 16), que existía desde hace meses y
nunca se veía: apagado por defecto y sin interfaz que lo explicara. Tres unidades del mismo
plan (`docs/PLAN-DICTADO-2026-07-31.md`, Ola 2): `2a` agrega dos presets, `2b` agrega la
elección manual, `2c` lo hace descubrible sin tocar el default. **`DICTATION_MODES_ENABLED`
sigue en `false`**: nada de esto cambia a dónde va el dictado de nadie, solo lo vuelve visible
y explicado antes de que alguien lo encienda.

**Los 5 presets** (`core/dictation_modes.PRESETS`), cada uno justificado en una línea, que es el
filtro real de admisión ("¿puedo escribir en UNA línea cuándo se usa este y no el de al lado?"):

| Preset | Cuándo se usa |
|---|---|
| `email` | prosa formal de correo, y respeta el saludo o el cierre si los dictaste |
| `chat` | mensaje casual, se permite minúscula inicial y omitir el punto final |
| `codigo` | deja los términos técnicos e identificadores literales, sin embellecer |
| `lista` | convierte lo dictado en viñetas, una por ítem, sin prosa alrededor |
| `notas` | prosa limpia y neutra: arregla la puntuación y nada más, sin formalidad de correo ni relajación de chat |

Sigue siendo un conjunto FIJO y CURADO, sin builder de modos custom (sección 16): la invariante
nunca fue "exactamente 3", fue "conjunto fijo, sin que el usuario invente los suyos", y esa
propiedad se conserva intacta con 5.

- **`2a`, elección automática por app en foco** (ya existía en 6.3, sin cambios de mecanismo):
  `DICTATION_MODE_MAP` (`exe:preset,exe:preset,...`) mapea el `.exe` capturado en
  `save_frontmost_app()` a un preset, vía `dictation_modes.preset_for_exe()`.
- **`2b`, elección MANUAL para el siguiente dictado** (`core/dictation_modes.py`:
  `set_manual_preset` / `get_manual_preset` / `consume_manual_preset` / `resolve_preset`):
  submenú "Próximo dictado" en la bandeja (clic derecho en el icono de Vflow), con
  `Automático` + los 5 presets. **Gana sobre el mapeo automático** cuando hay uno armado
  (`resolve_preset` implementa la precedencia explícita). Uso ÚNICO, no pegajoso: se consume al
  terminar ese dictado y vuelve solo a `Automático`, para evitar el mismo riesgo de "settings
  infinitos" que ya descartó el builder custom. Estado en memoria del proceso, nunca en DB ni
  archivo: se pierde al reiniciar Vflow, a propósito. El submenú queda visible aunque el flag
  global esté apagado (elegir un preset solo arma un estado, no manda nada por sí solo), pero
  avisa ahí mismo que el reformateo está apagado.
- **`2c`, panel descubrible en el dashboard** (`web/blueprints/settings.py`,
  `web/templates/dashboard.html`, sección Ajustes → "Modos de dictado por app"): antes de mostrar
  el interruptor, el panel explica los 5 presets con su línea de la tabla de arriba, avisa **sin
  eufemismos que activar esto manda el texto dictado a un modelo de lenguaje (LLM)** y que por
  defecto ese modelo corre en la nube (mismo backend que "Acta + Asistente de reuniones", arriba
  en el mismo panel; nota dinámica en el propio panel con cuál backend concreto aplica hoy), y
  renderiza la lista de apps que se reformatearían automáticamente según el mapa (parseado del
  mismo `DICTATION_MODE_MAP`, editable ahí mismo) **antes** del interruptor, no después de que el
  usuario ya dictó algo. `GET`/`POST /api/settings` (`dictation_modes_enabled`,
  `dictation_mode_map`) no cambiaron de contrato: ya los servía 6.3, `2c` solo cambió cómo se ven.

**Por qué el default se queda apagado (objeción A1 del debate adversarial del plan, la más
grave que encontró):** `DEFAULT_MODE_MAP` ya trae `outlook.exe`, `slack.exe`, `teams.exe`,
`whatsapp.exe` y `discord.exe` mapeados, y el backend batch por defecto es Groq, en la nube.
Poner el flag en ON habría mandado a la nube, sin pedirlo, todo lo que alguien dictara en esas
apps desde la primera actualización. Descubrible y activado por defecto NO son lo mismo: la
visibilidad se resuelve con interfaz, nunca con el default, mismo patrón que ya sigue el
proyecto con `WEBHOOK_ENABLED` y `OPS_BRIEFING_PATH`.

## Security & Privacy

### 1. API Key Encryption (DPAPI)

The GROQ_API_KEY is encrypted with Windows Data Protection API (DPAPI) via crypt32.dll and stored as base64 in `%APPDATA%\Vflow\GROQ_API_KEY_ENC`. Only the same Windows user on the same machine can decrypt it. Plaintext keys are automatically migrated to encrypted format on startup. This replaces the ineffective `os.chmod()` approach (which doesn't work on Windows).

### 2. History Privacy Mode

Configure history saving with `SAVE_HISTORY` in `.env` (default: `true`). Set to `false` to disable database recording — transcriptions paste normally but are never stored. Togglable from the dashboard Settings panel without restarting.

### 3. Automatic History Retention

Set `HISTORY_RETENTION_DAYS` in `.env` (default: `0` = keep forever). If > 0, the app automatically deletes transcriptions older than N days on startup. Configurable from the dashboard without code changes.

### 4. CSRF Hardening

The dashboard validates exact Origin/Referer hosts (`localhost`, `127.0.0.1`, or `::1` only) instead of prefix matching. This closes bypasses like `localhost.evil.com`.

### 5. Token local de sesión del dashboard

`_csrf_check` exime `GET`/`HEAD`/`OPTIONS` por diseño, así que cualquier proceso local podía leer `/api/transcriptions`, `/api/meetings` y `/api/meetings/<id>` con un `curl`, y aquí eso son transcripts y actas de reuniones con otras personas. `core/localauth.py` genera un token (`secrets.token_hex(32)`) persistido en `dashboard_token.txt` dentro de `APP_DATA_DIR` (gitignored), cacheado a nivel módulo bajo lock con la I/O fuera del lock; el archivo es la fuente de verdad (creación con `O_CREAT|O_EXCL`). El guard `_auth_check` (`web/state.py`, registrado como segundo `before_request` en `create_app()`, después del CSRF) exime `/static/*`; en las páginas HTML (`/`, `/reunion`) canjea `?t=<token>` por una cookie `vflow_token` (HttpOnly, SameSite=Strict) y **redirige a la misma ruta sin query string** para que el token no quede en la barra ni en el historial; el resto (`/api/*`, `/logo`) exige esa cookie o la cabecera `X-Vflow-Token`. Sin credencial: 401 (HTML mínimo en páginas, JSON en API). `main.py` añade `?t=` a las tres aperturas de navegador (bandeja: dashboard y reunión; clic en el pill).

**Fail-CLOSED, al revés que `core/ops_briefing.py`**: si el token no se puede crear ni leer, `verify()` devuelve `False` y se deniega; abrirse ante un error de I/O sería exactamente el bug que cierra.

**Límite conocido y aceptado (no es un pendiente)**: un proceso que corre como el mismo usuario de Windows puede leer el archivo del token o abrir la SQLite directamente. Esto sube el listón de "curl trivial" a "hay que leer un archivo del directorio de datos"; no sustituye al cifrado en reposo. El servidor MCP (`mcp_server/`) no se ve afectado: lee la SQLite directamente en modo `mode=ro`, nunca por HTTP.

## Customization

### App Version

Edit `config.py`:

- `APP_VERSION` (default: "1.0.0") — Version string displayed in system tray menu and tooltip

### Hotkeys

Edit `core/hotkey.py`:

- **Mode 1 (Ctrl+Alt hold)**: Press and hold Ctrl+Alt to transcribe; release to stop. Transcribed text auto-pastes.
- **Mode 2 (Triple-tap Shift)**: Press Shift three times within 400ms to start hands-free transcription; press Shift once to stop. Only *clean* taps count (press and release Shift with no other key in between): Shift used as part of a chord (Shift+A for a capital, Shift+Enter) neither starts nor stops recording, and any non-Shift key resets an in-progress tap sequence. The tap decision happens on Shift *release*.
- **Mode 3 (Ctrl+Shift+Alt hold)**: Press and hold Ctrl+Shift+Alt (Shift before Alt) to translate from any language to target language.
- **AltGr+H (highlight)**: during an active meeting (AltGr+R), press AltGr+H to mark the current timestamp as a highlight (auto-repeat suppressed); feedback = beep + tray notification "✓ Momento destacado (mm:ss)".
- **Mode 4 (AltGr+T toggle)**: Press AltGr+T once to start translation hands-free; press again to stop.
- **AltGr+A (HUD proactivo, Ola 5)**: toggles the floating proactive HUD open/closed (same action as a right-click on the pill). Auto-repeat suppressed.
- **AltGr+M ("me perdí", Ola 5)**: opens the HUD if closed and triggers an instant summary of the last ~2 minutes via `answer_live` (worker thread, non-blocking). Auto-repeat suppressed.
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

> Fuente de verdad programática: `config.ENV_CATALOG` (catálogo único, con default/kind/killswitch/doc por variable), verificada por `tests/test_env_catalog.py`. Esta lista es prosa para lectura humana; ante duda o drift, `ENV_CATALOG` manda.

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
- `ANTHROPIC_API_KEY` — Key de la API oficial de Anthropic (automáticamente cifrada con DPAPI, igual que `GROQ_API_KEY`). Habilita el backend de insights `"anthropic"`.
- `ANTHROPIC_MODEL_LIVE` (default: `claude-haiku-4-5`) — Modelo Anthropic para insights en vivo (Insight Stream) cuando el backend activo es `anthropic`.
- `ANTHROPIC_MODEL_BATCH` (default: `claude-sonnet-5`) — Modelo Anthropic para tareas batch (acta, consolidación, Asistente de reuniones) cuando el backend activo es `anthropic`.
- `CLAUDE_CLI_PATH` — Ruta explícita al binario de Claude Code CLI (`claude`/`claude.cmd`) si no se resuelve solo (orden: esta variable → `PATH` → `%APPDATA%\npm\claude.cmd`). Solo aplica al backend `claude-cli`.
- `CLAUDE_CLI_MODEL_LIVE` (default: `haiku`) — Alias de modelo para el análisis en vivo (Insight Stream) cuando el backend activo es `claude-cli`.
- `CLAUDE_CLI_MODEL_BATCH` (default: `sonnet`) — Alias de modelo para tareas batch (acta/consolidación/chat) cuando el backend activo es `claude-cli`.
- `WEBHOOK_ENABLED` (default: `false`) — Activa el envío del webhook saliente al cerrar una reunión persistida con acta (Ola 6).
- `WEBHOOK_URL` — URL destino del POST JSON firmado. Debe ser https salvo `WEBHOOK_ALLOW_LOCAL=true`.
- `WEBHOOK_SECRET` — Secreto para la firma HMAC-SHA256 (automáticamente cifrado con DPAPI, igual que `GROQ_API_KEY`); write-only, nunca se expone en `GET` settings.
- `WEBHOOK_SCOPE` (default: `pendientes`) — Alcance del payload: `pendientes` (metadatos + pendientes) o `acta` (+ `minutes_json` + capítulos + notas literales del usuario). El transcript crudo nunca se envía.
- `WEBHOOK_ALLOW_LOCAL` (default: `false`) — Si `true`, permite URLs loopback/privadas/link-local como destino (desactiva la protección anti-SSRF; solo para pruebas locales).
- `PENDING_EXPORT_DIR` — Carpeta del dead-drop local de pendientes (contrato de tarea v1, unidad 5.4: YAML + vista humana, `vflow-pendientes-<instalacion>-<meeting_id>-<hash8>.md`, create-only, solo con ≥1 pendiente); con o sin webhook activado. Buzón sugerido: `C:\OPS\_inbox-vflow\`. Los entregables del chat multi-reunión (unidad 5.3) van a la subcarpeta `entregables/` con prefijo `vflow-entregable-` (nunca `vflow-pendientes-*`).
- `USER_NAME` / `USER_ROLE` / `USER_DOMAIN` (default `""`, unidad 5.2) — Identidad opcional del usuario, inyectada como 1 línea (con coletilla anti-atribución) en insights en vivo (solo si el modo proactivo NO es `silent`), acta y Asistente; "Yo" pasa a ser el nombre real en pendientes. Vacío = apagado. Nota: el nombre entra en actas persistidas (visibles por clientes MCP).
- `AUTO_HIGHLIGHTS_ENABLED` (default `true`, unidad 5.1) — Candidatos automáticos a momento destacado como intención adicional del MISMO update_state (cero LLM extra, máx 2/ventana); se persisten en `highlights_json` con `source:"auto"` y JAMÁS entran al acta ni al gate anti-alucinación de `momentos_destacados` (solo los manuales AltGr+H alimentan el acta). Kill-switch en caliente.
- `TRANSCRIPTION_FALLBACK` (default `false`, unidad 5.5) — Espejo de `GROQ_FALLBACK`: con backend primario groq, un fallo de RED en el DICTADO (nunca reunión/URL) cae al modelo local si ya está descargado (sin auto-descarga; aviso de tray si falta). Breaker con cooldown `TRANSCRIPTION_FALLBACK_COOLDOWN` (default `120`s): dentro del cooldown el dictado va directo a local; un éxito de Groq lo resetea. translate solo con target `en`.
- `SMART_COMMANDS_ENABLED` (default: `true`, Ola 1 de PLAN-DICTADO): convierte disparadores dictados con prefijo (`"signo coma"`) en su signo. Ver la sección 20. Es el **único killswitch del proyecto que nace encendido**, y a propósito: la pasada es regex local, sin red ni disco ni LLM, así que no hay dato que se escape ni fallo silencioso posible. Por lo mismo falla ABIERTO: solo el literal `false` apaga, cualquier otro valor deja la pasada activa (al revés de `DASHBOARD_AUTH_ENABLED`, donde el lado seguro del fallo es cerrar).
- `DICTATION_MODES_ENABLED` (default: `false`) - Activa el reformateo post-dictado por uno de los 5 presets (email/chat/codigo/lista/notas), elegido por app en foco o a mano desde la bandeja ("Próximo dictado"). Ver la sección 21; panel descubrible en Ajustes.
- `DICTATION_MODE_MAP` - Mapa `exe:preset` (p. ej. `outlook.exe:email,slack.exe:chat,code.exe:codigo,notepad.exe:lista`) que asigna un preset de reformateo por `.exe` en foco; parseo tolerante a espacios/mayúsculas. Editable desde Ajustes, con la lista de apps afectadas visible antes de activar el flag de arriba.
- `DASHBOARD_AUTH_ENABLED` (default: `true`) - Exige el token local de sesión (`core/localauth.py`) en el dashboard y su API. Solo el valor `false` lo apaga; cualquier otro valor deja la protección encendida (fail-closed). Ver "Security & Privacy" → "Token local de sesión del dashboard".
- `OPS_BRIEFING_PATH` (default: `""`) — Ruta a un `.md` curado por el usuario (proyectos activos, compromisos, metas); si está seteado, su contenido se inyecta SOLO en el chat en vivo "Preguntar" (Ola 7, unidad 7.1). Vacío = apagado.
- `MEETING_RETENTION_DAYS` (default: `0` = conservar siempre) — Si > 0, al ARRANCAR la app se borran definitivamente las reuniones (actas y transcripts) más viejas que N días, incluida su entrada en el índice de búsqueda (`meetings_fts`). Es la única operación destructiva de la unidad 3.2; apagada por defecto. Configurable desde el dashboard (Ajustes → Reuniones), se aplica en el próximo reinicio, no al guardar.

### Backends de insights — Anthropic (API oficial) y claude-cli (suscripción Claude)

Además de `groq` (default), `endpoint` (LM Studio local) y `openrouter`, la capa de insights (`core/insights.py`) soporta dos backends adicionales, configurables por tarea con `INSIGHTS_BACKEND_LIVE` / `INSIGHTS_BACKEND_BATCH`:

- **`anthropic`** — API oficial de Anthropic. Modelo por tarea: live usa `ANTHROPIC_MODEL_LIVE` (Haiku, rápido/barato para el Insight Stream), batch usa `ANTHROPIC_MODEL_BATCH` (Sonnet, más inteligente para acta/chat). Nunca envía `temperature`/sampling params ni el campo `thinking` (Sonnet 5 corre thinking adaptativo por defecto sin configurarlo).
- **`claude-cli`** — usa la suscripción Claude del usuario vía Claude Code headless (`claude -p --model <alias> --output-format text`, prompt por stdin nunca por argv), sin API key. Disponible para vivo Y batch: el análisis en vivo se dispara ~1 vez/min (`INSIGHTS_INTERVAL_SECONDS`) en un hilo daemon con candado de un solo escritor (`core/meeting.py`), así que la latencia de arranque del CLI (~5-15s) solo retrasa el insight, no bloquea la captura. Modelo por tarea: `CLAUDE_CLI_MODEL_LIVE` (haiku) / `CLAUDE_CLI_MODEL_BATCH` (sonnet); timeout 90s en vivo / 180s batch. Coste real: consume la cuota del plan Pro/Max (una reunión larga con análisis en vivo puede gastar decenas de mensajes). Requiere Claude Code instalado y logueado; el `cwd` del subproceso es el directorio de datos de la app (nunca el repo, para no cargar su `CLAUDE.md`/`.mcp.json`).

**Fallback automático con circuit breaker** (`INSIGHTS_FALLBACK`, default `true`): si el backend primario de una tarea falla, `_chat` reintenta UNA vez con groq u openrouter (el primero disponible, nunca `endpoint`/`anthropic`/`claude-cli` como destino de respaldo) y abre un breaker por `INSIGHTS_FALLBACK_COOLDOWN` segundos (default 300) para no reintentar un backend roto en cada llamada. El panel Configuración → Reuniones tiene dos presets de un clic: **⭐ Usar mi suscripción** (ambos selects a `claude-cli`, requiere Claude Code instalado/logueado) y **🌐 Usar APIs (benchmark)** (vivo=`groq`, batch=`openrouter`, requiere ambas keys), más el checkbox del fallback.

### Backend Local (faster-whisper)

- Los modelos se descargan desde Hugging Face en `%APPDATA%\Vflow\models\` (bundle) o `<proyecto>/models/` (dev).
- **Limitación de traducción**: el backend local solo traduce a inglés (task="translate" nativa de Whisper). Para traducir a otros idiomas, usa el backend Groq.
- **Sin fallback a internet**: si el modelo no está descargado y se intenta transcribir, la app muestra un mensaje accionable ("ábrelo desde el dashboard") en lugar de llamar a Groq.
- **Descarga desde el dashboard**: panel Configuración → "Backend de transcripción" → "Local sin internet" → botón "Descargar modelo" con barra de progreso.

## Troubleshooting

| Problem                                                 | Solution                                                                                                                                                                                                   |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pill doesn't appear                                     | Try running as Administrator                                                                                                                                                                               |
| Audio not captured                                      | Check Microphone permissions in Windows Settings → Privacy                                                                                                                                                 |
| Paste doesn't work                                      | Run as Administrator for global hotkey/paste access                                                                                                                                                        |
| Ctrl+C doesn't kill the process                         | This is handled by `signal.signal(signal.SIGINT, signal.SIG_DFL)` in main.py                                                                                                                               |
| Short taps trigger transcription                        | Adjust the 0.3s threshold in `main.py` `_on_hotkey_released`                                                                                                                                               |
| Web dashboard not loading                               | Port auto-selects from 5678. Check: `netstat -an \| findstr 5678`                                                                                                                                          |
| Transcription hangs forever                             | API timeout is 10s. Check your GROQ_API_KEY is valid                                                                                                                                                       |
| Only one instance should run                            | Single-instance mutex (Win32 `Local\VflowSingleInstance`) prevents multiple launches; second instance shows warning and exits                                                                              |
| Text doesn't paste after recording                      | If window verification fails, text is left in clipboard with tray notification; paste manually via Ctrl+V                                                                                                  |
| Transcription failed — audio saved                      | Failed recordings saved to `%APPDATA%\Vflow\last_failed_recording.wav` for debugging (API/network errors)                                                                                                  |
| Logs not showing up                                     | Dev mode logs to project directory; bundled app logs to `%APPDATA%\Vflow\vflow.log` (RotatingFileHandler, 5 MB per file)                                                                                   |
| Clipboard content changed after paste                   | By default, clipboard is not restored. Set `RESTORE_CLIPBOARD=true` in `.env` to keep original clipboard content                                                                                           |
| Accidental recording with IDE shortcuts                 | Increase `ARMING_DELAY` in `config.py` (default 0.15s) to require longer hold before recording starts in Ctrl+Alt/Ctrl+Shift+Alt modes                                                                     |
| Microphone disconnected mid-recording                   | App detects silence within ~2s, stops recording, shows error state on pill, and displays tray notification. Audio watchdog prevents hanging.                                                               |
| Pill appears on wrong monitor                           | Pill auto-detects current monitor based on cursor position. If dragged between monitors and one disconnects, pill repositions to nearest active monitor. Check monitor layout in Windows Display Settings. |
| "Modelo local no descargado" en la notificación         | El backend está en "local" pero el modelo no se ha descargado. Abre el dashboard → Configuración → activa "Local sin internet" → pulsa "Descargar modelo".                                                 |
| El modelo local traduce a un idioma distinto del inglés | El backend local solo soporta traducción a inglés. Para otros idiomas de destino, cambia el backend a Groq en el dashboard.                                                                                |
| La primera transcripción con backend local es lenta     | CTranslate2 hace lazy-alloc en la primera inferencia. El warmup automático (al activar el backend) mitiga esto; si no se lanzó, espera unos segundos en la primera transcripción.                          |
| Backend local falla y no quiero perder el dictado       | Activa `GROQ_FALLBACK=true` (dashboard → Configuración → checkbox "Permitir Groq como respaldo"). Requiere `GROQ_API_KEY`. Advertencia: el audio saldrá a internet cuando el local falle.                  |
| VAD recorta palabras al inicio o final                  | Aumenta `speech_pad_ms` en `core/vad.py` (default 400 ms) o desactiva con `VAD_ENABLED=false` en `.env`.                                                                                                   |
| VAD no filtra / métricas de reunión vacías              | `onnxruntime` debe importarse antes que PyQt6 (con el orden inverso la DLL falla y el VAD queda en fail-open silencioso). `main.py` ya lo hace.                                                            |
