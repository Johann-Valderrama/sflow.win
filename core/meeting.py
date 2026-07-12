"""Modo reunión: captura dual mic + loopback con transcript en vivo de 2 canales.

Pieza central del "modo reunión" (estilo Proactor.ai pero local). A diferencia
del dictado (una fuente, ráfaga corta → pega), una reunión es una SESIÓN larga:

  - Captura el micrófono ("Yo") y el audio del sistema ("Ellos") **a la vez**,
    en dos buffers separados. Esto da diarización de 2 hablantes GRATIS, sin ML:
    tu voz sale por el mic, la voz remota entra por el loopback WASAPI.
  - Cada ~MEETING_CHUNK_SECONDS cierra una ventana por canal, la transcribe
    reutilizando el ``Transcriber`` (que ya aplica VAD en Groq + filtro de
    alucinaciones + diccionario), y añade el segmento al transcript en vivo.
  - El merge es cronológico por ventana: ``[mm:ss Yo] ... [mm:ss Ellos] ...``.

Es un **singleton de proceso** (``MEETING`` al final del módulo) compartido entre
el controlador Qt (hotkey AltGr+R / tray) y el servidor Flask (dashboard). Los
tres puntos de activación llaman a la misma instancia; el dashboard sondea el
transcript por polling.

No toca Qt directamente: corre en threads daemon y expone estado leíble.
La captura dual fue validada con ``test_dual_capture.py`` (sin glitches, drift
por canal <2%, anclaje por wall-clock de llegada suficiente para actas).
"""
import hashlib
import io
import json
import logging
import os
import queue
import re
import threading
import time
import wave

import numpy as np

from config import (
    SAMPLE_RATE,
    MEETING_CHUNK_SECONDS,
    MEETING_CHUNK_MAX_SECONDS,
    MEETING_POLL_SECONDS,
    MEETING_SILENCE_MS,
    MEETING_SILENCE_RMS,
    INSIGHTS_MIN_WORDS,
    INSIGHTS_FIRST_WORDS,
    INSIGHTS_INTERVAL_SECONDS,
    INSIGHTS_CONSOLIDATE_SECONDS,
    INSIGHTS_CONSOLIDATE_COOLDOWN,
    MEETINGS_DIR,
)
from core.recorder import MicSource, LoopbackSource
from core.assistant import _search_terms as _assistant_search_terms
from core.transcriber import Transcriber
from core import insights as _insights
from core import meeting_export as _export
from core import webhook as _webhook
from core import meeting_metrics as _metrics
from core import meeting_templates as _templates
from core import proactive as _proactive
from core import vad as _vad
from db.database import TranscriptionDB

logger = logging.getLogger(__name__)


