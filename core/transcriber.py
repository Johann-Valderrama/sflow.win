import io
import os
import threading
from groq import Groq
from config import GROQ_MODEL


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
