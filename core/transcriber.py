"""Transcriptor de audio con filtrado de alucinaciones.

Mantiene la API pública que usa main.py (``transcribe`` y ``translate``) sin
cambios; internamente delega en el backend configurado vía la variable de
entorno ``TRANSCRIPTION_BACKEND`` (por defecto ``"groq"``).

El filtrado de alucinaciones es agnóstico al backend y se aplica siempre aquí,
en la capa de orquestación, sin que el backend tenga que conocerlo.
"""
import io
import logging
import os
import socket
import threading
import time
from typing import Callable

from core.backends import get_backend
from core.backends.base import TranscriptionBackend
from core import dictionary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Clasificación de errores de red (unidad 5.5 — fallback simétrico Groq -> local)
# ---------------------------------------------------------------------------

def _is_network_error(exc: Exception) -> bool:
    """Devuelve True si ``exc`` es un fallo de RED (sin conexión / timeout), no
    un fallo de la API en sí (auth, rate-limit, bad request...).

    Solo los fallos de RED disparan el fallback a local: un 401/429/400 no se
    arregla reintentando offline, así que esos se propagan tal cual (igual que
    hoy), sin gastar el modelo local en un problema que no es de conectividad.

    Cubre, por capas (de más a menos específico):
    1. ``groq.APIConnectionError`` (y su subclase ``APITimeoutError``) — el SDK
       de Groq envuelve ahí los fallos de transporte de httpx. Explícitamente
       NO cubre ``groq.APIStatusError`` (padre de AuthenticationError,
       BadRequestError, RateLimitError...), que son fallos de la API, no de red.
    2. Excepciones de ``httpx`` (transporte real que usa el SDK de Groq) por si
       alguna se escapa sin envolver.
    3. Excepciones de ``requests`` por si algún backend futuro lo usa.
    4. Builtins de red de la stdlib (``ConnectionError``, ``TimeoutError``,
       ``socket.timeout``, ``socket.gaierror``).
    """
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
        return True

    try:
        import groq as _groq  # noqa: PLC0415
        if isinstance(exc, _groq.APIConnectionError):
            return True
    except ImportError:
        pass

    try:
        import httpx as _httpx  # noqa: PLC0415
        if isinstance(exc, (
            _httpx.ConnectError, _httpx.ConnectTimeout, _httpx.ReadTimeout, _httpx.NetworkError,
        )):
            return True
    except ImportError:
        pass

    try:
        import requests as _requests  # noqa: PLC0415
        if isinstance(exc, (_requests.exceptions.ConnectionError, _requests.exceptions.Timeout)):
            return True
    except ImportError:
        pass

    return False


# ---------------------------------------------------------------------------
# Filtrado de alucinaciones de Whisper
# ---------------------------------------------------------------------------

# Fragmentos INCONFUNDIBLES que jamás aparecen en dictado real: se buscan por
# contención (aunque vayan dentro de otro texto) porque nadie los dicta nunca.
# Lista basada en el upstream macOS (Daniel Carreón, ea0f413).
_HALLUCINATION_MARKERS = (
    "subtitulado por la comunidad",
    "subtítulos por la comunidad",
    "subtitulos realizados por la comunidad",
    "subtítulos realizados por la comunidad",
    "subtítulos por la comunidad de amara",
    "amara.org",
    "suscríbete al canal",
    "suscribete al canal",
)

# Alucinaciones CORTAS y sueltas que Whisper devuelve en silencio puro.
# A diferencia de las frases largas, estas son palabras/expresiones que SÍ podrían
# aparecer dentro de dictado real, por lo que se comparan por COINCIDENCIA EXACTA
# contra el texto completo normalizado (sin puntuación), no por contención: solo
# se descartan cuando son la ÚNICA salida del modelo. Tradeoff aceptado: si dictas
# literalmente "gracias" y nada más, se filtrará (caso rarísimo frente al de silencio).
_HALLUCINATION_EXACT = frozenset({
    # Cortas sueltas
    "gracias",
    "muchas gracias",
    "gracias a todos",
    "vale",
    "you",
    "thank you",
    "thanks",
    "bye",
    "adios",
    "adiós",
    "hasta luego",
    # Frases completas que SÍ podrían incrustarse en dictado real, por eso van por
    # exacto (solo se filtran cuando son la única salida del modelo).
    "gracias por ver",
    "gracias por ver el video",
    "gracias por ver el vídeo",
    "gracias por ver este video",
    "gracias por ver este vídeo",
    "thank you for watching",
    "thanks for watching",
    "please subscribe",
    "see you next time",
    "estoy listo para ayudarte",
    "qué transcripción de voz necesitas",
    "que transcripcion de voz necesitas",
})

