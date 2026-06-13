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

        # Insight Stream (rolling state): temas / pendientes / propuestas en vivo
        self._insights: dict = _insights.empty_state()
        self._insight_buffer: list = []     # texto nuevo no enviado aún al LLM
        self._insight_running = False       # evita llamadas LLM concurrentes
        self._last_insight_at = 0.0         # monotónico de la última actualización
        self._prev_topic_count = 0          # nº de temas en la actualización previa
        self._topic_changed_pending = False # un tema nuevo emergió (señal para consolidación)

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
            self._insights = _insights.empty_state()
            self._insight_buffer = []
            self._insight_running = False
            self._last_insight_at = time.monotonic()
            self._prev_topic_count = 0
            self._topic_changed_pending = False
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

        logger.info("Reunión detenida: %.0fs, %d segmentos (guardada=%s).", duration, len(segments), saved)
        return {
            "ok": True,
            "duration_seconds": duration,
            "transcript": transcript,
            "segments": segments,
            "insights": insights,
            "minutes": minutes,
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

        # Tras incorporar la ventana, evaluar si toca actualizar el Insight Stream
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
            state = dict(self._insights)

        threading.Thread(target=self._run_insight_update, args=(state, delta), daemon=True).start()

    def _run_insight_update(self, state: dict, delta: str):
        """Llama al LLM (bloqueante) y publica el nuevo estado. Siempre libera el flag."""
        try:
            new_state = _insights.update_state(state, delta)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reunión: error en Insight Stream: %s", exc)
            new_state = state
        with self._lock:
            # Detección de cambio de tema EN CÓDIGO (robusta, sin depender de que el
            # LLM emita un campo extra): si aparecieron temas nuevos respecto a la
            # actualización previa, marcamos un cambio de tema pendiente. Lo usa la
            # consolidación por evento (paso D) para refrescar en momentos naturales.
            n_temas = len(new_state.get("temas", []))
            if n_temas > self._prev_topic_count:
                self._topic_changed_pending = True
            self._prev_topic_count = n_temas
            self._insights = new_state
            self._insight_running = False
            self._last_insight_at = time.monotonic()


# Singleton de proceso compartido entre el controlador Qt y el servidor Flask.
MEETING = MeetingSession()
