"""Backend de transcripción local basado en faster-whisper.

No requiere conexión a internet. Corre en GPU (CUDA) cuando está disponible y
carga bien, o en CPU si no (ver LOCAL_DEVICE abajo; Ola 7 de PLAN-DICTADO,
docs/benchmarks/local-backend-gpu-2026-08-01.md). Limitación: la traducción
solo funciona hacia inglés (tarea nativa de Whisper); para otros idiomas de
destino se devuelve la transcripción en idioma original.

Variables de entorno relevantes:
    LOCAL_WHISPER_MODEL:       Tamaño del modelo: "small" (default) o "medium".
    LOCAL_MODEL_IDLE_MINUTES:  Minutos de inactividad antes de liberar el modelo
                                de la RAM. 0 = nunca liberar (default: 10).
    LOCAL_DEVICE:              "auto" (default), "cpu" o "cuda". "auto" usa CUDA
                                si está disponible y carga bien, si no cae a CPU.
                                Un fallo de CUDA al cargar SIEMPRE cae a CPU, incluso
                                pedido explícito ("cuda"): un fallo de GPU (driver,
                                VRAM ocupada, DLL de cuBLAS ausente del PATH) no puede
                                dejar al usuario sin dictado. Ver _ensure_cuda_on_path().
"""
import glob
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


def _requested_device() -> str:
    """Lee ``LOCAL_DEVICE`` y lo normaliza a ``"auto"`` | ``"cpu"`` | ``"cuda"``.

    Un valor no reconocido se trata como ``"auto"``: esta variable NO es un
    control de seguridad (al revés de ``DASHBOARD_AUTH_ENABLED``), así que
    fallar abierto aquí es lo correcto: un typo en el ``.env`` no debe dejar
    al usuario sin dictado local.
    """
    raw = os.getenv("LOCAL_DEVICE", "auto").strip().lower()
    return raw if raw in ("auto", "cpu", "cuda") else "auto"


def _ensure_cuda_on_path(*, search_bases: list[str] | None = None) -> None:
    """Blindaje contra PATH viejo (gotcha ya pagado en la skill OPS
    ``transcribir-video``, ``C:\\OPS\\skills-on-demand\\transcribir-video\\assets\\
    transcribir_video.py``, función homónima): CUDA 12.x puede estar instalado
    y con ``cublas64_12.dll`` en disco, pero el proceso actual haber heredado
    un PATH sin esa carpeta, típico cuando Vflow.exe se lanza desde la
    bandeja de Windows o el arranque del sistema, en vez de una terminal que
    ya tenía CUDA en PATH.

    Busca la carpeta con ``cublas64_12.dll`` bajo el Toolkit de NVIDIA y la
    antepone a ``os.environ["PATH"]`` si hace falta. Nunca falla: si no
    encuentra nada, el intento de carga en CUDA de ``_load_model()`` fallará
    más abajo y el fallback a CPU se hace cargo.

    No hardcodea la versión MENOR del Toolkit (hoy v12.9 en la máquina de
    Johann, ver el banco de la unidad 7a): busca por patrón ``v12.*\\bin``,
    porque un hardcode que hay que actualizar a mano cada vez que NVIDIA
    publica una versión nueva es construir lo temporal en vez de la regla
    durable. El "12" del nombre del DLL SÍ es fijo a propósito: es la versión
    MAYOR que ctranslate2/faster-whisper requieren hoy (el 13.3 instalado en
    esta máquina no lo usan), no un detalle de esta corrida.

    ``search_bases`` es un seam de testabilidad (no lo usa producción, que
    siempre pasa ``None`` y usa las rutas reales del Toolkit): permite a los
    tests apuntar a un directorio temporal en vez de depender de si ESTA
    máquina tiene CUDA instalado en el Program Files real.

    Todo el cuerpo va envuelto en ``try/except`` (hallazgo del verificador
    independiente de la unidad 7b): la garantía de "nunca falla" no debía
    descansar en que ``os.path.exists``/``glob.glob`` de la stdlib jamás
    lancen (permisos del sistema de archivos, ruta con caracteres raros,
    etc.), sino en el propio código. Si algo inesperado ocurre aquí, se
    loguea y se sigue como si no se hubiera encontrado nada: el intento de
    carga en CUDA de ``_load_model()`` fallará más abajo y el fallback a
    CPU se hace cargo igual.
    """
    try:
        already = any(
            os.path.exists(os.path.join(d, "cublas64_12.dll"))
            for d in os.environ.get("PATH", "").split(os.pathsep) if d
        )
        if already:
            return
        bases = search_bases if search_bases is not None else [
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA",
            os.path.expandvars(r"%ProgramFiles%\NVIDIA GPU Computing Toolkit\CUDA"),
        ]
        for base in bases:
            for bin_dir in sorted(glob.glob(os.path.join(base, "v12.*", "bin"))):
                if os.path.exists(os.path.join(bin_dir, "cublas64_12.dll")):
                    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
                    return
    except Exception as exc:
        logger.warning(
            "LocalBackend: _ensure_cuda_on_path() fallo inesperado (%s), "
            "se continua sin blindar el PATH.", exc,
        )


