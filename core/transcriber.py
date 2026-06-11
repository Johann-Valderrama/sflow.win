import io
import os
import threading
from groq import Groq
from config import GROQ_MODEL


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


class Transcriber:
    """Cliente de transcripción que envía audio a la API Groq Whisper."""

    def __init__(self):
        """Inicializa el transcriptor con cliente Groq diferido (lazy init)."""
        self._client = None
        self._lock = threading.Lock()

    def _get_client(self) -> Groq:
        """Lazy init: creates client on first use so API key from first-run dialog works."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    key = os.getenv("GROQ_API_KEY", "")
                    if not key:
                        raise ValueError("GROQ_API_KEY not configured")
                    self._client = Groq(api_key=key, timeout=10.0)
        return self._client

    def transcribe(self, wav_buffer: io.BytesIO, prompt: str = None) -> str:
        """Send WAV audio to Groq Whisper and return transcribed text.

        Args:
            wav_buffer: WAV audio data.
            prompt: Optional context from previous chunk to improve continuity.
        """
        wav_buffer.seek(0)
        data = wav_buffer.read()
        if len(data) < 100:
            return ""
        lang = os.getenv("WHISPER_LANGUAGE", "es")
        # whisper-large-v3-turbo has degraded language detection; use full model for auto-detect
        model = "whisper-large-v3" if (not lang or lang == "auto") else GROQ_MODEL
        kwargs = dict(
            file=("recording.wav", data),
            model=model,
            response_format="text",
            temperature=0.0,
        )
        if lang and lang != "auto":
            kwargs["language"] = lang
        if prompt:
            kwargs["prompt"] = prompt
        transcription = self._get_client().audio.transcriptions.create(**kwargs)
        text = transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
        if _is_hallucination(text):
            return ""
        return text

    def translate(self, wav_buffer: io.BytesIO, target_lang: str = "en") -> str:
        """Translate speech to target_lang.

        - target_lang == "en": uses Whisper /audio/translations endpoint (fastest, best quality).
        - target_lang != "en": transcribes with Whisper first, then translates via Groq LLM
          (llama-3.1-8b-instant), which supports any language pair.
        """
        wav_buffer.seek(0)
        data = wav_buffer.read()
        if len(data) < 100:
            return ""

        if target_lang == "en":
            # Whisper native translation → English (best accuracy)
            translation = self._get_client().audio.translations.create(
                file=("recording.wav", data),
                model="whisper-large-v3",
                response_format="text",
                temperature=0.0,
            )
            text = translation.strip() if isinstance(translation, str) else str(translation).strip()
            if _is_hallucination(text):
                return ""
            return text

        # For non-English targets: transcribe first, then LLM-translate
        wav_buffer.seek(0)
        transcript = self.transcribe(wav_buffer)
        if not transcript:
            return ""
        return self._llm_translate(transcript, target_lang)

    # Language code → full name for LLM prompt
    _LANG_NAMES = {
        "es": "Spanish", "en": "English", "fr": "French", "de": "German",
        "it": "Italian", "pt": "Portuguese", "ja": "Japanese", "zh": "Chinese",
        "ko": "Korean", "ru": "Russian", "ar": "Arabic", "nl": "Dutch",
    }

    def _llm_translate(self, text: str, target_lang: str) -> str:
        """Translate text to target_lang using Groq LLM (llama-3.1-8b-instant)."""
        target_name = self._LANG_NAMES.get(target_lang, target_lang)
        response = self._get_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Translate the following text to {target_name}. "
                        "Output only the translation, nothing else. "
                        "Preserve punctuation and formatting."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.1,
        )
        return response.choices[0].message.content.strip()
