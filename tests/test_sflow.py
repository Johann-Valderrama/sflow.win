"""Tests for Vflow core modules."""

import io
import json
import os
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# TestTranscriptionDB
# ---------------------------------------------------------------------------
class TestTranscriptionDB:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path):
        from db.database import TranscriptionDB
        self.db_path = str(tmp_path / "test.db")
        self.db = TranscriptionDB(db_path=self.db_path)

    def test_insert_and_get_recent(self):
        tid = self.db.insert(text="hello world", duration_seconds=1.5)
        assert tid >= 1
        rows = self.db.get_recent(limit=10)
        assert len(rows) == 1
        assert rows[0]["text"] == "hello world"
        assert rows[0]["duration_seconds"] == 1.5

    def test_delete_by_id(self):
        tid = self.db.insert(text="to delete")
        assert self.db.delete_by_id(tid) == 1
        assert self.db.get_recent() == []

    def test_delete_nonexistent_id(self):
        assert self.db.delete_by_id(99999) == 0

    def test_delete_by_ids_empty_list(self):
        assert self.db.delete_by_ids([]) == 0

    def test_delete_by_ids(self):
        id1 = self.db.insert(text="one")
        id2 = self.db.insert(text="two")
        id3 = self.db.insert(text="three")
        assert self.db.delete_by_ids([id1, id3]) == 2
        rows = self.db.get_recent()
        assert len(rows) == 1
        assert rows[0]["id"] == id2

    def test_update_text(self):
        tid = self.db.insert(text="old text")
        assert self.db.update_text(tid, "new text") == 1
        rows = self.db.get_recent()
        assert rows[0]["text"] == "new text"

    def test_update_nonexistent(self):
        assert self.db.update_text(99999, "nope") == 0

    def test_corrupt_db_recovery(self, tmp_path):
        corrupt_path = str(tmp_path / "corrupt.db")
        with open(corrupt_path, "wb") as f:
            f.write(b"this is not a valid sqlite database")
        from db.database import TranscriptionDB
        db = TranscriptionDB(db_path=corrupt_path)
        # Should have recovered and created a fresh DB
        tid = db.insert(text="recovered")
        assert tid >= 1