def _import_whisper_model():
    """Import diferido de ``WhisperModel`` (permite que la app arranque aunque
    faster-whisper no esté instalado). Aislado en su propia función para que
    los tests puedan monkeypatchear la construcción del modelo sin necesitar
    GPU real ni el paquete faster-whisper instalado."""
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper no está instalado. "
            "Ejecuta: pip install faster-whisper==1.1.1"
        ) from exc
    return WhisperModel


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
        # Dispositivo REAL con el que se cargó el modelo ("cpu"/"cuda"), distinto
        # de LOCAL_DEVICE (lo pedido): None hasta la primera carga. Expuesto vía
        # get_device() para diagnóstico/tests.
        self._device_used: str | None = None
        # Breaker de fallos de INFERENCIA en CUDA (distinto del fallback de CARGA
        # de _load_model()): time.monotonic() del último fallo de
        # model.transcribe() en CUDA, o None si nunca falló / ya se reseteó.
        # Mismo patrón que Transcriber._net_breaker_failed_at en core/transcriber.py.
        self._cuda_breaker_tripped_at: float | None = None

    # ------------------------------------------------------------------
    # Interfaz pública de TranscriptionBackend
    # ------------------------------------------------------------------

    def get_model_name(self) -> str:
        return f"faster-whisper-{self._model_name}"

    def get_device(self) -> str | None:
        """Dispositivo con el que se cargó el modelo actualmente en memoria
        ("cpu" o "cuda"); ``None`` si el modelo todavía no se ha cargado."""
        return self._device_used

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
        self._load_model()
        self._enter_inflight()
        try:
            lang = None if (not language or language == "auto") else language
            kwargs = dict(
                language=lang,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            if prompt:
                kwargs["initial_prompt"] = prompt

            def _do(model):
                # seek(0) en CADA intento: el reintento de _run_inference tras
                # un fallo de inferencia en CUDA reusa el MISMO wav_buffer, y
                # el primer intento pudo haber avanzado el cursor al iterar
                # los segmentos antes de fallar.
                wav_buffer.seek(0)
                segments, _ = model.transcribe(wav_buffer, **kwargs)
                return " ".join(seg.text for seg in segments).strip()

            return self._run_inference(_do)
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
        self._load_model()
        self._enter_inflight()
        try:
            if target_lang != "en":
                logger.warning(
                    "LocalBackend: traducción a '%s' no soportada localmente — "
                    "devolviendo transcripción en idioma original. "
                    "Usa Groq para traducir a idiomas distintos del inglés.",
                    target_lang,
                )
                # Transcribir en idioma original
                lang = os.getenv("WHISPER_LANGUAGE", "es")

                def _do(model):
                    wav_buffer.seek(0)  # ver nota de seek en transcribe()
                    segments, _ = model.transcribe(
                        wav_buffer,
                        language=lang,
                        vad_filter=True,
                        vad_parameters={"min_silence_duration_ms": 500},
                    )
                    return " ".join(seg.text for seg in segments).strip()

                return self._run_inference(_do)

            # Traducción nativa Whisper → inglés
            def _do_translate(model):
                wav_buffer.seek(0)  # ver nota de seek en transcribe()
                segments, _ = model.transcribe(
                    wav_buffer,
                    task="translate",
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                )
                return " ".join(seg.text for seg in segments).strip()

            return self._run_inference(_do_translate)
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

        Resuelve ``LOCAL_DEVICE`` (Ola 7 de PLAN-DICTADO): con "auto" o "cuda"
        intenta CUDA primero; si la construcción del modelo falla por
        cualquier motivo (driver, VRAM ocupada, DLL de cuBLAS ausente del
        PATH), cae a CPU SIEMPRE, incluso si el usuario pidió "cuda"
        explícito, porque un fallo de GPU no puede dejar al usuario sin
        dictado (fail-open deliberado, ver docstring del módulo). Con "cpu"
        nunca se intenta CUDA. Con el breaker de CUDA ABIERTO (fallo de
        INFERENCIA reciente, ver ``_run_inference``) tampoco se intenta,
        aunque el device pedido sea "auto" o "cuda": reintentar CUDA de cero
        en cada carga tras un problema persistente (VRAM ocupada, driver con
        TDR) es pagar el mismo fallo una y otra vez.

        ``compute_type`` va acoplado al device resuelto, no es una variable
        de entorno aparte: "int8" en los dos casos (recomendación medida en
        docs/benchmarks/local-backend-gpu-2026-08-01.md, en esta GPU no hay
        una segunda opción de compute_type que valga la pena exponer).
        ``cpu_threads`` solo aplica al camino CPU.
        """
        if self._model is None:
            with self._lock:
                if self._model is None:
                    WhisperModel = _import_whisper_model()

                    models_dir = _get_models_dir()
                    os.makedirs(models_dir, exist_ok=True)

                    # Limitar hilos de CPU para no saturar el equipo (solo aplica
                    # si termina cargando en CPU, sea por LOCAL_DEVICE=cpu o por
                    # fallback tras un fallo de CUDA).
                    cpu_threads = max(4, (os.cpu_count() or 4) // 2)
                    requested = _requested_device()

                    # local_files_only=True garantiza que faster-whisper/
                    # huggingface_hub NO contacte huggingface.co para verificar
                    # revisiones del modelo.  El modelo ya está descargado
                    # (comprobado por is_ready() antes de llegar aquí), así que
                    # no se necesita acceso a la red.  La descarga explícita
                    # desde el dashboard es el único punto donde se permite
                    # tráfico de red.
                    model = None
                    device_used = None

                    if requested in ("auto", "cuda") and self._cuda_breaker_open():
                        logger.info(
                            "LocalBackend: breaker de CUDA abierto (fallo de "
                            "inferencia reciente, cooldown %.0fs) - cargando "
                            "directo en CPU sin reintentar CUDA.",
                            self._cuda_retry_cooldown(),
                        )
                    elif requested in ("auto", "cuda"):
                        _ensure_cuda_on_path()
                        try:
                            logger.info(
                                "LocalBackend: intentando cargar modelo '%s' en CUDA "
                                "(compute_type=int8, LOCAL_DEVICE=%s)",
                                self._model_name, requested,
                            )
                            model = WhisperModel(
                                self._model_name,
                                device="cuda",
                                compute_type="int8",
                                download_root=models_dir,
                                local_files_only=True,
                            )
                            device_used = "cuda"
                        except Exception as exc:
                            if requested == "cuda":
                                logger.warning(
                                    "LocalBackend: LOCAL_DEVICE=cuda pedido "
                                    "explícitamente pero CUDA falló al cargar (%s), "
                                    "cayendo a CPU. Un fallo de GPU no puede dejar "
                                    "al usuario sin dictado.", exc,
                                )
                            else:
                                logger.warning(
                                    "LocalBackend: CUDA no disponible o falló al "
                                    "cargar (%s), usando CPU.", exc,
                                )
                            model = None

                    if model is None:
                        logger.info(
                            "LocalBackend: cargando modelo '%s' desde '%s' "
                            "(device=cpu, compute_type=int8, cpu_threads=%d)",
                            self._model_name, models_dir, cpu_threads,
                        )
                        model = self._build_cpu_model(WhisperModel, models_dir, cpu_threads)
                        device_used = "cpu"

                    self._model = model
                    self._device_used = device_used
                    logger.info(
                        "LocalBackend: modelo cargado correctamente (device=%s)",
                        device_used,
                    )
        return self._model

    def _build_cpu_model(self, WhisperModel, models_dir: str, cpu_threads: int):
        """Construye un ``WhisperModel`` en CPU con la configuración de
        producción. Extraído de ``_load_model()`` para que ``_run_inference``
        (reintento tras un fallo de INFERENCIA en CUDA) use exactamente la
        misma configuración sin duplicar los kwargs en dos sitios."""
        return WhisperModel(
            self._model_name,
            device="cpu",
            compute_type="int8",
            download_root=models_dir,
            cpu_threads=cpu_threads,
            local_files_only=True,
        )

    def _build_ephemeral_cpu_model(self):
        """Construye un modelo CPU de UN SOLO USO para el reintento inmediato
        de ``_run_inference`` tras un fallo de inferencia en CUDA.

        No toca ``self._model``: el modelo CUDA roto compartido se libera
        por separado vía ``release()`` (que respeta ``_inflight`` y nunca
        destruye un modelo mientras otro hilo lo esté usando), y esta
        instancia efímera solo sirve para no perder el audio que el usuario
        ya grabó mientras esa liberación ocurre en su momento seguro."""
        WhisperModel = _import_whisper_model()
        models_dir = _get_models_dir()
        cpu_threads = max(4, (os.cpu_count() or 4) // 2)
        logger.info(
            "LocalBackend: cargando modelo CPU efímero '%s' para el reintento "
            "tras fallo de inferencia en CUDA (compute_type=int8, cpu_threads=%d)",
            self._model_name, cpu_threads,
        )
        return self._build_cpu_model(WhisperModel, models_dir, cpu_threads)

    # ------------------------------------------------------------------
    # Breaker de fallos de INFERENCIA en CUDA (distinto del fallback de CARGA
    # de _load_model(): este cubre un modelo que cargó bien y luego falla al
    # transcribir, típicamente por VRAM ocupada por otra app o un reset de
    # driver a mitad de sesión). Mismo patrón que
    # Transcriber._net_breaker_* en core/transcriber.py.
    # ------------------------------------------------------------------

    @staticmethod
    def _cuda_retry_cooldown() -> float:
        """Segundos que el breaker evita reintentar CUDA tras un fallo de
        INFERENCIA (``LOCAL_CUDA_FALLBACK_COOLDOWN``, default 300, lazy,
        mismo patrón que ``TRANSCRIPTION_FALLBACK_COOLDOWN``/
        ``INSIGHTS_FALLBACK_COOLDOWN`` en core/transcriber.py e insights.py)."""
        try:
            return float(os.getenv("LOCAL_CUDA_FALLBACK_COOLDOWN", "300") or 300)
        except ValueError:
            return 300.0

    def _cuda_breaker_open(self) -> bool:
        """¿El breaker sigue abierto (un fallo de inferencia en CUDA ocurrió
        hace menos del cooldown)?"""
        if self._cuda_breaker_tripped_at is None:
            return False
        return (time.monotonic() - self._cuda_breaker_tripped_at) < self._cuda_retry_cooldown()

    def _cuda_breaker_trip(self) -> None:
        """Abre el breaker: registra el timestamp del fallo de inferencia."""
        self._cuda_breaker_tripped_at = time.monotonic()

    def _cuda_breaker_reset(self) -> None:
        """Cierra el breaker (una inferencia en CUDA volvió a tener éxito)."""
        if self._cuda_breaker_tripped_at is not None:
            logger.info("LocalBackend: inferencia en CUDA exitosa, breaker reseteado")
        self._cuda_breaker_tripped_at = None

    def _run_inference(self, call_fn):
        """Ejecuta ``call_fn(model)`` sobre ``self._model`` (ya resuelto por
        ``_load_model()``). Si el device en uso es CUDA y la llamada de
        INFERENCIA falla (a diferencia de un fallo de CARGA, que ya maneja
        ``_load_model()``/el bloque try/except de arriba), libera el modelo
        roto compartido de forma SEGURA (``release()``, que respeta
        ``_inflight`` y difiere la destrucción si otro hilo sigue usándolo) y
        reintenta la MISMA llamada con un modelo CPU efímero, para no perder
        el audio que el usuario ya grabó: tardar el doble es mejor que
        perder el dictado. Si el device en uso YA era CPU, o el reintento en
        CPU TAMBIÉN falla, la excepción se propaga tal cual: es un fallo
        genuino y no hay una tercera vía.

        Un éxito de inferencia en CUDA resetea el breaker (mismo criterio que
        ``Transcriber._run_net_fallback`` en core/transcriber.py: el breaker
        se cierra cuando el camino primario vuelve a funcionar).
        """
        try:
            result = call_fn(self._model)
            if self._device_used == "cuda":
                self._cuda_breaker_reset()
            return result
        except Exception as exc:
            if self._device_used != "cuda":
                raise  # CPU ya era el camino: fallo genuino, sin cambio de comportamiento
            logger.warning(
                "LocalBackend: fallo de INFERENCIA en CUDA (%s), liberando el "
                "modelo roto y reintentando en CPU para no perder el audio ya "
                "grabado.", exc,
            )
            self._cuda_breaker_trip()
            self.release()  # deferred-safe: no interrumpe otros trabajos en curso
            cpu_model = self._build_ephemeral_cpu_model()
            return call_fn(cpu_model)  # si esto también falla, propaga: fallo genuino

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
            self._device_used = None
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
