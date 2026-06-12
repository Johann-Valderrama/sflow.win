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


# ---------------------------------------------------------------------------
# Abstracción AudioSource
# ---------------------------------------------------------------------------

class AudioSource:
    """Interfaz mínima para fuentes de audio.  Dos implementaciones: MicSource y LoopbackSource."""

    def start(self, callback):
        """Inicia la captura y llama a callback(chunk: np.ndarray) con audio 16 kHz mono int16."""
        raise NotImplementedError

    def stop(self):
        """Detiene la captura y libera recursos."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# MicSource — comportamiento original con sounddevice
# ---------------------------------------------------------------------------

def _resolve_device(name: str) -> int | None:
    """Busca el índice del dispositivo de entrada por subcadena de nombre."""
    if not name:
        return None
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and name.lower() in dev["name"].lower():
            return i
    logger.warning("Audio device '%s' not found, using system default.", name)
    return None


class MicSource(AudioSource):
    """Captura micrófono usando sounddevice (comportamiento original sin cambios)."""

    # Centinela para distinguir "aún no resuelto" de "" (dispositivo por defecto válido)
    _UNSET = object()

    def __init__(self):
        self._stream: sd.InputStream | None = None
        # Caché de resolución de dispositivo: evita llamar sd.query_devices() en cada grabación
        self._cached_device_name: object = MicSource._UNSET
        self._cached_device_index: int | None = None

    def start(self, callback):
        name = os.getenv("AUDIO_DEVICE_NAME", "")
        if name != self._cached_device_name:
            self._cached_device_index = _resolve_device(name)
            self._cached_device_name = name

        def _sd_callback(indata: np.ndarray, frames: int, time_info, status):
            if status:
                logger.warning("Audio status: %s", status)
            callback(indata.copy())

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=AUDIO_DTYPE,
            blocksize=BLOCK_SIZE,
            callback=_sd_callback,
            device=self._cached_device_index,
        )
        self._stream.start()

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


# ---------------------------------------------------------------------------
# LoopbackSource — audio del sistema vía WASAPI loopback (pyaudiowpatch)
# ---------------------------------------------------------------------------

# Frecuencia nativa del dispositivo loopback (estéreo 48 kHz en esta máquina).
# La decimación 48000 → 16000 es ratio 1/3 exacto: seguro con resample_poly
# siempre que frames_per_buffer sea múltiplo de 3.
_LOOPBACK_NATIVE_RATE = 48000
_LOOPBACK_FRAMES = 1026  # múltiplo de 3 → decimación /3 exacta sin acumulador

class LoopbackSource(AudioSource):
    """Captura audio del sistema usando WASAPI loopback (pyaudiowpatch).

    Entrega chunks 16 kHz mono int16 al callback, igual que MicSource, de modo que
    el resto del pipeline (chunking, visualizador, VAD, transcripción) no necesita cambios.
    """

    def __init__(self):
        self._pa = None
        self._stream = None
        self._callback = None

    def start(self, callback):
        # Importación lazy: pyaudiowpatch solo se carga al usar LoopbackSource
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as e:
            raise RuntimeError(
                "pyaudiowpatch no está instalado. Instálalo con: pip install pyaudiowpatch"
            ) from e

        pa = pyaudio.PyAudio()
        self._pa = pa

        # Localizar el dispositivo loopback asociado a la salida por defecto
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])

        loopback = None
        if default_out.get("isLoopbackDevice"):
            loopback = default_out
        else:
            for dev in pa.get_loopback_device_info_generator():
                if default_out["name"] in dev["name"]:
                    loopback = dev
                    break

        if loopback is None:
            pa.terminate()
            self._pa = None
            raise RuntimeError(
                "No se encontró dispositivo loopback WASAPI para la salida por defecto. "
                "Asegúrate de tener habilitado un dispositivo de reproducción activo."
            )

        native_rate = int(loopback["defaultSampleRate"])
        channels = int(loopback["maxInputChannels"])
        logger.info(
            "LoopbackSource: dispositivo=[%d] %s, %d ch, %d Hz",
            loopback["index"], loopback["name"], channels, native_rate,
        )

        # Calcular ratio de decimación: native_rate → SAMPLE_RATE
        from math import gcd as _gcd
        _g = _gcd(native_rate, SAMPLE_RATE)
        _up = SAMPLE_RATE // _g    # ej. 16000/16000 = 1
        _down = native_rate // _g  # ej. 48000/16000 = 3

        # Si scipy está disponible usamos resample_poly (mejor calidad anti-aliasing).
        # Si no, para decimación exacta por entero (caso común: 48000→16000 = /3)
        # agrupamos muestras y promediamos — equivalente a un FIR caja de ancho=down.
        try:
            from scipy.signal import resample_poly as _resample_poly
            _has_scipy = True
        except ImportError:
            _has_scipy = False

        def _loopback_callback(in_data, frame_count, time_info, status):
            """Procesa un bloque de audio nativo: downmix a mono + resample a 16 kHz."""
            try:
                chunk = np.frombuffer(in_data, dtype=np.int16)
                if channels > 1:
                    # Reshape a (muestras, canales) y promediar → mono float
                    chunk = chunk.reshape(-1, channels).mean(axis=1)

                # Resample
                if _up != _down:
                    if _has_scipy:
                        resampled = _resample_poly(chunk.astype(np.float64), _up, _down)
                        chunk = np.clip(resampled, -32768, 32767).astype(np.int16)
                    else:
                        # Decimación por entero mediante promedio de grupos.
                        # Solo funciona cuando down es múltiplo exacto; para el caso
                        # estándar 48000→16000 (_down=3) es perfectamente correcto.
                        f = chunk.astype(np.float32)
                        # Recortar al múltiplo de _down más cercano
                        trim = (len(f) // _down) * _down
                        f = f[:trim]
                        if _up == 1:
                            chunk = f.reshape(-1, _down).mean(axis=1).astype(np.int16)
                        else:
                            # Upsample primero (repetición), luego downmix
                            chunk = np.repeat(f, _up).reshape(-1, _down).mean(axis=1).astype(np.int16)
                else:
                    chunk = chunk.astype(np.int16)

                # Asegurar shape (N, 1) como espera el pipeline (igual que sounddevice)
                callback(chunk.reshape(-1, 1))
            except Exception as exc:
                logger.error("LoopbackSource callback error: %s", exc)
            return (None, pyaudio.paContinue)

        self._callback = _loopback_callback
        self._stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=native_rate,
            input=True,
            input_device_index=loopback["index"],
            frames_per_buffer=_LOOPBACK_FRAMES,
            stream_callback=self._callback,
        )
        self._stream.start_stream()

    def stop(self):
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception as exc:
                logger.warning("LoopbackSource.stop: error al cerrar stream: %s", exc)
            self._stream = None
        if self._pa:
            try:
                self._pa.terminate()
            except Exception as exc:
                logger.warning("LoopbackSource.stop: error al terminar PyAudio: %s", exc)
            self._pa = None


# ---------------------------------------------------------------------------
# AudioRecorder — orquestador principal; acepta source="mic" | "system"
# ---------------------------------------------------------------------------

class AudioRecorder:
    """Captura audio desde micrófono o sistema y lo almacena en memoria.

    El parámetro *source* define la fuente:
      - "mic"    → MicSource (sounddevice, comportamiento original)
      - "system" → LoopbackSource (WASAPI loopback via pyaudiowpatch)

    La selección de fuente puede cambiarse entre grabaciones pero no durante una.
    El watchdog de micrófono se desactiva automáticamente en modo "system" (el loopback
    puede no entregar buffers en silencio total sin que sea un error).
    """

    def __init__(self, source: str = "mic"):
        self.audio_queue = queue.Queue()  # Para visualización en UI
        self.frames: list[np.ndarray] = []
        self.is_recording = False
        self._chunk_lock = threading.Lock()
        self.samples_captured: int = 0  # Contador monótono de muestras recibidas
        self.source: str = source  # "mic" | "system"
        self._active_source: AudioSource | None = None

        # Caché de la instancia MicSource para preservar la caché de dispositivo
        self._mic_source: MicSource = MicSource()

    def _make_source(self) -> AudioSource:
        """Crea (o reutiliza) la instancia de AudioSource según self.source."""
        if self.source == "system":
            # Nueva instancia por grabación: PyAudio abre/cierra recursos cada vez
            return LoopbackSource()
        else:
            return self._mic_source

    def _callback(self, chunk: np.ndarray):
        """Callback unificado: encola para visualización y almacena frames."""
        self.audio_queue.put(chunk)
        with self._chunk_lock:
            self.frames.append(chunk)
            self.samples_captured += chunk.shape[0]

    def samples_count(self) -> int:
        """Devuelve el contador monótono de muestras capturadas en la sesión actual.

        Lectura sin lock: el GIL garantiza atomicidad para enteros en CPython.
        Solo es válido entre start() y stop(); se resetea en cada start().
        """
        return self.samples_captured

    @property
    def watchdog_enabled(self) -> bool:
        """El watchdog de silencio solo aplica al micrófono, no al loopback del sistema."""
        return self.source != "system"

    def start(self):
        """Inicia la captura de audio con la fuente configurada.

        La fuente se relee del entorno en cada grabación para que los cambios
        hechos desde el tray o el dashboard apliquen sin reiniciar la app.
        """
        self.source = os.getenv("AUDIO_SOURCE", self.source)
        self.samples_captured = 0
        self.frames.clear()
        # Drenar datos viejos de la cola de visualización
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        self.is_recording = True
        self._active_source = self._make_source()
        self._active_source.start(self._callback)

    def stop(self) -> float:
        """Detiene la grabación y retorna la duración en segundos."""
        self.is_recording = False
        if self._active_source:
            self._active_source.stop()
            self._active_source = None
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
        """Extrae frames acumulados como WAV con overlap. Thread-safe."""
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
        """Convierte los frames restantes a WAV en memoria."""
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
