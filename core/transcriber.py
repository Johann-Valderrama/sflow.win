import io
import os
import threading
from groq import Groq
from config import GROQ_MODEL, WHISPER_LANGUAGE


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
        kwargs = dict(
            file=("recording.wav", data),
            model=GROQ_MODEL,
            language=WHISPER_LANGUAGE,
            response_format="text",
            temperature=0.0,
        )
        if prompt:
            kwargs["prompt"] = prompt
        transcription = self._get_client().audio.transcriptions.create(**kwargs)
        text = transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
        return text
