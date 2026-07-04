"""Filtrado VAD (Voice Activity Detection) para el path Groq.

Usa el modelo Silero VAD que ya incluye faster-whisper para recortar silencios
del buffer de audio ANTES de enviarlo a la API Groq. Esto reduce:
- Costo y latencia (menos bytes enviados).
- Alucinaciones de Whisper en grabaciones con mucho silencio inicial/final.

El backend local ya tiene su propio VAD interno (vad_filter=True en
faster_whisper.WhisperModel.transcribe), por lo que este módulo solo se usa
desde GroqBackend.

Variables de entorno:
    VAD_ENABLED (default "true") — Apagar en caso de problemas; el audio pasa
                                   sin modificar (fail-open nunca rompe el dictado).

Contrato:
    apply_vad(wav_buffer: io.BytesIO) -> io.BytesIO | None
        - Devuelve un nuevo BytesIO con solo los segmentos de voz (más padding).
        - Devuelve None si no se detecta ninguna voz (permite que el backend
          devuelva "" sin llamar a la API).
        - Si VAD está desactivado o falla por cualquier razón, devuelve el
          buffer original sin modificar (posición reseteada a 0).
        - El buffer de entrada puede estar en cualquier posición; se hace
          seek(0) internamente. El buffer original NO se modifica.
"""
import io
import logging
import os
import struct
import threading
import wave

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caché módulo-nivel del modelo Silero VAD
# ---------------------------------------------------------------------------
# get_vad_model() de faster-whisper construye 2 InferenceSession ONNX; cachearlo
# aquí garantiza UNA sola construcción por proceso con carga thread-safe (doble
# check bajo _VAD_LOAD_LOCK), independientemente de la semántica de caché de la
# versión instalada de la librería. La inferencia va bajo _VAD_INFER_LOCK (lock
# PROPIO del VAD, nunca el lock de MeetingSession): las sesiones ONNX se
# comparten entre el dictado (apply_vad) y el modo reunión (speech_timestamps),
# que corren en hilos distintos.
_VAD_MODEL = None
_VAD_LOAD_LOCK = threading.Lock()
_VAD_INFER_LOCK = threading.Lock()


def _get_cached_vad_model():
    """Devuelve el modelo Silero VAD cacheado a nivel módulo (carga thread-safe).

    En faster-whisper 1.1.1 get_vad_model() ya viene decorado con lru_cache, por
    lo que get_speech_timestamps() (que lo llama internamente) recibe la MISMA
    instancia que cacheamos aquí: llamar a esta función antes de la inferencia
    asegura que la construcción de las sesiones ONNX ocurre una vez y bajo
    nuestro lock, nunca en caliente dentro de una ventana de reunión.
    """
    global _VAD_MODEL
    if _VAD_MODEL is None:
        with _VAD_LOAD_LOCK:
            if _VAD_MODEL is None:
                from faster_whisper.vad import get_vad_model
                _VAD_MODEL = get_vad_model()
    return _VAD_MODEL


