"""Backend de transcripción basado en Groq Whisper API.

Encapsula toda la lógica de comunicación con Groq: lazy init del cliente,
transcripción con Whisper large-v3-turbo, traducción nativa a inglés y
traducción a otros idiomas vía LLM (llama-3.1-8b-instant).

Feature VAD: antes de enviar audio a la API se aplica Silero VAD para recortar
silencios (reduce costo/latencia y alucinaciones).  Controlado por la env var
``VAD_ENABLED`` (default "true").  Si el VAD no detecta voz devuelve "" sin
llamar a la API.  Si el VAD falla, el audio se envía sin modificar (fail-open).
"""
import io
import os
import threading

from groq import Groq

from config import GROQ_MODEL
from core.backends.base import TranscriptionBackend


# Código de idioma → nombre completo para el prompt LLM
_LANG_NAMES = {
    "es": "Spanish", "en": "English", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ja": "Japanese", "zh": "Chinese",
    "ko": "Korean", "ru": "Russian", "ar": "Arabic", "nl": "Dutch",
}


class GroqBackend(TranscriptionBackend):
    """Backend que usa la API Groq Whisper para transcribir y traducir audio."""

    def __init__(self):
        """Inicializa el backend con cliente Groq diferido (lazy init)."""
        self._client = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Interfaz pública de TranscriptionBackend
    # ------------------------------------------------------------------

    def get_model_name(self) -> str:
        return GROQ_MODEL

    def transcribe(
        self,
        wav_buffer: io.BytesIO,
        language: str,
        prompt: str | None = None,
    ) -> str:
        """Envía audio WAV a Groq Whisper y devuelve el texto transcrito.

        Aplica VAD internamente antes de llamar a la API (recorta silencios).

        Args:
            wav_buffer: Datos WAV.  El método hace ``seek(0)`` internamente.
            language:   Código ISO del idioma fuente; ``"auto"`` para
                        detección automática.
            prompt:     Contexto opcional del chunk anterior.

        Returns:
            Texto transcrito con ``strip()``.  Cadena vacía si los datos son
            insuficientes (<100 bytes) o si el VAD no detecta voz.
        """
        from core.vad import apply_vad  # noqa: PLC0415
        vad_result = apply_vad(wav_buffer)
        if vad_result is None:
            # Sin voz detectada; no llamar a la API
            return ""
        return self._transcribe_raw(vad_result, language=language, prompt=prompt)

    def translate(self, wav_buffer: io.BytesIO, target_lang: str = "en", prompt: str | None = None) -> str:
        """Traduce el audio al idioma destino.

        - ``target_lang == "en"``: usa el endpoint Whisper /audio/translations
          (más rápido y preciso).
        - Otros idiomas: transcribe primero con Whisper, luego traduce con el
          LLM llama-3.1-8b-instant (cualquier par de idiomas).

        Aplica VAD una sola vez al buffer de entrada.

        Args:
            wav_buffer: Datos WAV.
            target_lang: Código ISO del idioma destino.

        Returns:
            Texto traducido.  Cadena vacía si los datos son insuficientes.
        """
        from core.vad import apply_vad  # noqa: PLC0415
        vad_result = apply_vad(wav_buffer)
        if vad_result is None:
            return ""
        wav_buffer = vad_result

        wav_buffer.seek(0)
        data = wav_buffer.read()
        if len(data) < 100:
            return ""

        if target_lang == "en":
            # Traducción nativa de Whisper → inglés (mejor precisión)
            tr_kwargs = dict(
                file=("recording.wav", data),
                model="whisper-large-v3",
                response_format="text",
                temperature=0.0,
            )
            if prompt:
                tr_kwargs["prompt"] = prompt
            translation = self._get_client().audio.translations.create(**tr_kwargs)
            text = translation.strip() if isinstance(translation, str) else str(translation).strip()
            return text

        # Para idiomas distintos de inglés: transcribir primero (VAD ya aplicado),
        # luego traducir con LLM.  Llamamos _transcribe_raw para no re-aplicar VAD.
        wav_buffer.seek(0)
        lang = os.getenv("WHISPER_LANGUAGE", "es")
        transcript = self._transcribe_raw(wav_buffer, language=lang)
        if not transcript:
            return ""
        return self._llm_translate(transcript, target_lang)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _transcribe_raw(
        self,
        wav_buffer: io.BytesIO,
        language: str,
        prompt: str | None = None,
    ) -> str:
        """Transcribe sin aplicar VAD (para uso interno cuando VAD ya se aplicó).

        Hace seek(0) sobre ``wav_buffer`` y envía los datos a la API Groq.
        """
        wav_buffer.seek(0)
        data = wav_buffer.read()
        if len(data) < 100:
            return ""

        model = "whisper-large-v3" if (not language or language == "auto") else GROQ_MODEL
        kwargs = dict(
            file=("recording.wav", data),
            model=model,
            response_format="text",
            temperature=0.0,
        )
        if language and language != "auto":
            kwargs["language"] = language
        if prompt:
            kwargs["prompt"] = prompt

        transcription = self._get_client().audio.transcriptions.create(**kwargs)
        text = transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
        return text

    def _get_client(self) -> Groq:
        """Lazy init: crea el cliente en el primer uso para que la clave
        configurada en el FirstRunDialog esté disponible."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    key = os.getenv("GROQ_API_KEY", "")
                    if not key:
                        raise ValueError("GROQ_API_KEY not configured")
                    self._client = Groq(api_key=key, timeout=10.0)
        return self._client

    def _llm_translate(self, text: str, target_lang: str) -> str:
        """Traduce ``text`` a ``target_lang`` usando el LLM llama-3.1-8b-instant."""
        target_name = _LANG_NAMES.get(target_lang, target_lang)
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