# Caracteres de puntuación/espacio que se recortan de los extremos al normalizar
# para la comparación exacta (p. ej. "¡Gracias!" / "Gracias." → "gracias").
_TRIM_CHARS = " \t\n.,!?¡¿…\"'-"

# Umbral de longitud para distinguir alucinaciones de dictado legítimo largo.
# Una alucinación típica es la ÚNICA salida del modelo (texto corto y genérico).
# Si el texto supera este límite, asumimos que es dictado real que menciona
# casualmente una frase marcadora (p. ej. "le di las gracias por ver el video
# que le mandé") y NO lo descartamos.
_HALLUCINATION_MAX_LENGTH = 80


def _is_hallucination(text: str) -> bool:
    """Devuelve True si el texto es una alucinación conocida de Whisper.

    Whisper produce estas frases fijas cuando recibe audio silencioso o
    demasiado corto para transcribir. La heurística combina dos condiciones:

    1. El texto (en minúsculas, sin espacios extremos) contiene alguno de los
       marcadores de ``_HALLUCINATION_MARKERS``.
    2. El texto es suficientemente corto (≤ ``_HALLUCINATION_MAX_LENGTH``
       caracteres tras strip). Esto evita falsos positivos: si alguien dicta
       un párrafo largo que casualmente menciona "gracias por ver el video",
       el texto supera el umbral y no se descarta.

    Argumentos:
        text: Texto devuelto por la API de Whisper, ya con strip() aplicado.

    Retorna:
        True si debe considerarse alucinación y descartarse; False en caso
        contrario.
    """
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) > _HALLUCINATION_MAX_LENGTH:
        # Texto largo → casi seguro dictado real; no filtrar.
        return False
    lowered = stripped.lower()
    # 1) Coincidencia EXACTA contra alucinaciones cortas sueltas (texto completo
    #    normalizado sin puntuación), p. ej. "Gracias." → "gracias".
    if lowered.strip(_TRIM_CHARS) in _HALLUCINATION_EXACT:
        return True
    # 2) Contención de frases largas inconfundibles (subtítulos de Amara, etc.).
    return any(marker in lowered for marker in _HALLUCINATION_MARKERS)


# ---------------------------------------------------------------------------
# Transcriber
# ---------------------------------------------------------------------------

