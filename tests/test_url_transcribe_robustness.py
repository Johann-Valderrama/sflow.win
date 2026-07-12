"""Tests de la unidad 1.8 (robustez de la ruta URL, core/url_transcribe.py).

Cubre los bugs F5/F6/F7 de la auditoría:
  F5 — dedup conservador del solape entre chunks de audio largo.
  F6 — resiliencia por chunk: reintento único + marcador de hueco explícito
       en vez de tirar todo el trabajo previo; error solo si TODOS fallan.
  F7 — refcount + lock para _clean_crypt32_argtypes (dos descargas
       concurrentes ya no se pisan los argtypes de crypt32).

Además, un test documental (ex-unidad 1.6, descartada en debate por premisa
falsa) que deja constancia de que la ruta de AUDIO ya aplica el diccionario
personal vía Transcriber.transcribe (core/transcriber.py:221), una vez por
chunk — NO al final de _transcribe_pcm_chunked.
"""

import sys
import threading

import numpy as np
import pytest

import core.url_transcribe as ut
from core.url_transcribe import (
    _AllChunksFailedError,
    _clean_crypt32_argtypes,
    _dedupe_overlap_prefix,
    _format_gap_marker,
    _transcribe_pcm_chunked,
)


# ---------------------------------------------------------------------------
# F5 — dedup conservador del solape (función pura)
# ---------------------------------------------------------------------------

class TestDedupeOverlapPrefix:
    def test_exact_match_of_five_tokens_is_trimmed(self):
        accumulated = "hola como estas hoy vamos a hablar del proyecto"
        next_text = "vamos a hablar del proyecto de forma clara"
        result = _dedupe_overlap_prefix(accumulated, next_text)
        assert result == "de forma clara"

    def test_match_with_different_punctuation_and_case_still_trims(self):
        """Puntuación y capitalización distintas no impiden el match (se normaliza)."""
        accumulated = "empezamos temprano hoy vamos a comenzar"
        next_text = "Hoy, Vamos A Comenzar y seguir con la agenda"
        result = _dedupe_overlap_prefix(accumulated, next_text)
        assert result == "y seguir con la agenda"

    def test_match_below_min_tokens_does_not_touch_text(self):
        """Menos de 4 tokens de match contiguo → no se recorta nada."""
        accumulated = "el reporte de ventas del mes pasado"
        next_text = "del mes pasado fue mejor de lo esperado"
        # Solape real es "del mes pasado" (3 tokens) — por debajo del umbral (4).
        result = _dedupe_overlap_prefix(accumulated, next_text)
        assert result == next_text

    def test_short_legitimate_repetition_is_never_deduped(self):
        """Muletillas cortas tipo 'no, no' no se tocan (2 tokens, bajo el umbral)."""
        accumulated = "yo creo que no, no"
        next_text = "no, no quiero ir a esa reunión"
        result = _dedupe_overlap_prefix(accumulated, next_text)
        assert result == next_text

    def test_no_match_returns_next_text_unchanged(self):
        accumulated = "hablamos del presupuesto anual"
        next_text = "cambiamos de tema por completo ahora"
        result = _dedupe_overlap_prefix(accumulated, next_text)
        assert result == next_text

    def test_empty_accumulated_returns_next_text_unchanged(self):
        assert _dedupe_overlap_prefix("", "cualquier texto") == "cualquier texto"

    def test_empty_next_text_returns_empty(self):
        assert _dedupe_overlap_prefix("algo previo", "") == ""

    def test_full_duplicate_next_chunk_collapses_to_empty(self):
        """Si TODO el chunk siguiente es el mismo solape, el resultado es cadena vacía."""
        accumulated = "vamos a revisar el contrato completo"
        next_text = "revisar el contrato completo"
        result = _dedupe_overlap_prefix(accumulated, next_text)
        assert result == ""

    def test_longest_match_preferred_over_shorter_one(self):
        """Busca el match CONTIGUO más largo, no se conforma con el primero que encuentra."""
        accumulated = "a b c d e f g h"
        next_text = "e f g h i j k"
        result = _dedupe_overlap_prefix(accumulated, next_text)
        # El match más largo posible es "e f g h" (4 tokens) — recorta los 4.
        assert result == "i j k"


# ---------------------------------------------------------------------------
# F6 — resiliencia por chunk (reintento + marcador de hueco)
# ---------------------------------------------------------------------------

