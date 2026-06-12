"""Backend de transcripción local basado en faster-whisper.

No requiere conexión a internet: el modelo corre en CPU del equipo.
Limitación: la traducción solo funciona hacia inglés (tarea nativa de Whisper);
para otros idiomas de destino se devuelve la transcripción en idioma original.

Variables de entorno relevantes:
    LOCAL_WHISPER_MODEL       — Tamaño del modelo: "small" (default) o "medium".
    LOCAL_MODEL_IDLE_MINUTES  — Minutos de inactividad antes de liberar el modelo
                                de la RAM. 0 = nunca liberar (default: 10).
"""
import io
import logging
import os
import threading
import time

from core.backends.base import TranscriptionBackend

logger = logging.getLogger(__name__)

# Ruta donde se almacenan los modelos descargados.  En modo bundle los datos
# de usuario van a %APPDATA%\Vflow; en modo dev, a la carpeta del proyecto.
def _get_models_dir() -> str:
    """Devuelve el directorio de modelos según el modo de ejecución."""
    import sys
    from config import APP_DATA_DIR
    if getattr(sys, "frozen", False):
        return os.path.join(APP_DATA_DIR, "models")
    # En dev: carpeta models/ en la raíz del proyecto
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "models")


def _model_dir_name(model_name: str) -> str:
    """Nombre del directorio de Hugging Face para el modelo dado."""
    return f"Systran--faster-whisper-{model_name}"


def _is_model_downloaded(model_name: str) -> bool:
    """Comprueba si el modelo está descargado correctamente en disco.

    Acepta dos estructuras posibles:
    1. Caché de Hugging Face (huggingface_hub): modelos en
       ``<models_dir>/models--Systran--faster-whisper-<name>/snapshots/<hash>/``.
    2. Directorio plano (descarga manual): archivos en
       ``<models_dir>/Systran--faster-whisper-<name>/``.

    En ambos casos exige la presencia de los cuatro archivos esenciales del
    repositorio Systran/faster-whisper y rechaza si hay archivos ``*.incomplete``
    en el directorio de blobs (descarga a medias).
    """
    # Archivos que faster-whisper requiere en el directorio del snapshot.
    _REQUIRED_FILES = {"model.bin", "config.json", "tokenizer.json", "vocabulary.txt"}

    models_dir = _get_models_dir()

    # --- Estructura 1: caché HuggingFace (huggingface_hub) ---
    hf_cache_dir = os.path.join(models_dir, f"models--{_model_dir_name(model_name)}")
    if os.path.isdir(hf_cache_dir):
        # Rechazar si hay descargas incompletas en blobs/
        blobs_dir = os.path.join(hf_cache_dir, "blobs")
        if os.path.isdir(blobs_dir):
            for fname in os.listdir(blobs_dir):
                if fname.endswith(".incomplete"):
                    return False

        snapshots_dir = os.path.join(hf_cache_dir, "snapshots")
        if os.path.isdir(snapshots_dir):
            for snap_hash in os.listdir(snapshots_dir):
                snap_path = os.path.join(snapshots_dir, snap_hash)
                if os.path.isdir(snap_path):
                    present = set(os.listdir(snap_path))
                    if _REQUIRED_FILES.issubset(present):
                        return True

    # --- Estructura 2: directorio plano ---
    flat_dir = os.path.join(models_dir, _model_dir_name(model_name))
    if os.path.isdir(flat_dir):
        present = set(os.listdir(flat_dir))
        if _REQUIRED_FILES.issubset(present):
            return True

    return False


