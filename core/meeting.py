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
import io
import json
import logging
import os
import queue
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
)
from core.recorder import MicSource, LoopbackSource
from core.transcriber import Transcriber
from core import insights as _insights
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
        self._mic_frames: list = []
        self._sys_frames: list = []
        self._segments: list = []          # [{"t": float, "speaker": str, "text": str}]
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

    def _elapsed(self) -> float:
        return time.monotonic() - self._t0 if self._t0 else 0.0

    def status(self) -> dict:
        """Estado liviano para el dashboard (polling)."""
        with self._lock:
            return {
                "active": self._active,
                "started_at": self._started_at,
                "elapsed": self._elapsed() if self._active else 0,
                "elapsed_fmt": _fmt_mmss(self._elapsed()) if self._active else "00:00",
                "segment_count": len(self._segments),
                "sys_available": self._sys_available,
                "insight_running": self._insight_running,
                "error": self._last_error,
            }

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

    def transcript_segments(self) -> list:
        """Devuelve los segmentos ordenados cronológicamente, listos para render."""
        with self._lock:
            segs = sorted(self._segments, key=lambda s: (s["t"], 0 if s["speaker"] == self.LABEL_MIC else 1))
        return [
            {"t": s["t"], "time": _fmt_mmss(s["t"]), "speaker": s["speaker"], "text": s["text"]}
            for s in segs
        ]

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
            self._mic_frames = []
            self._sys_frames = []
            self._segments = []
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
            self._active = False  # los callbacks dejan de acumular frames
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

        # Acta post-reunión: una sola llamada LLM sobre el transcript completo, alimentada
        # con el análisis en vivo para que sea consistente con lo que vio el usuario.
        # Fail-safe: si el LLM no está disponible devuelve un acta vacía.
        minutes = _insights.generate_minutes(transcript, self._store_to_plain())
        with self._lock:
            self._last_minutes = minutes  # para que el dashboard la muestre aunque se terminara por hotkey/tray

        # Persistencia ÚNICA aquí (no en los callers): así da igual si la reunión
        # se terminó desde el hotkey, el tray o el dashboard — se guarda una sola vez.
        meeting_id = None
        saved = False
        if transcript and os.getenv("SAVE_HISTORY", "true").lower() == "true":
            try:
                if self._db is None:
                    self._db = TranscriptionDB()
                title = f"Reunión {self._started_at or ''}".strip()
                meeting_id = self._db.meeting_insert(
                    title=title,
                    transcript=transcript,
                    segments_json=json.dumps(segments, ensure_ascii=False),
                    duration_seconds=duration,
                    started_at=self._started_at,
                    insights_json=json.dumps(insights, ensure_ascii=False),
                    minutes_json=json.dumps(minutes, ensure_ascii=False),
                )
                saved = True
            except Exception as exc:  # noqa: BLE001
                logger.error("No se pudo guardar la reunión en la DB: %s", exc)

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
            "started_at": self._started_at,
            "meeting_id": meeting_id,
            "saved": saved,
        }

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

    def _mic_callback(self, chunk: np.ndarray):
        with self._lock:
            if self._active:
                self._mic_frames.append(chunk)
                # Alimentar el visualizador del pill (tu voz). Cap para no crecer sin
                # límite si nadie consume; el visualizador drena a VIZ_FPS.
                if self.viz_queue.qsize() < 32:
                    self.viz_queue.put(chunk)

    def _sys_callback(self, chunk: np.ndarray):
        with self._lock:
            if self._active:
                self._sys_frames.append(chunk)

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
                self._insight_buffer.append(f"{label}: {text}")

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

        threading.Thread(target=self._run_insight_update, args=(state, delta), daemon=True).start()

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

        threading.Thread(target=self._run_consolidation, daemon=True).start()

    def _run_consolidation(self):
        """Pasada de consolidación (bloqueante, en hilo). Aplica el resultado de forma
        aditiva/refinada al store (preserva IDs, no retira ítems). Siempre libera el flag."""
        plain = self._store_to_plain()
        transcript = self.transcript_text()
        try:
            consolidated = _insights.consolidate(transcript, plain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reunión: error en consolidación: %s", exc)
            consolidated = plain
        with self._lock:
            self._merge_plain_into_store(consolidated)
            self._prev_topic_count = len(self._insights["temas"])
            self._insight_running = False
            self._last_insight_at = time.monotonic()
        logger.info("Reunión: consolidación aplicada con transcript completo.")

    def _run_insight_update(self, plain_prev: dict, delta: str):
        """Llama al LLM (texto plano) y fusiona el resultado en el store con IDs.

        El LLM extrae; el código mantiene la estabilidad: IDs estables por similitud,
        ítems pegajosos (no se retiran), redacción se actualiza in-situ. Siempre libera el flag.
        """
        try:
            llm_state = _insights.update_state(plain_prev, delta)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reunión: error en Insight Stream: %s", exc)
            llm_state = plain_prev
        now = time.monotonic()
        with self._lock:
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
            self._insight_running = False
            self._last_insight_at = now
            # Surfacing del error del backend de insights (p. ej. LM Studio caído):
            # que el panel muestre el fallo en vez de parecer "congelado".
            self._last_error = _insights.last_error()

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