class _ScriptedBackend:
    """Backend falso: cada llamada consume el siguiente ítem del guion.

    Un ítem puede ser un str (texto devuelto) o una excepción (se lanza).
    """

    def __init__(self, script):
        self._script = list(script)
        self.call_count = 0

    def transcribe(self, wav_buffer, language=None, prompt=None):
        self.call_count += 1
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def small_chunks(monkeypatch):
    """Fuerza chunks de 1s (16000 muestras) y sin solape para tests rápidos."""
    monkeypatch.setattr(ut, "_CHUNK_SECONDS", 1)
    monkeypatch.setattr(ut, "_OVERLAP_SECONDS", 0)
    return ut._SAMPLE_RATE  # 16000


class TestChunkResilience:
    def test_middle_chunk_fails_twice_gets_gap_marker_rest_survives(self, small_chunks):
        """Chunk 2 de 3 falla tras su reintento: marcador de hueco + resto presente."""
        sr = small_chunks
        pcm = np.zeros(3 * sr, dtype=np.int16)
        backend = _ScriptedBackend([
            "primer fragmento",          # chunk 1, único intento, éxito
            RuntimeError("boom-1"),      # chunk 2, intento 1
            RuntimeError("boom-2"),      # chunk 2, reintento -> falla definitiva
            "tercer fragmento",           # chunk 3, único intento, éxito
        ])

        text = _transcribe_pcm_chunked(pcm, backend, on_progress=None)

        assert backend.call_count == 4
        assert "primer fragmento" in text
        assert "tercer fragmento" in text
        assert "[fragmento no transcrito ~00:01" in text
        # El marcador queda EN ORDEN, entre los dos fragmentos reales.
        assert text.index("primer fragmento") < text.index("[fragmento no transcrito")
        assert text.index("[fragmento no transcrito") < text.index("tercer fragmento")

    def test_chunk_succeeds_on_retry_no_marker(self, small_chunks):
        """Si el reintento SÍ tiene éxito, no hay marcador (fallo transitorio se recupera)."""
        sr = small_chunks
        pcm = np.zeros(2 * sr, dtype=np.int16)
        backend = _ScriptedBackend([
            RuntimeError("transient"),   # chunk 1, intento 1 falla
            "recuperado",                 # chunk 1, reintento -> éxito
            "segundo fragmento",           # chunk 2
        ])

        text = _transcribe_pcm_chunked(pcm, backend, on_progress=None)

        assert "[fragmento no transcrito" not in text
        assert "recuperado" in text
        assert "segundo fragmento" in text

    def test_silent_chunk_no_exception_no_marker(self, small_chunks):
        """Un chunk que devuelve '' (silencio legítimo) NO produce marcador de hueco."""
        sr = small_chunks
        pcm = np.zeros(3 * sr, dtype=np.int16)
        backend = _ScriptedBackend([
            "hola",
            "",              # silencio legítimo, sin excepción
            "adios",
        ])

        text = _transcribe_pcm_chunked(pcm, backend, on_progress=None)

        assert "[fragmento no transcrito" not in text
        assert "hola" in text
        assert "adios" in text

    def test_all_chunks_fail_raises_all_failed_error(self, small_chunks):
        """Si TODOS los chunks fallan tras su reintento, no hay nada que salvar → error."""
        sr = small_chunks
        pcm = np.zeros(2 * sr, dtype=np.int16)
        backend = _ScriptedBackend([
            RuntimeError("e1"), RuntimeError("e1-retry"),
            RuntimeError("e2"), RuntimeError("e2-retry"),
        ])

        with pytest.raises(_AllChunksFailedError):
            _transcribe_pcm_chunked(pcm, backend, on_progress=None)

    def test_short_audio_single_chunk_failure_propagates_as_all_failed(self, monkeypatch):
        """Audio corto (un solo chunk) que falla tras reintento: mismo comportamiento
        de hoy (se propaga como fallo), ahora vía _AllChunksFailedError."""
        # Sin monkeypatch de _CHUNK_SECONDS: cualquier pcm corto entra por la
        # rama de "un solo chunk" (total_samples <= chunk_samples).
        pcm = np.zeros(1000, dtype=np.int16)
        backend = _ScriptedBackend([RuntimeError("e1"), RuntimeError("e1-retry")])

        with pytest.raises(_AllChunksFailedError):
            _transcribe_pcm_chunked(pcm, backend, on_progress=None)

    def test_gap_marker_has_expected_mmss_timestamps(self):
        marker = _format_gap_marker(start_sample=16000, end_sample=32000, sample_rate=16000)
        assert marker == "[fragmento no transcrito ~00:01–00:02]"


