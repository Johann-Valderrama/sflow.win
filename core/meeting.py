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
import threading
import time
import wave

import numpy as np

from config import (
    SAMPLE_RATE,
    MEETING_CHUNK_SECONDS,
    INSIGHTS_MIN_WORDS,
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
        self._lock = threading.Lock()
        self._active = False
        self._mic_frames: list = []
        self._sys_frames: list = []
        self._segments: list = []          # [{"t": float, "speaker": str, "text": str}]
        self._mic: MicSource | None = None
        self._sys: LoopbackSource | None = None
        self._t0 = 0.0
        self._started_at: str | None = None
        self._chunk_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._transcriber = Transcriber()
        self._sys_available = False        # ¿el loopback arrancó? (si no, reunión solo-mic)
        self._last_error: str | None = None
        self._db: TranscriptionDB | None = None  # lazy: se crea al persistir la 1ª reunión

        # Insight Stream con IDs estables: el LLM extrae (contrato simple), el código
        # asigna IDs, deduplica por similitud y NUNCA retira ítems (anti-flicker /
        # anti-retracción). Cada ítem es un dict con "id". temas: {id,text};
        # pendientes: {id,texto,responsable}; propuestas: {id,texto,confianza}.
        self._insights: dict = {"temas": [], "pendientes": [], "propuestas": []}
        self._next_insight_id = 1
        self._insight_buffer: list = []     # texto nuevo no enviado aún al LLM
        self._insight_running = False       # evita llamadas LLM concurrentes
        self._last_insight_at = 0.0         # monotónico de la última actualización
        self._prev_topic_count = 0          # nº de temas en la actualización previa
        self._topic_changed_pending = False # un tema nuevo emergió (señal para consolidación)
        self._last_consolidate_at = 0.0     # monotónico de la última consolidación
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
            }

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
            self._insights = {"temas": [], "pendientes": [], "propuestas": []}
            self._next_insight_id = 1
            self._insight_buffer = []
            self._insight_running = False
            self._last_insight_at = time.monotonic()
            self._prev_topic_count = 0
            self._topic_changed_pending = False
            self._last_consolidate_at = time.monotonic()
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

        self._mic = None
        self._sys = None

        transcript = self.transcript_text()
        segments = self.transcript_segments()
        insights = self.get_insights()

        # Acta post-reunión: una sola llamada LLM sobre el transcript completo.
        # Fail-safe: si el LLM no está disponible devuelve un acta vacía.
        minutes = _insights.generate_minutes(transcript)

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

    def _sys_callback(self, chunk: np.ndarray):
        with self._lock:
            if self._active:
                self._sys_frames.append(chunk)

    def _chunk_loop(self):
        """Cada MEETING_CHUNK_SECONDS cierra una ventana por canal y la transcribe.

        Al recibir la señal de stop hace un último flush de lo que quede y sale.
        """
        while True:
            window_start = self._elapsed()
            fired = self._stop_event.wait(MEETING_CHUNK_SECONDS)
            self._flush_window(window_start)
            if fired:
                break

    def _flush_window(self, window_start: float):
        """Extrae los frames acumulados de cada canal, transcribe y añade segmentos."""
        with self._lock:
            mic_frames = self._mic_frames
            sys_frames = self._sys_frames
            self._mic_frames = []
            self._sys_frames = []

        for frames, label in ((mic_frames, self.LABEL_MIC), (sys_frames, self.LABEL_SYS)):
            if not frames:
                continue
            try:
                wav = _frames_to_wav(frames)
                # El Transcriber aplica VAD (Groq) + filtro de alucinaciones + diccionario.
                # En silencio devuelve "" → no se añade segmento.
                text = self._transcriber.transcribe(wav)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Reunión: error transcribiendo ventana de '%s': %s", label, exc)
                with self._lock:
                    self._last_error = f"Error de transcripción: {exc}"
                continue
            text = (text or "").strip()
            if not text:
                continue
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
            elapsed_ok = (time.monotonic() - self._last_insight_at) >= INSIGHTS_INTERVAL_SECONDS
            if words < INSIGHTS_MIN_WORDS and not elapsed_ok:
                return
            delta = "\n".join(self._insight_buffer)
            self._insight_buffer = []
            self._insight_running = True
            state = self._store_to_plain()  # el LLM recibe/devuelve texto plano

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
            prev_total = sum(len(self._insights[k]) for k in ("temas", "pendientes", "propuestas"))
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

    def _store_to_plain(self) -> dict:
        """Convierte el store con IDs a texto plano (lo que el LLM recibe y devuelve)."""
        with self._lock:
            return {
                "temas": [t["text"] for t in self._insights["temas"]],
                "pendientes": [{"texto": p["texto"], "responsable": p.get("responsable")}
                               for p in self._insights["pendientes"]],
                "propuestas": [{"texto": p["texto"], "confianza": p.get("confianza", "media")}
                               for p in self._insights["propuestas"]],
            }

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
        return {w for w in s.strip().lower().split() if w not in cls._STOPWORDS and len(w) >= 2}

    @classmethod
    def _same_item(cls, a: str, b: str) -> bool:
        """¿a y b son el mismo ítem? Combina similitud de caracteres y solapamiento de tokens.

        El solapamiento de tokens captura reformulaciones que SequenceMatcher pierde
        (p. ej. "plazo Q1" vs "el plazo del hito Q1"), sin sobre-fusionar ítems distintos.
        """
        import difflib
        a, b = a.strip().lower(), b.strip().lower()
        if not a or not b:
            return False
        if difflib.SequenceMatcher(None, a, b).ratio() >= cls._MATCH_THRESHOLD:
            return True
        ta, tb = cls._tokens(a), cls._tokens(b)
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
                if match is None:
                    new_item = {"id": self._new_insight_id()}
                    if text_key is None:
                        new_item["text"] = text
                    else:
                        new_item["texto"] = text
                        if "responsable" in item:
                            new_item["responsable"] = item.get("responsable")
                        if "confianza" in item:
                            new_item["confianza"] = item.get("confianza", "media")
                    store_list.append(new_item)
                    added += 1
                else:
                    cur = match["text" if text_key is None else "texto"]
                    if cur != text:
                        match["text" if text_key is None else "texto"] = text
                        changed += 1
                    if text_key is not None:
                        if item.get("responsable"):
                            match["responsable"] = item.get("responsable")
                        if item.get("confianza"):
                            match["confianza"] = item.get("confianza")

        merge_list(self._insights["temas"], plain.get("temas", []), None)
        merge_list(self._insights["pendientes"], plain.get("pendientes", []), "texto")
        merge_list(self._insights["propuestas"], plain.get("propuestas", []), "texto")
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
