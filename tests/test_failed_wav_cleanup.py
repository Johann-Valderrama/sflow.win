"""Tests de la unidad 1.2: restos en disco de last_failed_recording.wav.

Cubre las dos funciones puras extraídas de main.py:
- `_cleanup_stale_failed_wav`: TTL al arrancar (>24h se borra, reciente no).
- `_cleanup_failed_wav`: borrado tras el siguiente dictado exitoso.

Se testea directamente sobre archivos temporales (sin instanciar VflowApp
completo), igual que test_chunk_assembly.py hace con _join_pending_chunks.
"""

import os
import time

from main import _cleanup_stale_failed_wav, _cleanup_failed_wav


class TestCleanupStaleFailedWav:
    def test_old_wav_is_deleted(self, tmp_path):
        wav_path = str(tmp_path / "last_failed_recording.wav")
        with open(wav_path, "wb") as f:
            f.write(b"fake wav bytes")
        # Simular mtime de hace 25 horas (> TTL de 24h)
        old_time = time.time() - (25 * 3600)
        os.utime(wav_path, (old_time, old_time))

        _cleanup_stale_failed_wav(path=wav_path, max_age_hours=24)

        assert not os.path.exists(wav_path)

    def test_recent_wav_is_not_deleted(self, tmp_path):
        wav_path = str(tmp_path / "last_failed_recording.wav")
        with open(wav_path, "wb") as f:
            f.write(b"fake wav bytes")
        # mtime reciente (justo creado, muy por debajo del TTL)

        _cleanup_stale_failed_wav(path=wav_path, max_age_hours=24)

        assert os.path.exists(wav_path)

    def test_missing_wav_is_a_noop(self, tmp_path):
        wav_path = str(tmp_path / "does_not_exist.wav")
        # No debe lanzar excepción si el archivo no existe.
        _cleanup_stale_failed_wav(path=wav_path, max_age_hours=24)
        assert not os.path.exists(wav_path)


class TestCleanupFailedWav:
    def test_deletes_existing_wav_on_success(self, tmp_path):
        wav_path = str(tmp_path / "last_failed_recording.wav")
        with open(wav_path, "wb") as f:
            f.write(b"fake wav bytes")

        _cleanup_failed_wav(path=wav_path)

        assert not os.path.exists(wav_path)

    def test_missing_wav_is_a_noop(self, tmp_path):
        wav_path = str(tmp_path / "does_not_exist.wav")
        # No debe lanzar excepción si no hay WAV fallido pendiente (caso común:
        # el dictado anterior tuvo éxito, nunca se escribió el archivo).
        _cleanup_failed_wav(path=wav_path)
        assert not os.path.exists(wav_path)
