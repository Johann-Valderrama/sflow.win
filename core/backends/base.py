"""Interfaz base para backends de transcripción.

Define el contrato que todos los backends deben implementar.  Groq Whisper es
el único backend disponible en la Fase 1; en fases futuras se añadirá uno
local basado en faster-whisper.
"""
import io
from abc import ABC, abstractmethod


class TranscriptionBackend(ABC):
    """Contrato mínimo que debe cumplir cualquier backend de transcripción."""

    # ------------------------------------------------------------------
    # Métodos obligatorios
    # ------------------------------------------------------------------

    @abstractmethod
    def transcribe(
        self,
        wav_buffer: io.BytesIO,
        language: str,
        prompt: str | None = None,
    ) -> str:
        """Transcribe audio WAV al idioma indicado.

        Args:
            wav_buffer: Datos de audio en formato WAV (posición arbitraria;
                        el backend puede hacer seek interno).
            language:   Código ISO del idioma fuente (p. ej. ``"es"``).
                        ``"auto"`` indica detección automática.
            prompt:     Contexto opcional del chunk anterior para mejorar la
                        continuidad.

        Returns:
            Texto transcrito, ya con ``strip()`` aplicado.  Cadena vacía si
            no hay audio suficiente para transcribir.
        """

    @abstractmethod
    def translate(self, wav_buffer: io.BytesIO, target_lang: str = "en", prompt: str | None = None) -> str:
        """Traduce el audio a ``target_lang``.

        Args:
            wav_buffer: Datos de audio en formato WAV.
            target_lang: Código ISO del idioma destino (p. ej. ``"en"``).

        Returns:
            Texto traducido.  Cadena vacía si no hay audio suficiente.
        """

    @abstractmethod
    def get_model_name(self) -> str:
        """Nombre del modelo activo (útil para logs y métricas)."""

    # ------------------------------------------------------------------
    # Métodos opcionales con implementación por defecto
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """Indica si el backend está listo para recibir peticiones.

        Por defecto devuelve ``True``; backends con recursos de inicialización
        pesados (p. ej. carga de modelo local) pueden sobrescribir esto.
        """
        return True

    def warmup(self) -> None:
        """Pre-calienta el backend (descarga de modelo, carga en GPU, etc.).

        No-op por defecto.
        """

    def release(self) -> None:
        """Libera los recursos del backend (conexiones, memoria de GPU, etc.).

        No-op por defecto.
        """