# ---------------------------------------------------------------------------
# F7 — refcount + lock de _clean_crypt32_argtypes
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="crypt32/DPAPI es solo Windows")
class TestCryptArgtypesRefcount:
    def test_concurrent_entries_only_last_exit_restores(self):
        """Dos 'descargas' concurrentes: la primera en entrar limpia, la última
        en salir restaura. Si una termina antes que la otra, los argtypes NO
        deben quedar en None permanentemente para la que sigue en vuelo."""
        import ctypes

        fn = ctypes.windll.crypt32.CryptUnprotectData
        original_argtypes = fn.argtypes

        entered_a = threading.Event()
        release_a = threading.Event()
        seen = {}

        def worker_a():
            with _clean_crypt32_argtypes():
                seen["a"] = fn.argtypes
                entered_a.set()
                release_a.wait(timeout=5)

        def worker_b():
            assert entered_a.wait(timeout=5)
            with _clean_crypt32_argtypes():
                seen["b"] = fn.argtypes
            # b ya salió de su 'with' pero a sigue adentro: NO debe haberse
            # restaurado todavía (refcount aún > 0).
            seen["argtypes_after_b_exit_while_a_still_in"] = fn.argtypes

        ta = threading.Thread(target=worker_a)
        tb = threading.Thread(target=worker_b)
        try:
            ta.start()
            tb.start()
            tb.join(timeout=5)
            release_a.set()
            ta.join(timeout=5)

            assert seen["a"] is None
            assert seen["b"] is None
            assert seen["argtypes_after_b_exit_while_a_still_in"] is None
            assert fn.argtypes == original_argtypes
            assert ut._crypt32_argtypes_refcount == 0
        finally:
            release_a.set()
            ta.join(timeout=2)
            tb.join(timeout=2)
            fn.argtypes = original_argtypes
            ut._crypt32_argtypes_refcount = 0

    def test_sequential_entries_restore_correctly(self):
        """Entradas secuenciales (no anidadas) también restauran sin AttributeError."""
        import ctypes

        fn = ctypes.windll.crypt32.CryptUnprotectData
        original_argtypes = fn.argtypes

        with _clean_crypt32_argtypes():
            assert fn.argtypes is None
        assert fn.argtypes == original_argtypes

        with _clean_crypt32_argtypes():
            assert fn.argtypes is None
        assert fn.argtypes == original_argtypes
        assert ut._crypt32_argtypes_refcount == 0

    def test_many_threads_no_attribute_error_and_final_restore(self):
        """N hilos entrando/saliendo en cualquier orden: nunca AttributeError,
        y al final (todos fuera) los argtypes quedan restaurados."""
        import ctypes

        fn = ctypes.windll.crypt32.CryptUnprotectData
        original_argtypes = fn.argtypes
        errors = []

        def worker():
            try:
                for _ in range(20):
                    with _clean_crypt32_argtypes():
                        pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert errors == []
            assert fn.argtypes == original_argtypes
            assert ut._crypt32_argtypes_refcount == 0
        finally:
            fn.argtypes = original_argtypes
            ut._crypt32_argtypes_refcount = 0


# ---------------------------------------------------------------------------
# Documental — el diccionario ya se aplica vía Transcriber, no al final
# ---------------------------------------------------------------------------

class TestAudioRouteDictionaryContract:
    def test_audio_route_applies_dictionary_via_transcriber(self, monkeypatch):
        """Documenta que la ruta de AUDIO ya aplica el diccionario personal
        DENTRO de Transcriber.transcribe (core/transcriber.py:221), una vez
        por chunk. La auditoría había propuesto volver a aplicarlo al final
        de _transcribe_pcm_chunked (sobre el texto ya ensamblado); se
        descartó en el debate porque duplicaría la aplicación del
        diccionario (doble reemplazo sobre texto que ya fue reemplazado).
        """
        from core import dictionary
        from core.transcriber import Transcriber

        calls = []

        def spy_apply_replacements(text):
            calls.append(text)
            return text.upper()  # marca visible de que pasó por aquí

        monkeypatch.setattr(dictionary, "apply_replacements", spy_apply_replacements)

        class _FakeBackend:
            def __init__(self):
                self.n = 0

            def transcribe(self, wav_buffer, language=None, prompt=None):
                self.n += 1
                return f"texto crudo {self.n}"

        fake_backend = _FakeBackend()
        monkeypatch.setattr(Transcriber, "_get_backend", lambda self: fake_backend)
        monkeypatch.setattr(ut, "_CHUNK_SECONDS", 1)
        monkeypatch.setattr(ut, "_OVERLAP_SECONDS", 0)

        transcriber = Transcriber()
        pcm = np.zeros(2 * ut._SAMPLE_RATE, dtype=np.int16)

        text = _transcribe_pcm_chunked(pcm, transcriber, on_progress=None)

        assert fake_backend.n == 2
        # Exactamente una vez POR CHUNK — nunca una vez extra al final sobre
        # el texto ya ensamblado (eso sería doble reemplazo).
        assert len(calls) == 2
        assert text == "TEXTO CRUDO 1 TEXTO CRUDO 2"