def _frames_to_wav(frames: list) -> io.BytesIO:
    """Concatena una lista de chunks (N,1) int16 16kHz en un WAV mono en memoria."""
    audio = np.concatenate(frames, axis=0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    return buf


def _fmt_mmss(seconds: float) -> str:
    """Formatea segundos como mm:ss."""
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _parse_mmss(s: str) -> float:
    """Convierte 'mm:ss' a segundos. Defensivo: 0.0 ante cualquier entrada inválida.

    Duplicado deliberado de ``core.insights._mmss_to_seconds`` (misma lógica de 5
    líneas): evita depender de un símbolo privado de otro módulo por un helper
    tan chico. Si ambos divergieran alguna vez, sería una señal de que merece
    promoverse a un util compartido — no antes.
    """
    try:
        parts = str(s).strip().split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:  # noqa: BLE001
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Memoria cruzada en vivo (unidad 5.2) — retrieval puro (cero LLM) sobre actas
# pasadas en cada consolidación. Umbrales del gating anti falsos positivos:
# ---------------------------------------------------------------------------
# Mínimo de tokens significativos (según _tokens) en común entre UN tema del
# rolling state y el texto de la decisión/pendiente del acta pasada. Umbral
# conservador: prefiere callar a avisar en falso.
CROSS_MEMORY_MIN_OVERLAP = 2
# El bm25 de SQLite es negativo para todo match (más negativo = mejor); el score
# debe quedar POR DEBAJO de este techo. El gate decisivo es el overlap de tokens;
# este techo solo descarta filas sin score real (p. ej. fallback LIKE, sin FTS).
CROSS_MEMORY_BM25_MAX = 0.0
# Candidatos FTS a considerar por consolidación (ya vienen ordenados por bm25).
CROSS_MEMORY_MAX_RESULTS = 5


class MeetingSession:
    """Sesión de reunión con captura dual y transcript en vivo.

    Thread-safe: ``start``/``stop``/``toggle`` y la lectura de estado se
    protegen con un lock. Los callbacks de audio (hilos de sounddevice/PyAudio)
    solo añaden frames bajo el mismo lock.
    """

    LABEL_MIC = "Yo"
    LABEL_SYS = "Ellos"

    def __init__(self):
        # Cola de audio del micrófono para el visualizador del pill (mostrar TU voz
        # durante la reunión). El pill apunta su visualizador aquí mientras hay reunión.
        self.viz_queue: queue.Queue = queue.Queue()
        # RLock (reentrante): varios métodos que adquieren el lock se llaman entre sí
        # (p. ej. desde un bloque ya bloqueado). Con Lock no reentrante eso deadlockea
        # y congela todo el proceso (audio + servidor). RLock lo evita de forma segura.
        self._lock = threading.RLock()
        self._active = False
        # Token de generación de reunión (F1, fix de concurrencia jul 2026): cada
        # start() real lo incrementa bajo el lock. Los daemons de insights/consolidación
        # lo capturan al lanzarse y lo re-verifican antes de mergear su resultado en
        # self._insights — así un daemon de una reunión YA cerrada (o sobrescrita por
        # una B que arrancó durante el stop() lento de A) descarta su merge en vez de
        # corromper el estado de la reunión vigente.
        self._session_gen = 0
        # True mientras stop() está drenando/generando el acta (puede tardar segundos-
        # minutos: joins + 2 llamadas LLM). Mientras tanto self._active YA es False, así
        # que start() debe usar ESTE flag (no is_active()) para rechazar un 2º start que
        # pisaría los atributos de instancia que stop() sigue leyendo/mutando.
        self._stopping = False
        self._mic_frames: list = []
        self._sys_frames: list = []
        self._segments: list = []          # [{"t": float, "speaker": str, "text": str}]
        # Segmentos de VOZ (VAD) por canal, en segundos en el EJE DE AUDIO propio
        # del canal (anclaje por muestras acumuladas, NUNCA por window_start: el
        # loopback se salta silencios y su eje diverge del reloj de pared).
        self._speech: dict = {self.LABEL_MIC: [], self.LABEL_SYS: []}
        # Muestras de audio acumuladas YA procesadas por canal (a 16 kHz). El
        # timestamp de reunión de un segmento VAD = (acumulado_previo + offset
        # del segmento en muestras) / 16000, en el eje del canal.
        self._speech_samples: dict = {self.LABEL_MIC: 0, self.LABEL_SYS: 0}
        self._last_metrics: dict | None = None  # métricas de la última reunión terminada (panel 3.2)
        self._highlights: list = []        # [{"t": float, "time": "mm:ss", "source": "manual"}] — momentos marcados con AltGr+H
        # Candidatos automáticos a momento destacado (unidad 5.1 v2), detectados por
        # el Insight Stream sin costo LLM extra: [{"t", "time", "razon", "source":"auto"}].
        # JAMÁS se pasan a generate_minutes(highlights=...) ni entran al gate F12 del
        # acta — solo se combinan con self._highlights al PERSISTIR (highlights_json).
        self._auto_highlights: list = []
        self._notes: list = []             # [{"t": float, "time": "mm:ss", "text": str}] — notas rápidas del usuario
        self._feedback: list = []          # [{"key","tipo","texto","value","t","time"}] — feedback ✓/✗ del único push (unidad 2.2)
        # Detecciones proactivas encoladas en la reunión actual (unidad 5.1):
        # [{"key","clase","base","texto","detail","t","time"}]. Rastro en RAM que
        # (a) deduplica entre ciclos, (b) alimenta ya_reportadas del LLM, (c) se
        # persiste como detections_json al stop() (insumo del bucle de mejora).
        self._detections: list = []
        # Memoria cruzada en vivo (unidad 5.2): ids de reuniones PASADAS por las
        # que ya se emitió tarjeta en ESTA sesión (dedup: máx 1 por reunión pasada).
        self._cross_emitted: set = set()
        # Tarjetas de memoria cruzada emitidas, con timestamp, para el registro del
        # HUD (unidad 5.4): [{"t": float, "time": "mm:ss", "texto": str}].
        self._cross_cards: list = []
        # Niveles por canal (RMS 0..1 del último chunk de audio) para los VU del dashboard.
        # Escritura de float simple: atómica bajo el GIL, no necesita el lock (barato,
        # se recalcula en cada callback de audio; decisión de debate: nada de _tail_rms aquí).
        self._level_mic: float = 0.0
        self._level_sys: float = 0.0
        # Pausa "congelar-reloj": los callbacks dejan de acumular frames pero el reloj
        # se congela también (no sigue corriendo durante la pausa). Ver pause()/resume().
        self._paused: bool = False
        self._paused_total: float = 0.0
        self._pause_started: float | None = None
        self._mic: MicSource | None = None
        self._sys: LoopbackSource | None = None
        # Carryover de prompt por canal: la cola del último texto da contexto al
        # siguiente chunk para no perder palabras en la frontera (como url_transcribe).
        self._carry = {self.LABEL_MIC: "", self.LABEL_SYS: ""}
        self._window_start = 0.0
        self._t0 = 0.0
        self._started_at: str | None = None
        self._chunk_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        # Cola + worker de transcripción: el loop de chunking solo CORTA y encola; un
        # único worker transcribe en background. Así la latencia de Groq no arrastra la
        # cadencia del loop (cadencia más regular = menos jitter). Un solo worker preserva
        # el orden de los segmentos y el carryover.
        self._transcribe_q: queue.Queue | None = None
        self._transcribe_thread: threading.Thread | None = None
        self._transcriber = Transcriber()
        self._sys_available = False        # ¿el loopback arrancó? (si no, reunión solo-mic)
        self._last_error: str | None = None
        self._db: TranscriptionDB | None = None  # lazy: se crea al persistir la 1ª reunión
        # Plantilla por tipo de reunión (unidad 4.3): persiste ENTRE reuniones del
        # mismo proceso (no se resetea en start()). Única fuente de verdad — nunca
        # localStorage — así el hotkey (proceso Python) y el dashboard ven lo mismo.
        self._template: str = _templates.DEFAULT_TEMPLATE

        # Insight Stream con IDs estables: el LLM extrae (contrato simple), el código
        # asigna IDs, deduplica por similitud y NUNCA retira ítems (anti-flicker /
        # anti-retracción). Cada ítem es un dict con "id". temas: {id,text};
        # pendientes: {id,texto,responsable}; propuestas: {id,texto,confianza}.
        self._insights: dict = {"temas": [], "pendientes": [], "propuestas": [], "citas": []}
        self._next_insight_id = 1
        self._insight_buffer: list = []     # texto nuevo no enviado aún al LLM
        self._insight_running = False       # evita llamadas LLM concurrentes
        self._last_insight_at = 0.0         # monotónico de la última actualización
        self._prev_topic_count = 0          # nº de temas en la actualización previa
        self._topic_changed_pending = False # un tema nuevo emergió (señal para consolidación)
        self._last_consolidate_at = 0.0     # monotónico de la última consolidación
        self._last_minutes: dict | None = None  # acta de la última reunión terminada (para el dashboard)
        # Métricas de fluidez (instrumentación para medir, no opinar)
        self._insight_intervals: list = []  # segundos entre actualizaciones (jitter)
        self._churn_samples: list = []      # fracción de ítems que cambian por actualización
        self._retractions = 0               # ítems que aparecieron y desaparecieron sin resolverse
        self._updates_count = 0

    # ------------------------------------------------------------------
    # Estado
    # ------------------------------------------------------------------

    def is_active(self) -> bool:
        return self._active

    def is_stopping(self) -> bool:
        """True mientras un stop() está en curso (drenaje + acta), aunque ``is_active()``
        ya devuelva False. Ver comentario de ``self._stopping`` en ``__init__``."""
        with self._lock:
            return self._stopping

    def get_generation(self) -> int:
        """Token de generación actual (``_session_gen``, incrementado en cada
        ``start()`` real — ver comentario en ``__init__``).

        Expuesto para el polling incremental de ``GET /api/meeting`` (unidad 3.1):
        el cliente compara este valor contra el que guardó en su cursor local para
        detectar que arrancó una reunión nueva (o se reinició) y descartar su
        cursor `since`. Lectura bajo el mismo lock que protege el resto del estado."""
        with self._lock:
            return self._session_gen

    def get_levels(self) -> tuple:
        """(level_mic, level_sys) RMS 0..1 del último chunk, sin el lock (unidad 5.3).

        Los floats se escriben de forma atómica bajo el GIL en _mic_callback/_sys_callback
        (mismo razonamiento documentado ahí para _level_mic/_level_sys): leerlos aquí
        sin adquirir self._lock es intencional y barato — se llama en el tick de 1s
        del proactivo (main.py), y no hay trabajo bajo lock que valga la pena pagar
        por una lectura de dos floats.
        """
        return (self._level_mic, self._level_sys)

    def get_template(self) -> str:
        """Plantilla activa (persiste entre reuniones del proceso)."""
        with self._lock:
            return self._template

    def set_template(self, name: str) -> bool:
        """Cambia la plantilla activa. Devuelve True si ``name`` es válida (se aplicó),
        False si no (se conserva la plantilla previa). Efectivo de inmediato: si hay
        una reunión activa, el resto del ciclo (acta/chips en vivo) usa la nueva
        plantilla desde este momento."""
        if not _templates.is_valid(name):
            return False
        with self._lock:
            self._template = name
        return True

    def _elapsed(self) -> float:
        """Segundos transcurridos desde el inicio, excluyendo el tiempo en pausa.

        Congela el reloj mientras está pausada: usa _pause_started (el instante en
        que se pausó) en vez de "ahora", así el timer no avanza durante la pausa.
        """
        if not self._t0:
            return 0.0
        now = self._pause_started if (self._paused and self._pause_started) else time.monotonic()
        return now - self._t0 - self._paused_total

    def status(self) -> dict:
        """Estado liviano para el dashboard (polling)."""
        with self._lock:
            level_mic = self._level_mic if (self._active and not self._paused) else 0.0
            level_sys = self._level_sys if (self._active and not self._paused) else 0.0
            template = self._template
            return {
                "active": self._active,
                "started_at": self._started_at,
                "elapsed": self._elapsed() if self._active else 0,
                "elapsed_fmt": _fmt_mmss(self._elapsed()) if self._active else "00:00",
                "segment_count": len(self._segments),
                "sys_available": self._sys_available,
                "insight_running": self._insight_running,
                "error": self._last_error,
                "paused": self._paused,
                "levels": {"yo": level_mic, "ellos": level_sys},
                "template": template,
                "template_label": _templates.get(template)["label"],
                "proactive_mode": _proactive.get_mode(),
                # Tarjetas de detección YA ENTREGADAS al HUD y sin feedback ✓/✗
                # todavía (unidad 5.1). El panel web ya las pinta con el renderer
                # genérico renderCards si existen (gancho status().cards de 5.3).
                "cards": self._delivered_cards_locked(),
            }

    def _delivered_cards_locked(self) -> list:
        """Tarjetas de detección entregadas pendientes de feedback (lista corta,
        máx 5, para status()). EL CALLER DEBE TENER EL LOCK.

        Solo detecciones que PROACTIVE ya entregó al HUD (no las que siguen en
        cola esperando lull) y cuya key aún no tiene feedback ✓/✗ del usuario.
        Forma: {key, tipo: "deteccion", texto, detail?} — el contrato de renderCards.
        """
        fb_keys = {f.get("key") for f in self._feedback}
        cards = []
        for d in self._detections:
            if d["key"] in fb_keys:
                continue
            if not _proactive.PROACTIVE.delivered(d["key"]):
                continue
            card = {"key": d["key"], "tipo": "deteccion", "texto": d["texto"]}
            if d.get("detail"):
                card["detail"] = d["detail"]
            cards.append(card)
        return cards[-5:]

    def get_insights(self) -> dict:
        """Devuelve una copia del Insight Stream actual (temas/pendientes/propuestas)."""
        with self._lock:
            return {
                "temas": list(self._insights.get("temas", [])),
                "pendientes": list(self._insights.get("pendientes", [])),
                "propuestas": list(self._insights.get("propuestas", [])),
                "citas": list(self._insights.get("citas", [])),
            }

    def get_last_minutes(self) -> "dict | None":
        """Acta de la última reunión terminada (None si la actual sigue activa o no hubo)."""
        with self._lock:
            return dict(self._last_minutes) if self._last_minutes else None

    def get_last_metrics(self) -> "dict | None":
        """Métricas de la última reunión terminada (None si sigue activa o no hubo)."""
        with self._lock:
            return dict(self._last_metrics) if self._last_metrics else None

    def get_registro(self) -> list:
        """Registro cronológico de la reunión para el panel del HUD (unidad 5.4).

        Une los eventos que el copiloto fue notando —highlights (⭐), notas del
        usuario (📝), detecciones (❓/🤝/⚠️) y memoria cruzada (🕘)— cada uno con su
        timestamp real, ordenados del más reciente al más antiguo (como una línea
        de tiempo que se lee de arriba hacia abajo). Es PULL puro: no interrumpe,
        se consulta. Forma de cada ítem: {tipo, texto, time, t}.

        Nota v1: los pendientes del insight stream (rolling, sin timestamp estable)
        NO entran aquí; viven en las tarjetas "Ahora" y en el dashboard. Se pueden
        añadir después si se decide estampar su primer avistamiento.
        """
        with self._lock:
            entries = []
            for h in self._highlights:
                entries.append({"tipo": "highlight", "texto": "Momento destacado",
                                "time": h.get("time", ""), "t": h.get("t", 0.0)})
            for n in self._notes:
                entries.append({"tipo": "note", "texto": n.get("text", ""),
                                "time": n.get("time", ""), "t": n.get("t", 0.0)})
            for d in self._detections:
                entries.append({"tipo": "deteccion", "texto": d.get("texto", ""),
                                "time": d.get("time", ""), "t": d.get("t", 0.0)})
            for c in self._cross_cards:
                entries.append({"tipo": "cruzada", "texto": c.get("texto", ""),
                                "time": c.get("time", ""), "t": c.get("t", 0.0)})
        entries.sort(key=lambda e: e.get("t", 0.0), reverse=True)
        return entries

    def transcript_segments(self) -> list:
        """Devuelve los segmentos ordenados cronológicamente, listos para render."""
        with self._lock:
            segs = sorted(self._segments, key=lambda s: (s["t"], 0 if s["speaker"] == self.LABEL_MIC else 1))
        return [
            {"t": s["t"], "time": _fmt_mmss(s["t"]), "speaker": s["speaker"], "text": s["text"]}
            for s in segs
        ]

    def snapshot(self) -> dict:
        """Copia atómica del estado en vivo para el chat "Esta reunión" (unidad 2.3).

        Bajo UN SOLO lock copia transcript + insights: el asistente genera su
        respuesta sobre esta foto y el estado puede seguir mutando (o la reunión
        terminar) sin afectarla. Todos los contenedores devueltos son nuevos y
        sus valores son inmutables (str/float/None): mutar la sesión después NO
        cambia el snapshot. Los insights van en formato plano (sin IDs):
        temas [str], pendientes/propuestas/citas [{texto, ...}].
        """
        with self._lock:
            return {
                "active": self._active,
                "started_at": self._started_at,
                "elapsed": self._elapsed() if self._active else 0.0,
                "segments": self.transcript_segments(),
                "insights": self._store_to_plain_locked(),
            }

    def add_highlight(self) -> "dict | None":
        """Marca el instante actual como momento destacado (AltGr+H). Idempotente-safe:
        cada llamada añade un highlight nuevo (no es un toggle). Devuelve el dict
        {"t", "time", "source": "manual"} añadido, o None si no hay reunión activa.

        "source": "manual" (unidad 5.1 v2, retrocompat): distingue este highlight
        de los candidatos automáticos (source="auto") cuando ambos se combinan al
        persistir highlights_json. Entradas viejas en la DB sin "source" se leen
        como manuales por defecto (ver web/blueprints/meetings.py)."""
        with self._lock:
            if not self._active:
                return None
            t = self._elapsed()
            item = {"t": round(t, 1), "time": _fmt_mmss(t), "source": "manual"}
            self._highlights.append(item)
            return item

    def add_note(self, text: str) -> "dict | None":
        """Añade una nota rápida al instante actual. Devuelve el dict añadido,
        o None si no hay reunión activa o el texto está vacío."""
        text = (text or "").strip()
        if not text:
            return None
        with self._lock:
            if not self._active:
                return None
            t = self._elapsed()
            item = {"t": round(t, 1), "time": _fmt_mmss(t), "text": text}
            self._notes.append(item)
            return item

    def add_feedback(self, key: str, tipo: str, texto: str, value: int) -> "dict | None":
        """Registra el feedback ✓/✗ del único push permitido en vivo (unidad 2.2).

        Persiste la señal para el bucle de mejora de prompts; NO afecta el
        Insight Stream ni la UI en vivo más allá de la confirmación de la tarjeta.
        Dedup por key: si ya había feedback para esa key, REEMPLAZA su value en
        vez de duplicar (una sola señal por ítem, la más reciente gana).
        Devuelve el item añadido/actualizado, o None si no hay reunión activa o
        el value no es válido.
        """
        key = (key or "").strip()
        if not key or value not in (1, -1):
            return None
        with self._lock:
            if not self._active:
                return None
            t = self._elapsed()
            item = {
                "key": key,
                "tipo": (tipo or "").strip(),
                "texto": (texto or "").strip(),
                "value": value,
                "t": round(t, 1),
                "time": _fmt_mmss(t),
            }
            for existing in self._feedback:
                if existing["key"] == key:
                    existing.update(item)
                    return existing
            self._feedback.append(item)
            return item

    def pause(self) -> dict:
        """Pausa la captura: congela el reloj y deja de acumular frames.

        Marca la pausa PRIMERO (bajo el lock) y hace el flush de la ventana actual
        DESPUÉS: con _paused=True los callbacks dejan de acumular, así ningún frame
        que llegue durante el flush se cuela en la ventana post-pausa (antes el
        flush iba primero y frames pre-pausa contaminaban la siguiente ventana).
        Idempotente.
        """
        with self._lock:
            if not self._active or self._paused:
                return {"ok": True, "paused": self._paused}
            self._paused = True
            self._pause_started = time.monotonic()
            window_start = self._window_start
        # _flush_window adquiere su propio lock; se llama fuera del bloque anterior
        # para no anidar innecesariamente (el lock es reentrante, pero mejor evitarlo).
        self._flush_window(window_start)
        with self._lock:
            self._window_start = self._elapsed()
            return {"ok": True, "paused": True}

    def resume(self) -> dict:
        """Reanuda la captura tras una pausa. Idempotente."""
        with self._lock:
            if not self._active or not self._paused:
                return {"ok": True, "paused": self._paused}
            if self._pause_started is not None:
                self._paused_total += time.monotonic() - self._pause_started
            self._paused = False
            self._pause_started = None
            self._window_start = self._elapsed()
            return {"ok": True, "paused": False}

    def transcript_text(self) -> str:
        """Transcript completo como texto plano (para persistir)."""
        return "\n".join(
            f"[{s['time']} {s['speaker']}] {s['text']}" for s in self.transcript_segments()
        )

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def start(self) -> dict:
        """Inicia la captura dual y el loop de chunking. Idempotente."""
        with self._lock:
            if self._active:
                return {"ok": True, "already_active": True}
            if self._stopping:
                # stop() de la reunión anterior sigue drenando/generando el acta:
                # arrancar ahora pisaría self.* que ese stop() todavía lee/muta.
                return {
                    "ok": False,
                    "stopping": True,
                    "error": "La reunión anterior aún se está guardando. Espera unos segundos e intenta de nuevo.",
                }
            self._session_gen += 1
            self._mic_frames = []
            self._sys_frames = []
            self._segments = []
            self._speech = {self.LABEL_MIC: [], self.LABEL_SYS: []}
            self._speech_samples = {self.LABEL_MIC: 0, self.LABEL_SYS: 0}
            self._last_metrics = None
            self._highlights = []
            self._auto_highlights = []
            self._notes = []
            self._feedback = []
            self._detections = []
            self._cross_emitted = set()
            self._cross_cards = []
            self._level_mic = 0.0
            self._level_sys = 0.0
            self._paused = False
            self._paused_total = 0.0
            self._pause_started = None
            self._last_error = None
            self._carry = {self.LABEL_MIC: "", self.LABEL_SYS: ""}
            self._window_start = 0.0
            self._drain_viz_queue()
            self._insights = {"temas": [], "pendientes": [], "propuestas": [], "citas": []}
            self._next_insight_id = 1
            self._insight_buffer = []
            self._insight_running = False
            self._last_insight_at = time.monotonic()
            self._prev_topic_count = 0
            self._topic_changed_pending = False
            self._last_consolidate_at = time.monotonic()
            self._last_minutes = None
            self._insight_intervals = []
            self._churn_samples = []
            self._retractions = 0
            self._updates_count = 0
            self._t0 = time.monotonic()
            self._started_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._mic = MicSource()
            self._sys = LoopbackSource()

        # Arrancar el micrófono: si falla, la reunión no tiene sentido → abortar.
        try:
            self._mic.start(self._mic_callback)
        except Exception as exc:  # noqa: BLE001
            logger.error("Reunión: no se pudo abrir el micrófono: %s", exc)
            with self._lock:
                self._mic = None
                self._sys = None
                self._last_error = "No se pudo abrir el micrófono."
            return {"ok": False, "error": self._last_error}

        # Arrancar el loopback: si falla (sin dispositivo de salida activo, etc.),
        # degradar a reunión solo-mic en vez de abortar.
        try:
            self._sys.start(self._sys_callback)
            self._sys_available = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reunión: loopback no disponible, grabando solo micrófono: %s", exc)
            self._sys_available = False
            self._sys = None

        self._active = True
        self._stop_event = threading.Event()
        self._transcribe_q = queue.Queue()
        self._transcribe_thread = threading.Thread(target=self._transcribe_worker, daemon=True)
        self._transcribe_thread.start()
        self._chunk_thread = threading.Thread(target=self._chunk_loop, daemon=True)
        self._chunk_thread.start()
        logger.info("Reunión iniciada (loopback=%s).", self._sys_available)
        return {"ok": True, "sys_available": self._sys_available}

    def stop(self) -> dict:
        """Detiene la captura, hace el flush final y persiste la reunión."""
        with self._lock:
            if not self._active:
                return {"ok": True, "already_stopped": True}
            # Resume implícito: si se termina en pausa, cierra la contabilidad del
            # tiempo pausado ANTES de leer _elapsed() para la duración final.
            if self._paused and self._pause_started is not None:
                self._paused_total += time.monotonic() - self._pause_started
                self._paused = False
                self._pause_started = None
            self._active = False  # los callbacks dejan de acumular frames
            # F1 (fix concurrencia): marca que el cierre está en curso ANTES de soltar
            # el lock. start() rechaza mientras este flag siga True (ver start()),
            # aunque _active ya sea False. El finally de más abajo SIEMPRE lo baja,
            # incluso si algo dentro del cuerpo de abajo lanza una excepción.
            self._stopping = True
        try:
            duration = self._elapsed()

            # Detener fuentes (no llegan más frames)
            for src in (self._mic, self._sys):
                if src is not None:
                    try:
                        src.stop()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Reunión: error al detener fuente: %s", exc)

            # Señalar al loop que termine: hará un último flush antes de salir
            if self._stop_event is not None:
                self._stop_event.set()
            if self._chunk_thread is not None:
                self._chunk_thread.join(timeout=120)

            # Drenar la cola de transcripción pendiente ANTES del acta: el chunk loop hizo
            # un último flush (encoló los frames restantes); el sentinela None cierra el worker
            # tras procesar todo lo pendiente, dejando el transcript completo para el acta.
            if self._transcribe_q is not None:
                self._transcribe_q.put(None)
            if self._transcribe_thread is not None:
                self._transcribe_thread.join(timeout=120)

            self._mic = None
            self._sys = None
            self._drain_viz_queue()

            transcript = self.transcript_text()
            segments = self.transcript_segments()
            insights = self.get_insights()

            # Métricas de conversación Yo/Ellos (unidad 3.1): puras, sin LLM. El worker
            # ya terminó (join arriba), así que _speech está completo y estable.
            with self._lock:
                speech_copy = {k: list(v) for k, v in self._speech.items()}
            has_speech = any(speech_copy.values())
            meeting_metrics = _metrics.compute_metrics(speech_copy, segments, duration)
            with self._lock:
                self._last_metrics = meeting_metrics  # para el panel, junto a _last_minutes

            # Acta post-reunión: una sola llamada LLM sobre el transcript completo, alimentada
            # con el análisis en vivo para que sea consistente con lo que vio el usuario.
            # Fail-safe: si el LLM no está disponible devuelve un acta vacía.
            with self._lock:
                highlights = list(self._highlights)          # SOLO manuales (gate F12)
                auto_highlights = list(self._auto_highlights)  # nunca al acta — solo a persistencia
                notes = list(self._notes)
                feedback = list(self._feedback)
                detections = list(self._detections)
                template = self._template
            # highlights= es SOLO manuales a propósito: los candidatos automáticos
            # (source="auto") jamás alimentan el acta ni su gate anti-alucinación
            # F12 (core.insights._normalize_momentos) — se combinan con los
            # manuales más abajo, únicamente al persistir highlights_json.
            minutes = _insights.generate_minutes(transcript, self._store_to_plain(),
                                                 highlights=highlights, notes=notes,
                                                 segments=segments, template=template)
            with self._lock:
                self._last_minutes = minutes  # para que el dashboard la muestre aunque se terminara por hotkey/tray

            # Línea de tiempo de momentos clave (capítulos etiquetados por LLM).
            # Fail-safe: cualquier fallo devuelve [] y nunca bloquea el insert/acta.
            try:
                chapters = _insights.generate_chapters(transcript, segments)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reunión: error generando capítulos (se continúa sin ellos): %s", exc)
                chapters = []

            # Persistencia ÚNICA aquí (no en los callers): así da igual si la reunión
            # se terminó desde el hotkey, el tray o el dashboard — se guarda una sola vez.
            meeting_id = None
            saved = False
            if transcript and os.getenv("SAVE_HISTORY", "true").lower() == "true":
                try:
                    if self._db is None:
                        self._db = TranscriptionDB()
                    title = f"Reunión {self._started_at or ''}".strip()
                    insert_kwargs = dict(
                        title=title,
                        transcript=transcript,
                        segments_json=json.dumps(segments, ensure_ascii=False),
                        duration_seconds=duration,
                        started_at=self._started_at,
                        insights_json=json.dumps(insights, ensure_ascii=False),
                        minutes_json=json.dumps(minutes, ensure_ascii=False),
                        chapters_json=json.dumps(chapters, ensure_ascii=False),
                        template=template,
                    )
                    # highlights_json persiste manuales + autos JUNTOS (cada uno con su
                    # "source"); la acta (arriba) ya usó SOLO los manuales — esto es
                    # exclusivamente para el historial/visor (unidad 5.1 v2).
                    persisted_highlights = highlights + auto_highlights
                    if persisted_highlights:
                        insert_kwargs["highlights_json"] = json.dumps(persisted_highlights, ensure_ascii=False)
                    if notes:
                        insert_kwargs["notes_json"] = json.dumps(notes, ensure_ascii=False)
                    if feedback:
                        insert_kwargs["feedback_json"] = json.dumps(feedback, ensure_ascii=False)
                    # Rastro de detecciones proactivas (unidad 5.1): insumo del bucle
                    # de mejora junto a feedback_json. Patrón notes_json: NULL si no hubo.
                    if detections:
                        insert_kwargs["detections_json"] = json.dumps(detections, ensure_ascii=False)
                    # Patrón notes_json: solo se persiste si hubo voz detectada (sin
                    # speech las métricas serían todo ceros — mejor columna NULL).
                    if has_speech:
                        insert_kwargs["metrics_json"] = json.dumps(meeting_metrics, ensure_ascii=False)
                    meeting_id = self._db.meeting_insert(**insert_kwargs)
                    saved = True
                except Exception as exc:  # noqa: BLE001
                    logger.error("No se pudo guardar la reunión en la DB: %s", exc)

            # Export a Markdown (contrato OPS), best-effort. Se hace al terminar para que la
            # carpeta esté siempre al día sin necesidad de un programador de tareas.
            if saved and meeting_id is not None:
                try:
                    _export.export_meeting({
                        "id": meeting_id,
                        "started_at": self._started_at,
                        "duration_seconds": duration,
                        "transcript": transcript,
                        "minutes_json": json.dumps(minutes, ensure_ascii=False),
                        "insights_json": json.dumps(insights, ensure_ascii=False),
                    }, MEETINGS_DIR)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Export a markdown falló: %s", exc)

            # Webhook saliente + dead-drop de pendientes (unidad 6.1), fire-and-forget en
            # hilo daemon: un fallo de red JAMÁS bloquea ni propaga a stop(). Si la reunión
            # NO se persistió (meeting_id None) el webhook no dispara (lo loguea dentro).
            try:
                _webhook.dispatch_async({
                    "id": meeting_id,
                    "title": (f"Reunión {self._started_at or ''}".strip()),
                    "started_at": self._started_at,
                    "duration_seconds": duration,
                    "minutes_json": json.dumps(minutes, ensure_ascii=False),
                    "insights_json": json.dumps(insights, ensure_ascii=False),
                    "chapters_json": json.dumps(chapters, ensure_ascii=False),
                }, meeting_id)
            except Exception as exc:  # noqa: BLE001  (defensa extra; dispatch_async ya aísla)
                logger.warning("No se pudo disparar el webhook: %s", exc)

            metrics = self._fluidity_metrics()
            logger.info("Reunión detenida: %.0fs, %d segmentos (guardada=%s).", duration, len(segments), saved)
            logger.info(
                "Fluidez: %d updates, churn_avg=%.3f, retracciones=%d, intervalo=%.1fs (jitter=%.1fs)",
                metrics["updates"], metrics["churn_avg"], metrics["retractions"],
                metrics["interval_avg_s"], metrics["interval_jitter_s"],
            )
            return {
                "ok": True,
                "duration_seconds": duration,
                "transcript": transcript,
                "segments": segments,
                "insights": insights,
                "minutes": minutes,
                "metrics": metrics,
                # "metrics" ya lo ocupan las métricas de fluidez (instrumentación);
                # las de conversación Yo/Ellos van bajo su propia clave.
                "meeting_metrics": meeting_metrics,
                "started_at": self._started_at,
                "meeting_id": meeting_id,
                "saved": saved,
                "template": template,
            }
        finally:
            # Fase 3 (F1): pase lo que pase arriba (incluida una excepción), el
            # cierre se marca terminado. Así una excepción en el acta jamás deja
            # la sesión bloqueada rechazando cualquier start() futuro.
            with self._lock:
                self._stopping = False

    def toggle(self) -> dict:
        """Inicia si está parada, detiene si está activa. Devuelve el nuevo estado."""
        if self._active:
            res = self.stop()
            res["state"] = "stopped"
            return res
        res = self.start()
        res["state"] = "started"
        return res

    # ------------------------------------------------------------------
    # Captura y chunking
    # ------------------------------------------------------------------

    @staticmethod
    def _chunk_rms(chunk: np.ndarray) -> float:
        """RMS (0-1) barato de UN chunk de audio (nada de ventana deslizante: es
        justo lo que necesita el VU del dashboard, no la detección de silencio)."""
        if chunk.size == 0:
            return 0.0
        arr = chunk.astype(np.float32) / 32768.0
        return float(min(np.sqrt(np.mean(arr ** 2)) * 4.0, 1.0))  # factor de escala, satura a 1.0

    def _mic_callback(self, chunk: np.ndarray):
        with self._lock:
            if not self._active or self._paused:
                return
            self._mic_frames.append(chunk)
            # Alimentar el visualizador del pill (tu voz). Cap para no crecer sin
            # límite si nadie consume; el visualizador drena a VIZ_FPS.
            if self.viz_queue.qsize() < 32:
                self.viz_queue.put(chunk)
        self._level_mic = self._chunk_rms(chunk)

    def _sys_callback(self, chunk: np.ndarray):
        with self._lock:
            if not self._active or self._paused:
                return
            self._sys_frames.append(chunk)
        self._level_sys = self._chunk_rms(chunk)

    def _drain_viz_queue(self):
        """Vacía la cola del visualizador (al iniciar/terminar reunión)."""
        while True:
            try:
                self.viz_queue.get_nowait()
            except queue.Empty:
                break

    def _chunk_loop(self):
        """Cierra cada ventana en una PAUSA de silencio cerca del objetivo.

        Revisa cada MEETING_POLL_SECONDS: hace flush cuando la ventana llegó al
        objetivo Y ambos canales están en silencio (pausa natural), o cuando se
        alcanza el tope (corte forzado aunque nadie pare de hablar). Al recibir
        stop, hace un último flush y sale.
        """
        self._window_start = self._elapsed()
        while True:
            fired = self._stop_event.wait(MEETING_POLL_SECONDS)
            if fired:
                self._flush_window(self._window_start)
                break
            dur = self._elapsed() - self._window_start
            if dur >= MEETING_CHUNK_MAX_SECONDS or (dur >= MEETING_CHUNK_SECONDS and self._both_quiet()):
                self._flush_window(self._window_start)
                self._window_start = self._elapsed()

    @staticmethod
    def _tail_rms(frames: list) -> float:
        """RMS (0-1) de los últimos MEETING_SILENCE_MS de audio de una lista de frames."""
        if not frames:
            return 0.0
        needed = int(SAMPLE_RATE * MEETING_SILENCE_MS / 1000)
        tail, total = [], 0
        for f in reversed(frames):
            tail.append(f)
            total += f.shape[0]
            if total >= needed:
                break
        arr = np.concatenate(list(reversed(tail)), axis=0).astype(np.float32) / 32768.0
        if arr.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(arr ** 2)))

    def _both_quiet(self) -> bool:
        """True si ambos canales están en silencio al final de la ventana (pausa natural).

        Un canal sin frames recientes cuenta como silencioso (p. ej. el loopback no
        entrega buffers en silencio total)."""
        with self._lock:
            mic = list(self._mic_frames)
            sys = list(self._sys_frames)
        return self._tail_rms(mic) < MEETING_SILENCE_RMS and self._tail_rms(sys) < MEETING_SILENCE_RMS

    def _flush_window(self, window_start: float):
        """Corta la ventana: extrae los frames acumulados y los ENCOLA para el worker.

        No transcribe aquí (eso bloquearía el loop de chunking y arrastraría la cadencia);
        el worker de transcripción lo hace en background.
        """
        with self._lock:
            mic_frames = self._mic_frames
            sys_frames = self._sys_frames
            self._mic_frames = []
            self._sys_frames = []
        if (mic_frames or sys_frames) and self._transcribe_q is not None:
            self._transcribe_q.put((window_start, mic_frames, sys_frames))

    def _transcribe_worker(self):
        """Worker único: transcribe las ventanas encoladas en orden. None = sentinela de fin."""
        while True:
            item = self._transcribe_q.get()
            if item is None:
                break
            window_start, mic_frames, sys_frames = item
            try:
                self._process_window(window_start, mic_frames, sys_frames)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reunión: error procesando ventana: %s", exc)

    def _process_window(self, window_start: float, mic_frames: list, sys_frames: list):
        """Transcribe los frames de cada canal y añade los segmentos (corre en el worker)."""
        for frames, label in ((mic_frames, self.LABEL_MIC), (sys_frames, self.LABEL_SYS)):
            if not frames:
                continue
            # --- Métricas de voz (VAD) con anclaje POR MUESTRAS por canal ---
            # La inferencia corre FUERA del lock de la sesión (los frames llegan
            # detached como argumentos del worker); solo el append de resultados
            # y la actualización del contador van bajo el lock. El contador
            # avanza SIEMPRE (aunque el VAD falle o no haya voz) para que el eje
            # de audio del canal no se desalinee.
            audio = np.concatenate(frames, axis=0).reshape(-1)
            total_samples = int(audio.shape[0])
            base_samples = self._speech_samples.get(label, 0)  # único escritor: este worker serial
            speech_segs: list = []
            try:
                rel = _vad.speech_timestamps(audio.astype(np.float32) / 32768.0)
                base_s = base_samples / float(SAMPLE_RATE)
                speech_segs = _metrics.merge_segments(
                    [{"start": base_s + s["start"], "end": base_s + s["end"]} for s in rel]
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reunión: VAD de métricas falló en '%s' (se continúa sin voz): %s", label, exc)
            with self._lock:
                if speech_segs:
                    self._speech[label].extend(speech_segs)
                self._speech_samples[label] = base_samples + total_samples

            carry = self._carry.get(label, "")  # contexto del chunk anterior de ESTE canal
            try:
                wav = _frames_to_wav(frames)
                # El Transcriber aplica VAD (Groq) + filtro de alucinaciones + diccionario.
                # En silencio devuelve "" → no se añade segmento. El carryover (prompt) da
                # contexto para no perder palabras en la frontera entre chunks.
                text = self._transcriber.transcribe(wav, prompt=carry or None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reunión: error transcribiendo ventana de '%s': %s", label, exc)
                with self._lock:
                    self._last_error = f"Error de transcripción: {exc}"
                continue
            text = (text or "").strip()
            if not text:
                continue
            self._carry[label] = text[-200:]  # cola para el contexto del próximo chunk
            with self._lock:
                self._segments.append({"t": window_start, "speaker": label, "text": text})
                # Con marcador [mm:ss] (unidad 5.1): las detecciones devuelven "time"
                # copiándolo literalmente del delta — sin marcador el LLM no tendría
                # de dónde sacarlo sin inventar. El extractor de estado lo ignora.
                self._insight_buffer.append(f"[{_fmt_mmss(window_start)}] {label}: {text}")

        # Tras incorporar la ventana: primero la consolidación (si toca, tiene prioridad),
        # luego la actualización incremental. Ambas comparten el mutex _insight_running,
        # así que nunca corren a la vez (sin carrera entre los dos escritores del estado).
        self._maybe_consolidate()
        self._maybe_update_insights()

    # ------------------------------------------------------------------
    # Insight Stream (rolling state, en background)
    # ------------------------------------------------------------------

    def _maybe_update_insights(self):
        """Dispara una actualización del rolling state si hay delta suficiente.

        Condición: hay texto nuevo Y (≥ INSIGHTS_MIN_WORDS palabras nuevas O han
        pasado ≥ INSIGHTS_INTERVAL_SECONDS desde la última). Una sola llamada LLM
        a la vez (corre en un hilo daemon para no bloquear el loop de chunking).
        """
        if not _insights.is_available():
            return
        with self._lock:
            if self._insight_running or not self._insight_buffer:
                return
            words = sum(len(s.split()) for s in self._insight_buffer)
            # Primera actualización: umbral bajo para que el análisis aparezca pronto
            # (si no, el panel se siente "congelado" durante el primer minuto).
            threshold = INSIGHTS_FIRST_WORDS if self._updates_count == 0 else INSIGHTS_MIN_WORDS
            elapsed_ok = (time.monotonic() - self._last_insight_at) >= INSIGHTS_INTERVAL_SECONDS
            if words < threshold and not elapsed_ok:
                return
            delta = "\n".join(self._insight_buffer)
            self._insight_buffer = []
            self._insight_running = True
            state = self._store_to_plain_locked()  # ya estamos dentro del lock
            # Token de generación (F1): si para cuando este daemon termine ya arrancó
            # una reunión B (self._session_gen cambió), el merge se descarta.
            gen = self._session_gen

        threading.Thread(target=self._run_insight_update, args=(state, delta, gen), daemon=True).start()

    def _maybe_consolidate(self):
        """Dispara la consolidación por evento (cambio de tema con cooldown) o por tiempo máximo.

        Comparte el mutex _insight_running con la actualización incremental: si una está
        en curso, esta no arranca (y viceversa). Así un solo escritor toca el estado.
        """
        if not _insights.is_available() or INSIGHTS_CONSOLIDATE_SECONDS <= 0:
            return
        with self._lock:
            if self._insight_running:
                return
            now = time.monotonic()
            since = now - self._last_consolidate_at
            due_time = since >= INSIGHTS_CONSOLIDATE_SECONDS
            due_topic = self._topic_changed_pending and since >= INSIGHTS_CONSOLIDATE_COOLDOWN
            if not (due_time or due_topic):
                return
            if not (self._insights["temas"] or self._insights["pendientes"]):
                return  # nada que consolidar todavía
            self._insight_running = True
            self._last_consolidate_at = now
            self._topic_changed_pending = False
            # Token de generación (F1): ver _maybe_update_insights.
            gen = self._session_gen

        threading.Thread(target=self._run_consolidation, args=(gen,), daemon=True).start()

    def _run_consolidation(self, gen: int):
        """Pasada de consolidación (bloqueante, en hilo). Aplica el resultado de forma
        aditiva/refinada al store (preserva IDs, no retira ítems). Siempre libera el flag.

        ``gen`` es el token de generación de reunión capturado al lanzar este hilo
        (F1, fix de concurrencia): si para cuando el LLM responde ya arrancó una
        reunión B (``self._session_gen`` cambió), el merge se descarta — de lo
        contrario esta pasada tardía de A contaminaría el estado de B. El check es
        SOLO por gen (no por ``_active``): un daemon del gen vigente es legítimo
        aunque ``_active`` haya volteado durante su propio cierre.
        """
        plain = self._store_to_plain()
        transcript = self.transcript_text()
        try:
            consolidated = _insights.consolidate(transcript, plain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reunión: error en consolidación: %s", exc)
            consolidated = plain
        cross_memory_ok = False
        try:
            with self._lock:
                if gen != self._session_gen:
                    logger.info("Reunión: consolidación descartada (gen obsoleto, reunión ya cerrada/reemplazada).")
                else:
                    self._merge_plain_into_store(consolidated)
                    self._prev_topic_count = len(self._insights["temas"])
                    cross_memory_ok = True
                self._last_insight_at = time.monotonic()
        finally:
            with self._lock:
                self._insight_running = False
        if cross_memory_ok:
            logger.info("Reunión: consolidación aplicada con transcript completo.")

            # Memoria cruzada en vivo (unidad 5.2): retrieval puro (cero LLM) sobre
            # actas pasadas, fuera del lock. Best-effort total: un fallo aquí JAMÁS
            # rompe la consolidación.
            try:
                self._cross_memory_check()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reunión: error en memoria cruzada (se ignora): %s", exc)

    def _run_insight_update(self, plain_prev: dict, delta: str, gen: int):
        """Llama al LLM (texto plano) y fusiona el resultado en el store con IDs.

        El LLM extrae; el código mantiene la estabilidad: IDs estables por similitud,
        ítems pegajosos (no se retiran), redacción se actualiza in-situ. Siempre libera el flag.

        Detecciones proactivas (unidad 5.1): la MISMA llamada devuelve además las
        detecciones vía out-param (no gasta cuota extra). Se procesan al final,
        fuera del lock del merge; su fallo nunca afecta el estado.

        Momentos candidatos (unidad 5.1 v2): mismo patrón, gateado por
        ``AUTO_HIGHLIGHTS_ENABLED`` — con el flag apagado ``momentos_out`` viaja
        como ``None`` y update_state ni siquiera añade la tarea al prompt.

        ``gen`` (F1, fix de concurrencia): token de generación capturado al lanzar
        este hilo. Si para cuando el LLM responde ya arrancó una reunión B
        (``self._session_gen`` cambió), el merge y las detecciones/momentos se
        descartan — de lo contrario esta respuesta tardía de A contaminaría el
        estado de B.
        """
        detections: dict = {}
        auto_enabled = self._auto_highlights_enabled()
        momentos: "list | None" = [] if auto_enabled else None
        with self._lock:
            # Contexto de dedup para el LLM: textos ya avisados (los últimos 20 bastan).
            ya_reportadas = [d.get("base", "") for d in self._detections][-20:]
        try:
            llm_state = _insights.update_state(plain_prev, delta,
                                               detections_out=detections,
                                               ya_reportadas=ya_reportadas,
                                               momentos_out=momentos)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reunión: error en Insight Stream: %s", exc)
            llm_state = plain_prev
        now = time.monotonic()
        stale = False
        try:
            with self._lock:
                if gen != self._session_gen:
                    stale = True
                    logger.info("Reunión: actualización de insights descartada (gen obsoleto).")
                else:
                    if self._last_insight_at:
                        self._insight_intervals.append(now - self._last_insight_at)
                    prev_total = sum(len(self._insights.get(k, [])) for k in ("temas", "pendientes", "propuestas", "citas"))
                    _added, changed = self._merge_plain_into_store(llm_state)
                    if prev_total:
                        self._churn_samples.append(changed / prev_total)
                    self._updates_count += 1

                    n_temas = len(self._insights["temas"])
                    if n_temas > self._prev_topic_count:
                        self._topic_changed_pending = True  # tema nuevo → señal para consolidación (paso D)
                    self._prev_topic_count = n_temas
                    # Surfacing del error del backend de insights (p. ej. LM Studio caído):
                    # que el panel muestre el fallo en vez de parecer "congelado".
                    self._last_error = _insights.last_error()
                self._last_insight_at = now
        finally:
            with self._lock:
                self._insight_running = False

        if stale:
            return

        # Detecciones proactivas (unidad 5.1): fuera del lock, fail-safe total.
        try:
            self._handle_detections(detections)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reunión: error procesando detecciones (se ignoran): %s", exc)

        # Momentos candidatos (unidad 5.1 v2): idem, fuera del lock, fail-safe total.
        if momentos is not None:
            try:
                self._handle_momentos_candidatos(momentos)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reunión: error procesando momentos candidatos (se ignoran): %s", exc)

    # ------------------------------------------------------------------
    # Detecciones proactivas (unidad 5.1)
    # ------------------------------------------------------------------

    # Flag por clase (env, default tras la calibración con transcripts reales).
    # Una clase ruidosa se apaga cambiando SU default aquí, sin tocar más código.
    _DETECTION_FLAGS = {
        "preguntas_sin_responder": ("PROACTIVE_DETECT_PREGUNTAS", "true"),
        "compromisos": ("PROACTIVE_DETECT_COMPROMISOS", "true"),
        "acuerdos_vagos": ("PROACTIVE_DETECT_ACUERDOS", "true"),
    }

    _DETECTION_TEXT_FIELD = {
        "preguntas_sin_responder": "pregunta",
        "compromisos": "texto",
        "acuerdos_vagos": "texto",
    }

    @staticmethod
    def _detection_key(texto: str) -> str:
        """Key estable de una detección: hash del texto NORMALIZADO (minúsculas,
        sin puntuación, espacios colapsados). Los IDs del rolling state son
        inestables entre actualizaciones; el hash del texto no."""
        norm = re.sub(r"\W+", " ", str(texto or "").lower()).strip()
        return "det-" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _format_detection_card(clase: str, item: dict) -> str:
        """Texto de la tarjeta HUD/panel por clase de detección."""
        if clase == "preguntas_sin_responder":
            return f"❓ Pregunta sin responder: {item.get('pregunta', '')}".strip()
        if clase == "compromisos":
            return f"🤝 Compromiso: {item.get('texto', '')}".strip()
        falta = item.get("falta", "ambos")
        label = {"fecha": "sin fecha", "responsable": "sin dueño"}.get(falta, "sin fecha/dueño")
        return f"⚠️ Acuerdo {label}: {item.get('texto', '')}".strip()

    @classmethod
    def _detection_class_enabled(cls, clase: str) -> bool:
        env_name, default = cls._DETECTION_FLAGS[clase]
        return (os.getenv(env_name, default) or default).strip().lower() == "true"

    def _handle_detections(self, detections: dict):
        """Convierte las detecciones del LLM en tarjetas y las encola en PROACTIVE.

        Gating en orden (todas las puertas anti-ruido de la unidad 5.1):
          1. Modo: solo copilot/trainer (silent = cero proactividad extra).
          2. Flag por clase (PROACTIVE_DETECT_PREGUNTAS/_COMPROMISOS/_ACUERDOS).
          3. Dedup local por key (hash del texto normalizado) contra el rastro
             de la reunión — complementa el dedup del LLM (ya_reportadas) y el
             de la cola (delivered/discarded keys).
          4. Presupuesto de atención vía try_push("deteccion", card) — ~1 push
             no-pendiente cada 5 min, decidido y consumido ATÓMICAMENTE (unidad
             1.1: un solo acquire del lock de PROACTIVE evita el TOCTOU entre
             decidir y marcar el push que existía con should_push/mark_pushed
             separados). Si el presupuesto está agotado, la detección NO se
             encola NI se registra: si sigue vigente, el LLM la re-reporta en un
             ciclo posterior y entra entonces (mejor tarde que perdida o ráfaga).
        La cola (PROACTIVE) añade encima caducidad y espera de lull; la entrega
        al HUD ya está cableada en main.py (_tick_proactive, genérica por tipo).
        """
        if not detections:
            return
        if _proactive.get_mode() not in ("copilot", "trainer"):
            return
        if not self._active:
            return
        for clase, text_field in self._DETECTION_TEXT_FIELD.items():
            if not self._detection_class_enabled(clase):
                continue
            for item in detections.get(clase) or []:
                if not isinstance(item, dict):
                    continue
                base = str(item.get(text_field) or "").strip()
                if not base:
                    continue
                key = self._detection_key(base)
                with self._lock:
                    if any(d["key"] == key for d in self._detections):
                        continue
                texto = self._format_detection_card(clase, item)
                card = {"key": key, "tipo": "deteccion", "texto": texto}
                detail = str(item.get("time") or "").strip() or None
                if detail:
                    card["detail"] = detail
                if not _proactive.PROACTIVE.try_push("deteccion", card):
                    continue
                with self._lock:
                    t = self._elapsed()
                    self._detections.append({
                        "key": key,
                        "clase": clase,
                        "base": base,
                        "texto": texto,
                        "detail": detail,
                        "t": round(t, 1),
                        "time": _fmt_mmss(t),
                    })

    # ------------------------------------------------------------------
    # Marcado automático de momentos clave (unidad 5.1 v2)
    # ------------------------------------------------------------------

    @staticmethod
    def _auto_highlights_enabled() -> bool:
        """Kill-switch AUTO_HIGHLIGHTS_ENABLED (env, lectura perezosa — mismo
        patrón que ``_cross_memory_enabled``). Con el flag apagado, meeting.py
        ni siquiera pide la tarea adicional a update_state (cero tokens extra)."""
        return (os.getenv("AUTO_HIGHLIGHTS_ENABLED", "true") or "true").strip().lower() == "true"

    def _handle_momentos_candidatos(self, momentos: list):
        """Convierte los candidatos automáticos del Insight Stream (ya limpiados
        por ``insights._clean_momentos``) en highlights ``source="auto"``.

        Gating y dedup (diseño cerrado, no re-litigar):
          1. Sesión activa (si terminó entre el disparo y la respuesta, se ignora).
          2. Item con "razon" y "time" (mm:ss) válidos; "time" no parseable → descarte.
          3. Dedup contra highlights MANUALES: si cae a <2s de uno ya marcado con
             AltGr+H, se descarta el auto (el manual manda — es la señal más fuerte).
          4. Dedup entre autos: si cae a <5s de un auto ya registrado en esta
             reunión (de una ventana anterior), se descarta (evita duplicar el
             mismo instante si el LLM lo vuelve a reportar).
        JAMÁS toca self._highlights ni se pasa a generate_minutes(highlights=...):
        los autos nunca entran al gate F12 del acta (ver core/insights.py
        _normalize_momentos). Solo se combinan con los manuales al PERSISTIR
        (ver stop()). Fail-safe silencioso: nunca lanza.
        """
        if not momentos:
            return
        if not self._active:
            return
        for item in momentos:
            if not isinstance(item, dict):
                continue
            razon = str(item.get("razon") or "").strip()
            time_s = str(item.get("time") or "").strip()
            if not razon or not time_s:
                continue
            t = _parse_mmss(time_s)
            if t <= 0 and time_s not in ("0:00", "00:00"):
                continue
            with self._lock:
                if not self._active:
                    return
                if any(abs(h.get("t", 0.0) - t) < 2.0 for h in self._highlights):
                    continue  # cerca de un highlight MANUAL: el manual manda
                if any(abs(a.get("t", 0.0) - t) < 5.0 for a in self._auto_highlights):
                    continue  # ya reportado en una ventana anterior
                self._auto_highlights.append({
                    "t": round(t, 1), "time": _fmt_mmss(t),
                    "razon": razon, "source": "auto",
                })

    # ------------------------------------------------------------------
    # Memoria cruzada en vivo (unidad 5.2)
    # ------------------------------------------------------------------

    @staticmethod
    def _cross_memory_enabled() -> bool:
        """Kill-switch PROACTIVE_DETECT_CRUZADA (env, default on). Mismo patrón
        que los flags PROACTIVE_DETECT_* de 5.1: lectura perezosa en cada uso,
        así se puede apagar sin reiniciar la app (y monkeypatchear en tests)."""
        return (os.getenv("PROACTIVE_DETECT_CRUZADA", "true") or "true").strip().lower() == "true"

    @staticmethod
    def _fmt_ddmm(started_at) -> str:
        """'2026-06-12 10:00:00' → '12/06'. Devuelve '' si no hay fecha parseable."""
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(started_at or ""))
        return f"{m.group(3)}/{m.group(2)}" if m else ""

    @staticmethod
    def _minutes_past_items(minutes: dict) -> list:
        """Decisiones y pendientes de un acta pasada como textos planos.

        Tolera los DOS formatos históricos de bullets: str plano (actas viejas)
        y dict {texto, t?} (trazabilidad de la Ola 4). Nunca lanza excepción.
        """
        items = []
        if not isinstance(minutes, dict):
            return items
        for key in ("decisiones", "pendientes"):
            seq = minutes.get(key)
            if not isinstance(seq, list):
                continue
            for it in seq:
                if isinstance(it, str):
                    text = it.strip()
                elif isinstance(it, dict):
                    text = str(it.get("texto") or "").strip()
                else:
                    continue
                if text:
                    items.append(text)
        return items

    def _cross_memory_check(self):
        """Memoria cruzada en vivo: si los temas del rolling state ya se trataron
        en reuniones PASADAS (actas en la DB), emite una tarjeta push tipo
        "El 12/06 se acordó: <decisión/pendiente>". Retrieval puro: CERO LLM.

        Corre en cada consolidación (~240s), nunca en cada update_state. Gating
        en orden (anti falsos positivos, v1):
          1. Flag PROACTIVE_DETECT_CRUZADA (kill-switch, default on).
          2. Modo: solo copilot/trainer (silent = cero proactividad extra).
          3. FTS bm25 (score proyectado por meetings_search) para candidatear.
          4. Overlap REAL de tokens (≥ CROSS_MEMORY_MIN_OVERLAP significativos,
             helper _tokens) entre UN tema actual y el texto del acta pasada.
          5. Dedup: máx 1 tarjeta por reunión pasada por sesión (_cross_emitted).
          6. Presupuesto vía try_push("cruzada", card) de la máquina 5.3 —
             decisión + encolado atómicos (unidad 1.1); si está agotado o el
             dedup de la cola rechaza la tarjeta, NO se registra el dedup local
             (_cross_emitted): si el tema sigue vivo, re-entra en una
             consolidación posterior (mejor tarde que perdida).
        Se excluyen SIEMPRE la reunión activa y las reuniones sin acta.
        """
        if not self._cross_memory_enabled():
            return
        if _proactive.get_mode() not in ("copilot", "trainer"):
            return
        if not self._active:
            return
        with self._lock:
            temas = [t["text"] for t in self._insights["temas"]]
            emitted = set(self._cross_emitted)
            own_started_at = self._started_at
        if not temas:
            return
        temas_tokens = [toks for toks in (self._tokens(t) for t in temas) if toks]
        if not temas_tokens:
            return
        query = _assistant_search_terms(" ".join(temas)).strip()
        if not query:
            return

        if self._db is None:
            self._db = TranscriptionDB()
        results = self._db.meetings_search(query, limit=CROSS_MEMORY_MAX_RESULTS, match="or")

        for r in results:
            mid = r.get("id")
            if mid is None or mid in emitted:
                continue
            # Excluir la reunión activa. Normalmente ni existe en la DB (se
            # inserta al stop()), pero el started_at la delata si existiera.
            if own_started_at and r.get("started_at") == own_started_at:
                continue
            score = r.get("score")
            if score is None or score >= CROSS_MEMORY_BM25_MAX:
                continue
            row = self._db.meeting_get(mid)
            if not row or not row.get("minutes_json"):
                continue  # reunión sin acta: fuera
            try:
                minutes = json.loads(row["minutes_json"])
            except (ValueError, TypeError):
                continue

            # Mejor decisión/pendiente del acta por overlap de tokens con UN tema.
            best_text, best_overlap = None, 0
            for item_text in self._minutes_past_items(minutes):
                item_toks = self._tokens(item_text)
                if not item_toks:
                    continue
                overlap = max(len(item_toks & t_toks) for t_toks in temas_tokens)
                if overlap > best_overlap:
                    best_overlap, best_text = overlap, item_text
            if best_text is None or best_overlap < CROSS_MEMORY_MIN_OVERLAP:
                continue

            fecha = self._fmt_ddmm(row.get("started_at"))
            prefijo = f"El {fecha} se acordó" if fecha else "En una reunión pasada se acordó"
            texto = f"{prefijo}: {best_text}"
            card = {"key": f"cruz-{mid}", "tipo": "cruzada", "texto": texto}
            # Presupuesto de atención (máquina 5.3), atómico: sin presupuesto (o
            # rechazada por dedup de la cola) no se emite NI se registra el dedup
            # local — puede re-entrar en la próxima consolidación.
            if not _proactive.PROACTIVE.try_push("cruzada", card):
                return
            with self._lock:
                self._cross_emitted.add(mid)
                t = self._elapsed()
                self._cross_cards.append({"t": round(t, 1), "time": _fmt_mmss(t), "texto": texto})
            logger.info("Reunión: memoria cruzada emitida (reunión pasada %s, overlap=%d).",
                        mid, best_overlap)
            return  # máx 1 tarjeta por consolidación (el presupuesto manda igual)

    def _store_to_plain_locked(self) -> dict:
        """Convierte el store con IDs a texto plano. EL CALLER DEBE TENER EL LOCK."""
        return {
            "temas": [t["text"] for t in self._insights["temas"]],
            "pendientes": [{"texto": p["texto"], "responsable": p.get("responsable"),
                            "fecha": p.get("fecha"), "hora": p.get("hora")}
                           for p in self._insights["pendientes"]],
            "propuestas": [{"texto": p["texto"], "confianza": p.get("confianza", "media")}
                           for p in self._insights["propuestas"]],
            "citas": [{"texto": c["texto"], "fecha": c.get("fecha"), "hora": c.get("hora")}
                      for c in self._insights.get("citas", [])],
        }

    def _store_to_plain(self) -> dict:
        """Convierte el store con IDs a texto plano (adquiere el lock)."""
        with self._lock:
            return self._store_to_plain_locked()

    def _new_insight_id(self) -> int:
        i = self._next_insight_id
        self._next_insight_id += 1
        return i

    _MATCH_THRESHOLD = 0.65   # similitud de caracteres para considerar mismo ítem
    _TOKEN_CONTAIN = 0.7      # solapamiento de tokens (robusto ante reformulaciones)
    _STOPWORDS = frozenset({
        "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al",
        "y", "o", "en", "a", "que", "se", "su", "sus", "lo", "le", "es", "por", "para",
    })

    @classmethod
    def _tokens(cls, s: str) -> set:
        # Mantener tokens largos y CUALQUIER token con dígito (los números discriminan:
        # "Resident Evil 2" ≠ "Resident Evil 3"; "Q1" ≠ "Q2"; "versión 2020" ≠ "2024").
        return {w for w in s.strip().lower().split()
                if w not in cls._STOPWORDS and (len(w) >= 2 or any(c.isdigit() for c in w))}

    @staticmethod
    def _num_tokens(toks: set) -> set:
        """Tokens que contienen algún dígito (2, 3, q1, 2020…) — son discriminantes."""
        return {t for t in toks if any(c.isdigit() for c in t)}

    @classmethod
    def _same_item(cls, a: str, b: str) -> bool:
        """¿a y b son el mismo ítem? Combina similitud de caracteres y solapamiento de tokens,
        pero NUNCA fusiona si difieren sus tokens numéricos.

        Esto evita el sobre-merge por similitud de caracteres ("Resident Evil 2" vs
        "Resident Evil 3" tienen ~0.93 de ratio pero son temas distintos), conservando
        el match de reformulaciones reales ("plazo Q1" vs "el plazo del hito Q1").
        """
        import difflib
        a, b = a.strip().lower(), b.strip().lower()
        if not a or not b:
            return False
        if a == b:
            return True
        ta, tb = cls._tokens(a), cls._tokens(b)
        # Números/identificadores distintos → ítems distintos (regla decisiva).
        if cls._num_tokens(ta) != cls._num_tokens(tb):
            return False
        if difflib.SequenceMatcher(None, a, b).ratio() >= cls._MATCH_THRESHOLD:
            return True
        if not ta or not tb:
            return False
        inter = len(ta & tb)
        return inter / min(len(ta), len(tb)) >= cls._TOKEN_CONTAIN

    def _merge_plain_into_store(self, plain: dict):
        """Fusiona el estado plano del LLM en el store con IDs. Devuelve (añadidos, cambiados).

        - Match por similitud → mismo ID (estable), actualiza redacción in-situ.
        - Sin match → ítem nuevo con ID nuevo.
        - Los ítems previos que el LLM no devolvió se CONSERVAN (pegajosos): nunca se retiran.
        """
        added = changed = 0

        def merge_list(store_list, incoming, text_key):
            nonlocal added, changed
            for item in incoming:
                text = (item if text_key is None else str(item.get(text_key, ""))).strip()
                if not text:
                    continue
                field = "text" if text_key is None else "texto"
                match = None
                for e in store_list:
                    if self._same_item(e[field], text):
                        match = e
                        break
                # Campos opcionales que se conservan/actualizan (compromisos, propuestas)
                optional = ("responsable", "confianza", "fecha", "hora")
                if match is None:
                    new_item = {"id": self._new_insight_id()}
                    if text_key is None:
                        new_item["text"] = text
                    else:
                        new_item["texto"] = text
                        for k in optional:
                            if item.get(k):
                                new_item[k] = item.get(k)
                    store_list.append(new_item)
                    added += 1
                else:
                    cur = match["text" if text_key is None else "texto"]
                    if cur != text:
                        match["text" if text_key is None else "texto"] = text
                        changed += 1
                    if text_key is not None:
                        for k in optional:
                            if item.get(k):
                                match[k] = item.get(k)

        merge_list(self._insights["temas"], plain.get("temas", []), None)
        merge_list(self._insights["pendientes"], plain.get("pendientes", []), "texto")
        merge_list(self._insights["propuestas"], plain.get("propuestas", []), "texto")
        self._insights.setdefault("citas", [])
        merge_list(self._insights["citas"], plain.get("citas", []), "texto")
        return added, changed

    def _fluidity_metrics(self) -> dict:
        """Resumen de métricas de fluidez de la sesión (churn, jitter, retracciones)."""
        import statistics
        churn = round(sum(self._churn_samples) / len(self._churn_samples), 3) if self._churn_samples else 0.0
        jitter = round(statistics.pstdev(self._insight_intervals), 1) if len(self._insight_intervals) > 1 else 0.0
        avg_interval = round(sum(self._insight_intervals) / len(self._insight_intervals), 1) if self._insight_intervals else 0.0
        return {
            "updates": self._updates_count,
            "churn_avg": churn,            # fracción media de ítems que cambian/desaparecen (↓ mejor)
            "retractions": self._retractions,  # ítems retirados sin resolver (↓ mejor, ideal 0)
            "interval_avg_s": avg_interval,
            "interval_jitter_s": jitter,  # desviación del intervalo entre updates (↓ = más regular)
        }


# Singleton de proceso compartido entre el controlador Qt y el servidor Flask.
MEETING = MeetingSession()
