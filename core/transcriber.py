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
import threading

from core.backends import get_backend
from core.backends.base import TranscriptionBackend
from core import dictionary

logger = logging.getLogger(__name__)


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

    def transcribe(self, wav_buffer: io.BytesIO, prompt: str = None) -> str:
        """Envía audio WAV al backend activo y devuelve el texto transcrito.

        Si el backend activo es "local" y falla, y ``GROQ_FALLBACK=true`` está
        activado y hay ``GROQ_API_KEY`` configurada, reintenta con el backend
        Groq y emite WARNING.  Si el fallback también falla, propaga la
        excepción original del backend local.

        Args:
            wav_buffer: Datos de audio en formato WAV.
            prompt: Contexto opcional del chunk anterior para mejorar continuidad.

        Returns:
            Texto transcrito.  Cadena vacía si no hay audio útil o el resultado
            es una alucinación conocida de Whisper.
        """
        lang = os.getenv("WHISPER_LANGUAGE", "es")
        effective_prompt = dictionary.compose_prompt(prompt, include_vocab=True)
        backend_name = os.getenv("TRANSCRIPTION_BACKEND", "groq").strip().lower()

        try:
            text = self._get_backend().transcribe(wav_buffer, language=lang, prompt=effective_prompt)
        except Exception as local_exc:
            if self._should_use_groq_fallback(backend_name):
                logger.warning(
                    "Backend local falló; usando Groq como respaldo (GROQ_FALLBACK=true). Error: %s",
                    local_exc,
                )
                wav_buffer.seek(0)
                try:
                    groq_backend = get_backend("groq")
                    text = groq_backend.transcribe(wav_buffer, language=lang, prompt=effective_prompt)
                except Exception:
                    raise local_exc from None  # propagar excepción original
            else:
                raise

        if _is_hallucination(text):
            return ""
        return dictionary.apply_replacements(text)

    def translate(self, wav_buffer: io.BytesIO, target_lang: str = "en") -> str:
        """Traduce el audio al idioma destino usando el backend activo.

        Si el backend activo es "local" y falla, y ``GROQ_FALLBACK=true`` está
        activado, reintenta con el backend Groq (mismo comportamiento que
        ``transcribe``).

        Args:
            wav_buffer: Datos de audio en formato WAV.
            target_lang: Código ISO del idioma destino.

        Returns:
            Texto traducido.  Cadena vacía si no hay audio útil o el resultado
            es una alucinación.
        """
        backend_name = os.getenv("TRANSCRIPTION_BACKEND", "groq").strip().lower()
        # El backend local solo traduce a inglés con task=translate nativa;
        # no inyectar vocab en ese caso.
        include_vocab = (backend_name == "groq")
        effective_prompt = dictionary.compose_prompt(None, include_vocab=include_vocab)

        try:
            text = self._get_backend().translate(wav_buffer, target_lang=target_lang, prompt=effective_prompt)
        except Exception as local_exc:
            if self._should_use_groq_fallback(backend_name):
                logger.warning(
                    "Backend local falló; usando Groq como respaldo (GROQ_FALLBACK=true). Error: %s",
                    local_exc,
                )
                wav_buffer.seek(0)
                try:
                    groq_backend = get_backend("groq")
                    groq_prompt = dictionary.compose_prompt(None, include_vocab=True)
                    text = groq_backend.translate(wav_buffer, target_lang=target_lang, prompt=groq_prompt)
                except Exception:
                    raise local_exc from None
            else:
                raise

        if _is_hallucination(text):
            return ""
        return dictionary.apply_replacements(text)

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
        """
        if backend_name != "local":
            return False
        if os.getenv("GROQ_FALLBACK", "false").lower().strip() != "true":
            return False
        return bool(os.getenv("GROQ_API_KEY", "").strip())