def speech_timestamps(audio) -> list:
    """Detecta segmentos de voz en un array de audio y los devuelve en SEGUNDOS.

    Args:
        audio: np.ndarray float32 mono 16 kHz normalizado en [-1, 1] (1-D).

    Returns:
        list[dict]: [{"start": float, "end": float}] en SEGUNDOS relativos al
        inicio del audio dado. faster-whisper devuelve MUESTRAS (ints); aquí se
        convierte dividiendo por 16000. Lista vacía si no hay voz.

    La inferencia corre bajo el lock propio del VAD (_VAD_INFER_LOCK), nunca
    bajo el lock de MeetingSession: el caller debe llamar SIN sostener locks
    ajenos. Lanza excepciones si faster-whisper no está disponible (el caller
    decide el fail-open).
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    if audio is None or len(audio) == 0:
        return []

    _get_cached_vad_model()  # asegura carga única fuera del hot path

    # Padding corto (100 ms) a propósito: estos timestamps alimentan métricas de
    # talk-time; el padding generoso de apply_vad (400 ms) inflaría los totales.
    vad_opts = VadOptions(
        threshold=0.5,
        min_silence_duration_ms=500,
        min_speech_duration_ms=100,
        speech_pad_ms=100,
    )
    with _VAD_INFER_LOCK:
        raw = get_speech_timestamps(audio, vad_opts, sampling_rate=16000)
    return [
        {"start": round(seg["start"] / 16000.0, 3), "end": round(seg["end"] / 16000.0, 3)}
        for seg in raw
    ]


def apply_vad(wav_buffer: io.BytesIO) -> "io.BytesIO | None":
    """Aplica Silero VAD al buffer WAV y devuelve audio recortado.

    Args:
        wav_buffer: Buffer WAV con audio 16kHz mono int16. Puede estar en
                    cualquier posición; se hace seek(0) internamente.

    Returns:
        Nuevo io.BytesIO con los segmentos de voz (incluido padding), listo
        para lectura desde el inicio.  None si no se detecta voz.
        En caso de error o VAD desactivado: el buffer original (seek(0)).
    """
    if os.getenv("VAD_ENABLED", "true").lower().strip() != "true":
        wav_buffer.seek(0)
        return wav_buffer

    try:
        return _apply_vad_impl(wav_buffer)
    except Exception as exc:  # noqa: BLE001
        logger.debug("VAD: error inesperado — pasando audio sin modificar. %s", exc)
        wav_buffer.seek(0)
        return wav_buffer


# ---------------------------------------------------------------------------
# Implementación interna
# ---------------------------------------------------------------------------

def _apply_vad_impl(wav_buffer: io.BytesIO) -> "io.BytesIO | None":
    """Implementación real del VAD; lanza excepciones si algo falla."""
    import numpy as np

    try:
        from faster_whisper.vad import (
            VadOptions,
            collect_chunks,
            get_speech_timestamps,
        )
    except ImportError:
        logger.debug("VAD: faster_whisper no instalado — pasando audio sin modificar")
        wav_buffer.seek(0)
        return wav_buffer

    # --- Decodificar WAV → float32 numpy ---
    wav_buffer.seek(0)
    with wave.open(wav_buffer, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth != 2:
        # Solo int16 soportado; devolver original
        logger.debug("VAD: sampwidth=%d no soportado — pasando audio sin modificar", sampwidth)
        wav_buffer.seek(0)
        return wav_buffer

    # Decodificar int16 → float32 en [-1, 1]
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    # Si es estéreo, mezclar a mono
    if n_channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)

    # Remuestrear a 16kHz si es necesario (Silero requiere 16kHz)
    if framerate != 16000:
        try:
            import scipy.signal as _scipy_signal
            target_len = int(len(samples) * 16000 / framerate)
            samples = _scipy_signal.resample(samples, target_len)
            framerate = 16000
        except ImportError:
            logger.debug("VAD: scipy no disponible para remuestrear %dHz — pasando sin modificar", framerate)
            wav_buffer.seek(0)
            return wav_buffer

    # --- Detección de voz con parámetros conservadores ---
    vad_opts = VadOptions(
        min_silence_duration_ms=500,
        speech_pad_ms=400,        # padding generoso para no cortar inicios/finales
        threshold=0.5,
    )

    # Modelo cacheado a nivel módulo (una sola construcción por proceso) e
    # inferencia bajo el lock propio del VAD: mismo comportamiento de recorte,
    # sin reconstruir sesiones ONNX ni competir con el modo reunión.
    _get_cached_vad_model()
    with _VAD_INFER_LOCK:
        timestamps = get_speech_timestamps(samples, vad_opts, sampling_rate=16000)

    if not timestamps:
        logger.debug("VAD: no se detectó voz — devolviendo None")
        return None

    # Recortar y concatenar segmentos de voz
    speech_chunks, _ = collect_chunks(samples, timestamps, sampling_rate=16000)
    speech_audio = np.concatenate(speech_chunks)

    # --- Reconstruir WAV en BytesIO ---
    out_buf = io.BytesIO()
    pcm_int16 = (speech_audio * 32767).clip(-32768, 32767).astype(np.int16)
    pcm_bytes = struct.pack(f"<{len(pcm_int16)}h", *pcm_int16)

    with wave.open(out_buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm_bytes)

    out_buf.seek(0)
    logger.debug(
        "VAD: audio recortado de %.2fs a %.2fs (%d segmentos de voz)",
        len(samples) / 16000,
        len(speech_audio) / 16000,
        len(timestamps),
    )
    return out_buf