# ---------------------------------------------------------------------------
# TestAudioRecorder
# ---------------------------------------------------------------------------
class TestAudioRecorder:
    @pytest.fixture(autouse=True)
    def recorder(self):
        from core.recorder import AudioRecorder
        self.recorder = AudioRecorder()

    def test_initial_state(self):
        assert self.recorder.is_recording is False
        assert self.recorder.frames == []

    def test_stop_without_start(self):
        duration = self.recorder.stop()
        assert duration == 0.0

    def test_get_wav_buffer_empty(self):
        buf = self.recorder.get_wav_buffer()
        assert isinstance(buf, io.BytesIO)
        assert buf.read() == b""

    @patch("core.recorder.sd.InputStream")
    def test_start_stop_lifecycle(self, mock_stream_cls):
        mock_stream = MagicMock()
        mock_stream_cls.return_value = mock_stream
        self.recorder.start()
        assert self.recorder.is_recording is True
        mock_stream.start.assert_called_once()
        # Simulate some audio data
        fake_audio = np.zeros((1024, 1), dtype=np.int16)
        self.recorder._callback(fake_audio, 1024, None, None)
        duration = self.recorder.stop()
        assert self.recorder.is_recording is False
        assert duration > 0
        mock_stream.stop.assert_called_once()

    @patch("core.recorder.sd.InputStream")
    def test_start_failure_no_mic(self, mock_stream_cls):
        import sounddevice as sd
        mock_stream_cls.side_effect = sd.PortAudioError("No device")
        with pytest.raises(sd.PortAudioError):
            self.recorder.start()

    @patch("core.recorder.sd.InputStream")
    def test_no_recording_limit(self, mock_stream_cls):
        """Verify there is no max frame limit — unlimited recording via chunks."""
        mock_stream = MagicMock()
        mock_stream_cls.return_value = mock_stream
        self.recorder.start()
        fake_audio = np.zeros((1024, 1), dtype=np.int16)
        # Add many frames — should all be stored (no limit)
        for _ in range(5000):
            self.recorder._callback(fake_audio, 1024, None, None)
        assert len(self.recorder.frames) == 5000

    def test_extract_chunk_empty(self):
        """extract_chunk returns None when no frames."""
        assert self.recorder.extract_chunk() is None

    def test_extract_chunk_returns_wav_and_keeps_overlap(self):
        """extract_chunk returns WAV data and keeps overlap frames."""
        from config import SAMPLE_RATE, BLOCK_SIZE, CHUNK_OVERLAP_SECONDS

        overlap_frame_count = int(CHUNK_OVERLAP_SECONDS * SAMPLE_RATE / BLOCK_SIZE)
        total_frames = overlap_frame_count + 50  # 50 frames beyond overlap

        fake_audio = np.ones((BLOCK_SIZE, 1), dtype=np.int16)
        for i in range(total_frames):
            self.recorder.frames.append(fake_audio * (i + 1))

        chunk_buf = self.recorder.extract_chunk()
        assert chunk_buf is not None

        # Should have extracted frames and left overlap
        assert len(self.recorder.frames) == overlap_frame_count

        # Verify returned buffer is valid WAV
        import wave
        chunk_buf.seek(0)
        with wave.open(chunk_buf, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getframerate() == SAMPLE_RATE
            # Should contain the non-overlap frames
            expected_samples = 50 * BLOCK_SIZE
            assert wf.getnframes() == expected_samples

    def test_extract_chunk_too_few_frames(self):
        """extract_chunk returns None if only overlap frames exist."""
        from config import SAMPLE_RATE, BLOCK_SIZE, CHUNK_OVERLAP_SECONDS

        overlap_frame_count = int(CHUNK_OVERLAP_SECONDS * SAMPLE_RATE / BLOCK_SIZE)
        fake_audio = np.zeros((BLOCK_SIZE, 1), dtype=np.int16)
        for _ in range(overlap_frame_count):
            self.recorder.frames.append(fake_audio)

        assert self.recorder.extract_chunk() is None
        # Frames should be untouched
        assert len(self.recorder.frames) == overlap_frame_count


# ---------------------------------------------------------------------------
# TestTranscriber
# ---------------------------------------------------------------------------
class TestTranscriber:
    @pytest.fixture(autouse=True)
    def transcriber(self):
        from core.transcriber import Transcriber
        self.transcriber = Transcriber()

    def test_empty_buffer(self):
        buf = io.BytesIO(b"")
        result = self.transcriber.transcribe(buf)
        assert result == ""

    def test_tiny_buffer(self):
        buf = io.BytesIO(b"x" * 50)
        result = self.transcriber.transcribe(buf)
        assert result == ""

    @patch.dict(os.environ, {"GROQ_API_KEY": ""})
    def test_missing_api_key(self):
        from core.transcriber import Transcriber
        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            t.transcribe(buf)

    @patch("core.transcriber.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_successful_transcription(self, mock_groq_cls):
        from core.transcriber import Transcriber
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_client.audio.transcriptions.create.return_value = "  Hello world  "
        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        result = t.transcribe(buf)
        assert result == "Hello world"

    @patch("core.transcriber.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_api_error(self, mock_groq_cls):
        from core.transcriber import Transcriber
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_client.audio.transcriptions.create.side_effect = RuntimeError("API error")
        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        with pytest.raises(RuntimeError):
            t.transcribe(buf)

    @patch("core.transcriber.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_transcribe_with_prompt(self, mock_groq_cls):
        """Verify prompt parameter is passed to the API call."""
        from core.transcriber import Transcriber
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_client.audio.transcriptions.create.return_value = "continued text"
        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        result = t.transcribe(buf, prompt="previous context")
        assert result == "continued text"
        call_kwargs = mock_client.audio.transcriptions.create.call_args
        assert call_kwargs.kwargs.get("prompt") == "previous context" or \
               (call_kwargs[1].get("prompt") == "previous context")

    @patch("core.transcriber.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_transcribe_without_prompt(self, mock_groq_cls):
        """Verify prompt is NOT passed when None."""
        from core.transcriber import Transcriber
        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client
        mock_client.audio.transcriptions.create.return_value = "hello"
        t = Transcriber()
        buf = io.BytesIO(b"x" * 200)
        t.transcribe(buf)
        call_kwargs = mock_client.audio.transcriptions.create.call_args
        # prompt should not be in the kwargs
        assert "prompt" not in (call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1])


# ---------------------------------------------------------------------------
# TestHotkeyListener
# ---------------------------------------------------------------------------
class TestHotkeyListener:
    @pytest.fixture(autouse=True)
    def listener(self):
        from core.hotkey import HotkeyListener
        self.listener = HotkeyListener()
        self.pressed_count = 0
        self.released_count = 0
        self.listener.pressed.connect(lambda: self._inc_pressed())
        self.listener.released.connect(lambda: self._inc_released())

    def _inc_pressed(self):
        self.pressed_count += 1

    def _inc_released(self):
        self.released_count += 1

    def test_initial_state(self):
        assert self.listener._recording is False
        assert self.listener._hands_free is False

    def test_hold_mode_ctrl_alt(self):
        from pynput import keyboard
        self.listener._on_press(keyboard.Key.ctrl_l)
        self.listener._on_press(keyboard.Key.alt_l)
        assert self.listener._recording is True
        assert self.pressed_count == 1

    def test_hold_mode_release(self):
        from pynput import keyboard
        self.listener._on_press(keyboard.Key.ctrl_l)
        self.listener._on_press(keyboard.Key.alt_l)
        self.listener._on_release(keyboard.Key.ctrl_l)
        assert self.listener._recording is False
        assert self.released_count == 1

    def test_double_tap_hands_free(self):
        from pynput import keyboard
        self.listener._on_press(keyboard.Key.ctrl_l)
        self.listener._on_release(keyboard.Key.ctrl_l)
        self.listener._on_press(keyboard.Key.ctrl_l)
        assert self.listener._hands_free is True
        assert self.listener._recording is True
        assert self.pressed_count == 1

    def test_hands_free_stop_on_ctrl(self):
        from pynput import keyboard
        # Start hands-free
        self.listener._on_press(keyboard.Key.ctrl_l)
        self.listener._on_release(keyboard.Key.ctrl_l)
        self.listener._on_press(keyboard.Key.ctrl_l)
        self.listener._on_release(keyboard.Key.ctrl_l)
        # Stop hands-free with another Ctrl press
        import time
        time.sleep(0.5)  # outside double-tap window
        self.listener._on_press(keyboard.Key.ctrl_l)
        assert self.listener._recording is False
        assert self.released_count == 1

    def test_ctrl_autorepeat_ignored(self):
        from pynput import keyboard
        self.listener._on_press(keyboard.Key.ctrl_l)
        # Simulate auto-repeat (press while already held)
        self.listener._on_press(keyboard.Key.ctrl_l)
        self.listener._on_press(keyboard.Key.ctrl_l)
        # Should not have triggered anything extra
        assert self.pressed_count == 0  # no alt held, no double tap


# ---------------------------------------------------------------------------
# TestFlaskEndpoints
# ---------------------------------------------------------------------------
try:
    import flask as _flask
    _has_flask = True
except ImportError:
    _has_flask = False


@pytest.mark.skipif(not _has_flask, reason="flask not installed")
class TestFlaskEndpoints:
    @pytest.fixture(autouse=True)
    def client(self, tmp_path):
        from web.server import app, _db
        # Use a temp DB for tests
        _db.db_path = str(tmp_path / "test.db")
        _db._init_db()
        self.app = app
        self.client = app.test_client()
        self.db = _db

    def test_get_transcriptions_empty(self):
        resp = self.client.get("/api/transcriptions")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_get_transcriptions(self):
        self.db.insert(text="test entry")
        resp = self.client.get("/api/transcriptions")
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["text"] == "test entry"

    def test_delete_single(self):
        tid = self.db.insert(text="to delete")
        resp = self.client.delete(f"/api/transcriptions/{tid}")
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 1

    def test_delete_batch(self):
        id1 = self.db.insert(text="one")
        id2 = self.db.insert(text="two")
        resp = self.client.post(
            "/api/transcriptions/delete-batch",
            json={"ids": [id1, id2]},
        )
        assert resp.status_code == 200
        assert resp.get_json()["deleted"] == 2

    def test_delete_batch_invalid_ids(self):
        resp = self.client.post(
            "/api/transcriptions/delete-batch",
            json={"ids": ["not_a_number"]},
        )
        assert resp.status_code == 400
        assert "integers" in resp.get_json()["error"]

    def test_delete_batch_missing_ids(self):
        resp = self.client.post(
            "/api/transcriptions/delete-batch",
            json={},
        )
        assert resp.status_code == 400

    def test_delete_bulk_invalid_date(self):
        resp = self.client.delete("/api/transcriptions?range=day&date=not-a-date")
        assert resp.status_code == 400

    def test_update_transcription(self):
        tid = self.db.insert(text="old")
        resp = self.client.put(
            f"/api/transcriptions/{tid}",
            json={"text": "new"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_update_nonexistent(self):
        resp = self.client.put(
            "/api/transcriptions/99999",
            json={"text": "nope"},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestChunkedTranscription
# ---------------------------------------------------------------------------
class TestChunkedTranscription:
    """Integration-style tests for the chunked transcription flow."""

    @patch("core.transcriber.Groq")
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk_test_key_12345678901234567890"})
    def test_multi_chunk_join(self, mock_groq_cls):
        """Simulate multiple chunks being transcribed and joined."""
        from core.transcriber import Transcriber
        from core.recorder import AudioRecorder
        from config import SAMPLE_RATE, BLOCK_SIZE, CHUNK_OVERLAP_SECONDS

        mock_client = MagicMock()
        mock_groq_cls.return_value = mock_client

        transcriber = Transcriber()
        recorder = AudioRecorder()

        # Simulate enough frames for a chunk extraction
        overlap_count = int(CHUNK_OVERLAP_SECONDS * SAMPLE_RATE / BLOCK_SIZE)
        num_frames = overlap_count + 100
        fake_audio = np.zeros((BLOCK_SIZE, 1), dtype=np.int16)
        for _ in range(num_frames):
            recorder.frames.append(fake_audio.copy())

        # Extract first chunk
        mock_client.audio.transcriptions.create.return_value = "first chunk text"
        chunk1_buf = recorder.extract_chunk()
        assert chunk1_buf is not None
        text1 = transcriber.transcribe(chunk1_buf)
        assert text1 == "first chunk text"

        # Add more frames for second chunk
        for _ in range(num_frames):
            recorder.frames.append(fake_audio.copy())

        # Extract second chunk with prompt context
        mock_client.audio.transcriptions.create.return_value = "second chunk text"
        chunk2_buf = recorder.extract_chunk()
        assert chunk2_buf is not None
        text2 = transcriber.transcribe(chunk2_buf, prompt=text1[-200:])
        assert text2 == "second chunk text"

        # Verify prompt was passed
        last_call = mock_client.audio.transcriptions.create.call_args
        assert last_call.kwargs.get("prompt") == "first chunk text" or \
               last_call[1].get("prompt") == "first chunk text"

        # Final: get remaining frames
        mock_client.audio.transcriptions.create.return_value = "final text"
        final_buf = recorder.get_wav_buffer()
        text3 = transcriber.transcribe(final_buf, prompt=text2[-200:])

        # Join all texts
        all_texts = [text1, text2, text3]
        full = " ".join(all_texts)
        assert full == "first chunk text second chunk text final text"