class LocalBackend(TranscriptionBackend):
    """Backend que transcribe localmente con faster-whisper (sin internet).

    El modelo se carga de forma lazy (en el primer uso) y se libera
    automáticamente tras un período de inactividad configurable.

    Seguridad ante carreras (release-durante-inferencia):
    - ``_inflight`` cuenta los trabajos de transcripción activos.
    - ``_release_pending`` marca que release() fue solicitado mientras había
      trabajos en curso.  El último trabajo en terminar ejecuta la liberación.
    - El timer de inactividad también usa este mecanismo vía ``_release_pending``.
    - Todos los accesos a estos campos están protegidos por ``_lock``.
    """

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()
        self._idle_timer: threading.Timer | None = None
        self._inflight: int = 0            # trabajos de inferencia en curso
        self._release_pending: bool = False  # release() solicitado con _inflight > 0
        # Nombre del modelo leído en el constructor; puede cambiar en el entorno.
        self._model_name: str = os.getenv("LOCAL_WHISPER_MODEL", "small").strip().lower()
        self._idle_minutes: int = int(os.getenv("LOCAL_MODEL_IDLE_MINUTES", "10") or "10")

    # ------------------------------------------------------------------
    # Interfaz pública de TranscriptionBackend
    # ------------------------------------------------------------------

    def get_model_name(self) -> str:
        return f"faster-whisper-{self._model_name}"

    def is_ready(self) -> bool:
        """Devuelve True si el modelo está descargado en disco (sin cargarlo)."""
        return _is_model_downloaded(self._model_name)

    def warmup(self) -> None:
        """Carga el modelo y ejecuta una inferencia dummy de silencio (~0.5s).

        Paga el lazy-alloc de CTranslate2 para que la primera transcripción
        real no tenga latencia extra.  Seguro llamar desde un thread de fondo.
        """
        if not self.is_ready():
            logger.warning(
                "LocalBackend.warmup(): modelo '%s' no descargado — omitiendo warmup",
                self._model_name,
            )
            return
        try:
            model = self._load_model()
            import numpy as np
            dummy = np.zeros(int(0.5 * 16000), dtype=np.float32)
            dummy_buf = io.BytesIO()
            import wave, struct
            with wave.open(dummy_buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                pcm = struct.pack(f"<{len(dummy)}h", *[int(s * 32767) for s in dummy])
                wf.writeframes(pcm)
            dummy_buf.seek(0)
            list(model.transcribe(dummy_buf, language="es", vad_filter=True)[0])
            logger.info("LocalBackend: warmup completado para modelo '%s'", self._model_name)
        except Exception as e:
            logger.warning("LocalBackend.warmup(): error durante warmup — %s", e)

    def release(self) -> None:
        """Libera el modelo de la RAM.

        Si hay trabajos de inferencia en curso (``_inflight > 0``), marca
        ``_release_pending`` y pospone la liberación al último trabajo que
        termine.  Esto evita el segfault por ``del self._model`` durante
        inferencia nativa de CTranslate2.
        """
        with self._lock:
            self._cancel_idle_timer()
            if self._inflight > 0:
                self._release_pending = True
                logger.info(
                    "LocalBackend: release() diferido — %d trabajo(s) en curso",
                    self._inflight,
                )
                return
            self._do_release_locked()

    def transcribe(
        self,
        wav_buffer: io.BytesIO,
        language: str,
        prompt: str | None = None,
    ) -> str:
        """Transcribe audio WAV con el modelo local.

        Args:
            wav_buffer: Datos de audio en formato WAV.
            language:   Código ISO del idioma fuente; ``"auto"`` para
                        detección automática (language=None en Whisper).
            prompt:     Contexto opcional del chunk anterior.

        Returns:
            Texto transcrito.  Cadena vacía si no hay habla detectada.

        Raises:
            RuntimeError: Si el modelo no está descargado en disco.
        """
        self._require_model_downloaded()
        model = self._load_model()
        self._enter_inflight()
        try:
            wav_buffer.seek(0)
            lang = None if (not language or language == "auto") else language
            kwargs = dict(
                language=lang,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            if prompt:
                kwargs["initial_prompt"] = prompt

            segments, _ = model.transcribe(wav_buffer, **kwargs)
            text = " ".join(seg.text for seg in segments).strip()
            return text
        finally:
            self._exit_inflight()

    def translate(self, wav_buffer: io.BytesIO, target_lang: str = "en", prompt: str | None = None) -> str:
        """Traduce el audio al idioma destino.

        Solo admite traducción a inglés (tarea nativa de Whisper).  Si
        ``target_lang`` es distinto de ``"en"``, loggea un aviso y devuelve
        la transcripción en el idioma original SIN llamar a ningún servicio
        externo.

        Args:
            wav_buffer: Datos de audio en formato WAV.
            target_lang: Código ISO del idioma destino.

        Returns:
            Texto traducido (si target_lang == "en") o transcrito.

        Raises:
            RuntimeError: Si el modelo no está descargado en disco.
        """
        self._require_model_downloaded()
        model = self._load_model()
        self._enter_inflight()
        try:
            wav_buffer.seek(0)
            if target_lang != "en":
                logger.warning(
                    "LocalBackend: traducción a '%s' no soportada localmente — "
                    "devolviendo transcripción en idioma original. "
                    "Usa Groq para traducir a idiomas distintos del inglés.",
                    target_lang,
                )
                # Transcribir en idioma original
                lang = os.getenv("WHISPER_LANGUAGE", "es")
                segments, _ = model.transcribe(
                    wav_buffer,
                    language=lang,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                )
                return " ".join(seg.text for seg in segments).strip()

            # Traducción nativa Whisper → inglés
            segments, _ = model.transcribe(
                wav_buffer,
                task="translate",
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            return " ".join(seg.text for seg in segments).strip()
        finally:
            self._exit_inflight()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_model_downloaded(self) -> None:
        """Lanza RuntimeError si el modelo no está descargado."""
        if not self.is_ready():
            raise RuntimeError(
                f"Modelo local '{self._model_name}' no descargado. "
                "Descárgalo desde el dashboard de Vflow (Configuración → Modelo local)."
            )

    def _load_model(self):
        """Carga el modelo faster-whisper en memoria (lazy, thread-safe).

        Usa import diferido para que la app arranque aunque faster-whisper
        no esté instalado.
        """
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from faster_whisper import WhisperModel  # noqa: PLC0415
                    except ImportError as exc:
                        raise RuntimeError(
                            "faster-whisper no está instalado. "
                            "Ejecuta: pip install faster-whisper==1.1.1"
                        ) from exc

                    models_dir = _get_models_dir()
                    os.makedirs(models_dir, exist_ok=True)

                    # Limitar hilos de CPU para no saturar el equipo
                    cpu_threads = max(4, (os.cpu_count() or 4) // 2)
                    logger.info(
                        "LocalBackend: cargando modelo '%s' desde '%s' "
                        "(device=cpu, compute_type=int8, cpu_threads=%d)",
                        self._model_name,
                        models_dir,
                        cpu_threads,
                    )
                    # local_files_only=True garantiza que faster-whisper/
                    # huggingface_hub NO contacte huggingface.co para verificar
                    # revisiones del modelo.  El modelo ya está descargado
                    # (comprobado por is_ready() antes de llegar aquí), así que
                    # no se necesita acceso a la red.  La descarga explícita
                    # desde el dashboard es el único punto donde se permite
                    # tráfico de red.
                    self._model = WhisperModel(
                        self._model_name,
                        device="cpu",
                        compute_type="int8",
                        download_root=models_dir,
                        cpu_threads=cpu_threads,
                        local_files_only=True,
                    )
                    logger.info("LocalBackend: modelo cargado correctamente")
        return self._model

    # ------------------------------------------------------------------
    # Gestión del contador de trabajos en vuelo (anti-carrera)
    # ------------------------------------------------------------------

    def _enter_inflight(self) -> None:
        """Incrementa el contador de trabajos activos (protegido por lock)."""
        with self._lock:
            self._inflight += 1

    def _exit_inflight(self) -> None:
        """Decrementa el contador; si llega a 0 y hay un release pendiente,
        ejecuta la liberación y NO reinicia el timer de inactividad."""
        with self._lock:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                if self._release_pending:
                    self._release_pending = False
                    self._do_release_locked()
                    return
        # Solo reiniciar el timer si el modelo sigue vivo
        self._reset_idle_timer()

    def _do_release_locked(self) -> None:
        """Libera el modelo; debe llamarse con ``_lock`` adquirido."""
        self._cancel_idle_timer()
        if self._model is not None:
            del self._model
            self._model = None
            logger.info("LocalBackend: modelo '%s' liberado de memoria", self._model_name)

    # ------------------------------------------------------------------
    # Timer de inactividad
    # ------------------------------------------------------------------

    def _reset_idle_timer(self) -> None:
        """Reinicia el temporizador de liberación por inactividad."""
        if self._idle_minutes <= 0:
            return
        with self._lock:
            self._cancel_idle_timer()
            seconds = self._idle_minutes * 60
            self._idle_timer = threading.Timer(seconds, self._on_idle_timeout)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        """Cancela el temporizador de inactividad (debe llamarse con el lock)."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _on_idle_timeout(self) -> None:
        """Callback del temporizador: solicita liberación por inactividad.

        Si hay trabajos en curso, marca ``_release_pending`` para diferir la
        liberación en lugar de destruir el modelo nativo durante inferencia.
        """
        logger.info(
            "LocalBackend: %d minutos sin uso — solicitando liberación de '%s'",
            self._idle_minutes,
            self._model_name,
        )
        with self._lock:
            self._idle_timer = None
            if self._inflight > 0:
                self._release_pending = True
                logger.info(
                    "LocalBackend: liberación diferida — %d trabajo(s) activos",
                    self._inflight,
                )
                return
            self._do_release_locked()
