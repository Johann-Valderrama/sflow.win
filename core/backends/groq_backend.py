"""Backend de transcripción basado en Groq Whisper API.

Encapsula toda la lógica de comunicación con Groq: lazy init del cliente,
transcripción con Whisper large-v3-turbo, traducción nativa a inglés y
traducción a otros idiomas vía LLM (llama-3.1-8b-instant).
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

        Args:
            wav_buffer: Datos WAV.  El método hace ``seek(0)`` internamente.
            language:   Código ISO del idioma fuente; ``"auto"`` para
                        detección automática.
            prompt:     Contexto opcional del chunk anterior.

        Returns:
            Texto transcrito con ``strip()``.  Cadena vacía si los datos son
            insuficientes (<100 bytes).
        """
        wav_buffer.seek(0)
        data = wav_buffer.read()
        if len(data) < 100:
            return ""

        # whisper-large-v3-turbo tiene detección de idioma degradada;
        # usar el modelo completo para auto-detect.
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

    def translate(self, wav_buffer: io.BytesIO, target_lang: str = "en", prompt: str | None = None) -> str:
        """Traduce el audio al idioma destino.

        - ``target_lang == "en"``: usa el endpoint Whisper /audio/translations
          (más rápido y preciso).
        - Otros idiomas: transcribe primero con Whisper, luego traduce con el
          LLM llama-3.1-8b-instant (cualquier par de idiomas).

        Args:
            wav_buffer: Datos WAV.
            target_lang: Código ISO del idioma destino.

        Returns:
            Texto traducido.  Cadena vacía si los datos son insuficientes.
        """
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

        # Para idiomas distintos de inglés: transcribir primero, luego LLM
        wav_buffer.seek(0)
        # Usamos WHISPER_LANGUAGE del entorno para la transcripción intermedia;
        # el caller (Transcriber) ya gestiona el idioma, pero aquí necesitamos
        # un valor concreto para la llamada interna.
        lang = os.getenv("WHISPER_LANGUAGE", "es")
        transcript = self.transcribe(wav_buffer, language=lang)
        if not transcript:
            return ""
        return self._llm_translate(transcript, target_lang)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

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