class Transcriber:
    """Orquestador de transcripción: gestiona el backend activo y filtra
    alucinaciones.

    Conserva la API pública original para que main.py no requiera cambios:
    - ``transcribe(wav_buffer, prompt=None) -> str``
    - ``translate(wav_buffer, target_lang="en") -> str``

    El backend se selecciona por la env var ``TRANSCRIPTION_BACKEND`` (por
    defecto ``"groq"``).  Se re-lee en cada llamada para permitir toggle desde
    el dashboard sin reiniciar la app; si cambia, el backend anterior se libera
    y se instancia uno nuevo (operación protegida por lock).
    """

    def __init__(self):
        """Inicializa el transcriptor sin instanciar el backend todavía
        (lazy init para que la clave del FirstRunDialog esté disponible)."""
        self._backend: TranscriptionBackend | None = None
        self._backend_name: str | None = None
        self._lock = threading.Lock()

        # --- Fallback simétrico Groq -> local por fallos de RED (unidad 5.5) ---
        # Solo se activa cuando el CALLER pasa net_fallback=True explícitamente
        # (dictado normal en main.py); reunión (core/meeting.py) y URL
        # (core/url_transcribe.py) crean su propia instancia de Transcriber y
        # nunca pasan net_fallback=True, así que este estado nunca se toca ahí.
        self._net_breaker_failed_at: float | None = None  # time.monotonic() del último fallo de red
        self._net_notified_using_local: bool = False       # dedup: aviso "usando local" por episodio
        self._net_missing_notified_at: float | None = None  # dedup: aviso "modelo no descargado"
        # Callback opcional (seteado por main.py) para notificar al usuario (tray)
        # cuando ocurre un evento de fallback de red. Firma: (message: str) -> None.
        # Se invoca desde el hilo background del dictado; core/ no conoce Qt —
        # main.py decide cómo mostrar el mensaje (ver VflowApp).
        self.on_net_fallback_event: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # Gestión del backend
    # ------------------------------------------------------------------

    def _get_backend(self) -> TranscriptionBackend:
        """Devuelve el backend activo, recreándolo si ``TRANSCRIPTION_BACKEND``
        cambió desde la última llamada."""
        current_name = os.getenv("TRANSCRIPTION_BACKEND", "groq").strip().lower()
        if self._backend is None or self._backend_name != current_name:
            with self._lock:
                # Re-leer tras adquirir el lock (doble comprobación)
                current_name = os.getenv("TRANSCRIPTION_BACKEND", "groq").strip().lower()
                if self._backend is None or self._backend_name != current_name:
                    if self._backend is not None:
                        try:
                            self._backend.release()
                        except Exception as e:  # noqa: BLE001
                            logger.warning("Error al liberar backend %r: %s", self._backend_name, e)
                    logger.info("Instanciando backend de transcripción: %r", current_name)
                    self._backend = get_backend(current_name)
                    self._backend_name = current_name
        return self._backend

    # ------------------------------------------------------------------
    # API pública (sin cambios respecto a la versión anterior)
    # ------------------------------------------------------------------

    def transcribe(
        self,
        wav_buffer: io.BytesIO,
        prompt: str = None,
        return_raw: bool = False,
        net_fallback: bool = False,
    ):
        """Envía audio WAV al backend activo y devuelve el texto transcrito.

        Si el backend activo es "local" y falla, y ``GROQ_FALLBACK=true`` está
        activado y hay ``GROQ_API_KEY`` configurada, reintenta con el backend
        Groq y emite WARNING.  Si el fallback también falla, propaga la
        excepción original del backend local.

        Si el backend activo es "groq" y falla por un error de RED (unidad 5.5),
        ``net_fallback=True`` y ``TRANSCRIPTION_FALLBACK=true`` están activos, y
        el modelo local está descargado, reintenta con el backend local (sentido
        inverso y simétrico al fallback anterior). Ver ``_run_net_fallback``.

        Args:
            wav_buffer: Datos de audio en formato WAV.
            prompt: Contexto opcional del chunk anterior para mejorar continuidad.
            return_raw: Si True, devuelve la tupla ``(text, raw_text)`` donde
                ``raw_text`` es el texto tal como salió del backend, ANTES de
                aplicar los reemplazos del diccionario (``None`` si coincide
                con ``text``, para no obligar al caller a comparar strings).
                Por defecto False para no romper a los llamadores existentes
                (main.py, core/meeting.py, core/url_transcribe.py): sin este
                kwarg, la firma de retorno sigue siendo ``str`` sin cambios.
            net_fallback: Si True, habilita el fallback Groq -> local por fallo
                de RED (unidad 5.5). Por defecto False: SOLO el flujo de
                dictado normal en main.py lo activa; reunión (core/meeting.py)
                y URL (core/url_transcribe.py) NUNCA pasan True, así que su
                comportamiento no cambia (scope deliberadamente acotado).

        Returns:
            Texto transcrito (str), o tupla (text, raw_text) si return_raw=True.
            Cadena vacía si no hay audio útil o el resultado es una
            alucinación conocida de Whisper.
        """
        lang = os.getenv("WHISPER_LANGUAGE", "es")
        effective_prompt = dictionary.compose_prompt(prompt, include_vocab=True)
        backend_name = os.getenv("TRANSCRIPTION_BACKEND", "groq").strip().lower()

        def _call_primary():
            return self._get_backend().transcribe(wav_buffer, language=lang, prompt=effective_prompt)

        def _call_local():
            return get_backend("local").transcribe(wav_buffer, language=lang, prompt=effective_prompt)

        try:
            text = self._run_net_fallback(
                wav_buffer,
                net_fallback=net_fallback,
                backend_name=backend_name,
                call_primary=_call_primary,
                call_local=_call_local,
            )
        except Exception as primary_exc:
            if self._should_use_groq_fallback(backend_name):
                logger.warning(
                    "Backend local falló; usando Groq como respaldo (GROQ_FALLBACK=true). Error: %s",
                    primary_exc,
                )
                wav_buffer.seek(0)
                try:
                    groq_backend = get_backend("groq")
                    text = groq_backend.transcribe(wav_buffer, language=lang, prompt=effective_prompt)
                except Exception:
                    raise primary_exc from None  # propagar excepción original
            else:
                raise

        if _is_hallucination(text):
            return ("", None) if return_raw else ""
        final_text = dictionary.apply_replacements(text)
        if not return_raw:
            return final_text
        raw = text if text != final_text else None
        return final_text, raw

    def translate(
        self,
        wav_buffer: io.BytesIO,
        target_lang: str = "en",
        return_raw: bool = False,
        net_fallback: bool = False,
    ):
        """Traduce el audio al idioma destino usando el backend activo.

        Si el backend activo es "local" y falla, y ``GROQ_FALLBACK=true`` está
        activado, reintenta con el backend Groq (mismo comportamiento que
        ``transcribe``).

        Args:
            wav_buffer: Datos de audio en formato WAV.
            target_lang: Código ISO del idioma destino.
            return_raw: Si True, devuelve la tupla ``(text, raw_text)`` igual
                que ``transcribe``. Por defecto False (sin cambio de firma).
            net_fallback: Ver ``transcribe``. Solo tiene efecto cuando el
                backend efectivo es "groq" de forma NATIVA (no vía la
                redirección local->groq de arriba, que ya implica que el
                target no es "en") Y ``target_lang == "en"`` — la ÚNICA
                traducción que el backend local sabe hacer nativamente
                (unidad 5.5, punto de diseño 8). Para cualquier otro target
                el comportamiento es idéntico al actual (sin fallback).

        Returns:
            Texto traducido (str), o tupla (text, raw_text) si return_raw=True.
            Cadena vacía si no hay audio útil o el resultado es una
            alucinación.
        """
        original_backend_name = os.getenv("TRANSCRIPTION_BACKEND", "groq").strip().lower()
        backend_name = original_backend_name

        # El backend local solo traduce a inglés: con target != "en" degrada a
        # transcripción sin traducir. Si el usuario activó GROQ_FALLBACK, esa
        # degradación también cuenta como "el local no puede" → usar Groq
        # directamente (el opt-in ya autoriza enviar el audio a internet).
        if (
            backend_name == "local"
            and target_lang.strip().lower() != "en"
            and self._should_use_groq_fallback(backend_name)
        ):
            logger.warning(
                "Backend local no traduce a '%s'; usando Groq como respaldo (GROQ_FALLBACK=true).",
                target_lang,
            )
            backend_name = "groq"

        # El backend local solo traduce a inglés con task=translate nativa;
        # no inyectar vocab en ese caso.
        include_vocab = (backend_name == "groq")
        effective_prompt = dictionary.compose_prompt(None, include_vocab=include_vocab)

        # Usar el backend redirigido si aplica (get_backend es singleton por nombre)
        active_backend = (
            get_backend("groq") if backend_name == "groq" and original_backend_name == "local"
            else self._get_backend()
        )

        def _call_primary():
            return active_backend.translate(wav_buffer, target_lang=target_lang, prompt=effective_prompt)

        def _call_local():
            return get_backend("local").translate(wav_buffer, target_lang=target_lang, prompt=effective_prompt)

        # net_fallback (Groq -> local, unidad 5.5) solo aplica al camino NATIVO
        # groq->groq (no a la redirección local->groq de arriba, que ocurre
        # precisamente cuando target != "en" — target que el local no soporta,
        # así que caer de vuelta al local sería absurdo) y solo si target=="en".
        net_fallback_applicable = (
            backend_name == "groq"
            and original_backend_name == "groq"
            and target_lang.strip().lower() == "en"
        )

        try:
            if net_fallback_applicable:
                text = self._run_net_fallback(
                    wav_buffer,
                    net_fallback=net_fallback,
                    backend_name="groq",
                    call_primary=_call_primary,
                    call_local=_call_local,
                )
            else:
                text = _call_primary()
        except Exception as primary_exc:
            if self._should_use_groq_fallback(backend_name):
                logger.warning(
                    "Backend local falló; usando Groq como respaldo (GROQ_FALLBACK=true). Error: %s",
                    primary_exc,
                )
                wav_buffer.seek(0)
                try:
                    groq_backend = get_backend("groq")
                    groq_prompt = dictionary.compose_prompt(None, include_vocab=True)
                    text = groq_backend.translate(wav_buffer, target_lang=target_lang, prompt=groq_prompt)
                except Exception:
                    raise primary_exc from None
            else:
                raise

        if _is_hallucination(text):
            return ("", None) if return_raw else ""
        final_text = dictionary.apply_replacements(text)
        if not return_raw:
            return final_text
        raw = text if text != final_text else None
        return final_text, raw

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _should_use_groq_fallback(backend_name: str) -> bool:
        """Devuelve True si se debe intentar el fallback a Groq.

        Condiciones:
        1. El backend activo es "local".
        2. ``GROQ_FALLBACK=true`` en el entorno.
        3. Hay una ``GROQ_API_KEY`` configurada (no vacía).

        Ver también ``TRANSCRIPTION_FALLBACK`` (unidad 5.5): el fallback
        SIMÉTRICO inverso, local -> Groq de aquí no aplica; ese es Groq -> local,
        gestionado por ``_run_net_fallback`` / ``_transcription_fallback_enabled``.
        """
        if backend_name != "local":
            return False
        if os.getenv("GROQ_FALLBACK", "false").lower().strip() != "true":
            return False
        return bool(os.getenv("GROQ_API_KEY", "").strip())

    # ------------------------------------------------------------------
    # Fallback simétrico Groq -> local por fallos de RED (unidad 5.5)
    # ------------------------------------------------------------------

    @staticmethod
    def _transcription_fallback_enabled() -> bool:
        """``TRANSCRIPTION_FALLBACK=true`` (opt-in, default false, lazy)."""
        return os.getenv("TRANSCRIPTION_FALLBACK", "false").strip().lower() == "true"

    @staticmethod
    def _net_fallback_cooldown() -> float:
        """Segundos que el breaker evita reintentar Groq tras un fallo de red
        (``TRANSCRIPTION_FALLBACK_COOLDOWN``, default 120, lazy — mismo patrón
        que ``INSIGHTS_FALLBACK_COOLDOWN`` en core/insights.py)."""
        try:
            return float(os.getenv("TRANSCRIPTION_FALLBACK_COOLDOWN", "120") or 120)
        except ValueError:
            return 120.0

    def _net_breaker_open(self) -> bool:
        """¿El breaker sigue abierto (Groq falló hace menos del cooldown)?"""
        if self._net_breaker_failed_at is None:
            return False
        return (time.monotonic() - self._net_breaker_failed_at) < self._net_fallback_cooldown()

    def _net_breaker_trip(self) -> None:
        """Abre el breaker: registra el timestamp del fallo de red de Groq."""
        self._net_breaker_failed_at = time.monotonic()

    def _net_breaker_reset(self) -> None:
        """Cierra el breaker (Groq volvió a responder) y limpia los dedups de
        notificación asociados a ese episodio, para que un fallo de red futuro
        vuelva a notificar."""
        if self._net_breaker_failed_at is not None:
            logger.info("net_fallback: Groq volvió a responder — breaker reseteado")
        self._net_breaker_failed_at = None
        self._net_notified_using_local = False
        self._net_missing_notified_at = None

    def _local_model_ready(self) -> bool:
        """¿El modelo local está DESCARGADO en disco? Nunca dispara una
        descarga — solo comprueba (``is_ready()`` de LocalBackend es un check
        de archivos, no carga el modelo)."""
        try:
            return get_backend("local").is_ready()
        except Exception as exc:  # noqa: BLE001
            logger.warning("net_fallback: no se pudo comprobar el modelo local: %s", exc)
            return False

    def _trigger_local_warmup_async(self) -> None:
        """Precalienta el modelo local en un hilo daemon fire-and-forget
        (objeción D4 del debate): se dispara UNA vez, en la transición
        cerrado->abierto del breaker, para que el fallback ya esté tibio en el
        siguiente dictado. Un fallo de warmup se ignora — el fallback igual
        lo intentará (LocalBackend.warmup() ya es fail-safe internamente)."""
        def _do_warmup():
            try:
                get_backend("local").warmup()
            except Exception as exc:  # noqa: BLE001
                logger.warning("net_fallback: warmup del modelo local falló: %s", exc)

        threading.Thread(target=_do_warmup, daemon=True, name="vflow-net-fallback-warmup").start()

    def _emit_net_fallback_event(self, message: str) -> None:
        """Invoca ``on_net_fallback_event`` (seteado por main.py) si existe.
        core/ no conoce Qt: esto es un callback plano; main.py decide cómo
        mostrarlo (tray). Un callback que lanza no debe romper el dictado."""
        callback = self.on_net_fallback_event
        if callback is None:
            return
        try:
            callback(message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("net_fallback: callback de notificación falló: %s", exc)

    def _notify_using_local_fallback(self) -> None:
        """Notifica UNA vez por episodio de breaker que el dictado está
        usando el modelo local por falta de red (punto de diseño 9)."""
        if self._net_notified_using_local:
            return
        self._net_notified_using_local = True
        self._emit_net_fallback_event("Sin internet: transcribiendo con el modelo local.")

    def _notify_model_missing_once(self) -> None:
        """Notifica que no hay red Y el modelo local no está descargado
        (punto de diseño 4). Dedup con el mismo cooldown que el breaker de red
        (reutiliza ``TRANSCRIPTION_FALLBACK_COOLDOWN`` en vez de crear una
        variable nueva): no spamea en cada dictado, pero si la falta de
        internet persiste más allá del cooldown, vuelve a avisar (decisión
        documentada — no hay una variable ENV_CATALOG dedicada para esto)."""
        now = time.monotonic()
        cooldown = self._net_fallback_cooldown()
        if self._net_missing_notified_at is not None and (now - self._net_missing_notified_at) < cooldown:
            return
        self._net_missing_notified_at = now
        self._emit_net_fallback_event(
            "Sin internet y sin modelo local descargado — descárgalo en el dashboard "
            "para dictar offline."
        )

    def _run_net_fallback(
        self,
        wav_buffer: io.BytesIO,
        *,
        net_fallback: bool,
        backend_name: str,
        call_primary: "Callable[[], str]",
        call_local: "Callable[[], str]",
    ) -> str:
        """Ejecuta ``call_primary()`` (backend Groq); si falla por un error de
        RED y el fallback está habilitado, reintenta con ``call_local()``.

        Contrato (unidad 5.5, diseño cerrado del debate adversarial):
        - Con ``net_fallback=False`` (default) o ``backend_name != "groq"`` o
          ``TRANSCRIPTION_FALLBACK`` apagado: comportamiento IDÉNTICO a llamar
          ``call_primary()`` directamente — cero cambio (objeción D1).
        - Si el breaker está abierto (fallo de red reciente) Y el modelo local
          está descargado: va DIRECTO a local sin pagar el timeout de Groq
          (punto 5). Si el modelo NO está descargado, reintenta Groq (no hay
          alternativa razonable).
        - Si ``call_primary()`` falla con un error QUE NO es de red: se
          propaga tal cual, sin fallback (objeción D3 — un 401/429 no se
          arregla offline).
        - Si falla por red y el modelo local no está descargado: notifica una
          vez y propaga el error de red ORIGINAL (punto 4).
        - Si falla por red, el modelo está listo, y el fallback a local
          también falla: propaga el error de red ORIGINAL (no el del
          fallback), igual que el patrón local->Groq existente.
        """
        fallback_eligible = (
            net_fallback and backend_name == "groq" and self._transcription_fallback_enabled()
        )

        if fallback_eligible and self._net_breaker_open() and self._local_model_ready():
            logger.info("net_fallback: breaker abierto — usando modelo local directo (sin reintentar Groq)")
            wav_buffer.seek(0)
            text = call_local()
            self._notify_using_local_fallback()
            return text

        try:
            text = call_primary()
            if backend_name == "groq":
                self._net_breaker_reset()
            return text
        except Exception as exc:
            if not fallback_eligible or not _is_network_error(exc):
                raise
            if not self._local_model_ready():
                self._notify_model_missing_once()
                raise
            was_open = self._net_breaker_open()
            self._net_breaker_trip()
            if not was_open:
                self._trigger_local_warmup_async()
            wav_buffer.seek(0)
            try:
                text = call_local()
            except Exception:
                raise exc from None  # propagar el error de RED original
            self._notify_using_local_fallback()
            return text
