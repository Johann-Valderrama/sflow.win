import io
import os
import wave
import queue
import logging
import threading
import numpy as np
import sounddevice as sd
from config import SAMPLE_RATE, CHANNELS, AUDIO_DTYPE, BLOCK_SIZE, CHUNK_OVERLAP_SECONDS

logger = logging.getLogger(__name__)


def _resolve_device(name: str) -> int | None:
    """Find input device index by name substring. Returns None to use system default."""
    if not name:
        return None
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and name.lower() in dev["name"].lower():
            return i
    logger.warning("Audio device '%s' not found, using system default.", name)
    return None


class AudioRecorder:
    """Captura audio del micrófono usando sounddevice y lo almacena en memoria."""

    # Centinela para distinguir "aún no resuelto" de "" (dispositivo por defecto válido)
    _UNSET = object()

    def __init__(self):
        """Inicializa la cola de audio, lista de frames y estado de grabación."""
        self.audio_queue = queue.Queue()  # For UI visualization
        self.frames: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self.is_recording = False
        self._chunk_lock = threading.Lock()
        self.samples_captured: int = 0  # Contador monótono de muestras recibidas
        # Caché de resolución de dispositivo: evita llamar sd.query_devices() en cada grabación
        self._cached_device_name: object = AudioRecorder._UNSET
        self._cached_device_index: int | None = None

    def _callback(self, indata: np.ndarray, frames: int, time_info, status):
        """Callback de sounddevice: encola audio para visualización y almacena frames."""
        if status:
            logger.warning("Audio status: %s", status)
        self.audio_queue.put(indata.copy())
        with self._chunk_lock:
            self.frames.append(indata.copy())
            self.samples_captured += indata.shape[0]

    def samples_count(self) -> int:
        """Devuelve el contador monótono de muestras capturadas en la sesión actual.

        Lectura sin lock: el GIL garantiza atomicidad para enteros en CPython.
        Solo es válido entre start() y stop(); se resetea en cada start().
        """
        return self.samples_captured

    def start(self):
        """Inicia la captura de audio abriendo un InputStream de sounddevice."""
        self.samples_captured = 0
        self.frames.clear()
        # Drain any old data from the queue
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        self.is_recording = True
        name = os.getenv("AUDIO_DEVICE_NAME", "")
        if name != self._cached_device_name:
            self._cached_device_index = _resolve_device(name)
            self._cached_device_name = name
        device = self._cached_device_index
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=AUDIO_DTYPE,
            blocksize=BLOCK_SIZE,
            callback=self._callback,
            device=device,
        )
        self.stream.start()

    def stop(self) -> float:
        """Stop recording and return duration in seconds based on captured samples."""
        self.is_recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        # Drenar buffers de visualización no consumidos para liberar memoria
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        with self._chunk_lock:
            if not self.frames:
                return 0.0
            total_samples = sum(f.shape[0] for f in self.frames)
        return total_samples / SAMPLE_RATE

    def extract_chunk(self) -> io.BytesIO | None:
        """Extract accumulated frames as WAV, keep overlap. Thread-safe."""
        with self._chunk_lock:
            if not self.frames:
                return None
            overlap_frames = int(CHUNK_OVERLAP_SECONDS * SAMPLE_RATE / BLOCK_SIZE)
            if overlap_frames >= len(self.frames):
                return None
            chunk_frames = self.frames[:-overlap_frames] if overlap_frames else self.frames[:]
            if not chunk_frames:
                return None
            self.frames = self.frames[-overlap_frames:] if overlap_frames else []

        audio_data = np.concatenate(chunk_frames, axis=0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())
        buf.seek(0)
        return buf

    def get_wav_buffer(self) -> io.BytesIO:
        """Convert remaining frames to in-memory WAV buffer."""
        with self._chunk_lock:
            frames = self.frames
            self.frames = []
        if not frames:
            return io.BytesIO()
        audio_data = np.concatenate(frames, axis=0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())
        buf.seek(0)
        return buf
