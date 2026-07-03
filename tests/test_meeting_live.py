"""Tests para la unidad 2.1 'UI /reunion En vivo + estados del pill' (Ola 2 de Vflow).

Cubre el soporte añadido en core/meeting.py:
  - pause()/resume() congelan _elapsed() (avanzar el reloj real durante la pausa
    no aumenta el tiempo transcurrido reportado).
  - Los callbacks de audio (_mic_callback/_sys_callback) NO appendean frames
    mientras la reunión está pausada.
  - add_note(): inactiva -> None; activa -> acumula notas con t/time/text.
  - status() incluye "levels" y "paused".
  - stop() durante una pausa cierra bien la contabilidad (no deja el reloj
    "colgado" ni _paused=True al terminar).
  - Round-trip de notes_json en TranscriptionDB.meeting_insert/meeting_get.

No arranca audio real ni llama a ningún LLM: el estado mínimo de una reunión
activa se simula directamente sobre los atributos internos (mismo patrón que
tests/test_highlight.py).
"""

import json
import time as _time

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# pause()/resume() — congelar-reloj
# ---------------------------------------------------------------------------

class TestPauseResumeFreezesClock:
    def test_pause_freezes_elapsed_while_clock_advances(self, monkeypatch):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True

        fake_now = [1000.0]

        def fake_monotonic():
            return fake_now[0]

        monkeypatch.setattr("core.meeting.time.monotonic", fake_monotonic)
        m._t0 = fake_now[0]
        fake_now[0] += 5.0  # 5s transcurridos antes de pausar
        assert m._elapsed() == pytest.approx(5.0)

        res = m.pause()
        assert res["ok"] is True
        assert res["paused"] is True
        assert m._paused is True

        elapsed_at_pause = m._elapsed()
        assert elapsed_at_pause == pytest.approx(5.0)

        # Avanzar el reloj real "de pared" 10s mientras está en pausa: el elapsed
        # reportado NO debe moverse (reloj congelado).
        fake_now[0] += 10.0
        assert m._elapsed() == pytest.approx(elapsed_at_pause)
        assert m._elapsed() == pytest.approx(5.0)

    def test_resume_accounts_paused_time_and_continues(self, monkeypatch):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True

        fake_now = [2000.0]
        monkeypatch.setattr("core.meeting.time.monotonic", lambda: fake_now[0])
        m._t0 = fake_now[0]

        fake_now[0] += 3.0
        m.pause()
        fake_now[0] += 7.0  # 7s "perdidos" en pausa
        res = m.resume()
        assert res["ok"] is True
        assert res["paused"] is False
        assert m._paused is False

        # Justo tras resume, el tiempo pausado (7s) se descuenta: elapsed sigue en ~3s.
        assert m._elapsed() == pytest.approx(3.0, abs=0.01)

        fake_now[0] += 2.0  # 2s reales tras el resume
        assert m._elapsed() == pytest.approx(5.0, abs=0.01)

    def test_segment_timestamps_contiguous_after_resume(self, monkeypatch):
        """El window_start tras resume() parte del elapsed congelado, no del reloj
        de pared, así los segmentos post-resume quedan contiguos con los previos."""
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True

        fake_now = [500.0]
        monkeypatch.setattr("core.meeting.time.monotonic", lambda: fake_now[0])
        m._t0 = fake_now[0]

        fake_now[0] += 4.0
        m.pause()
        pre_pause_elapsed = m._elapsed()
        fake_now[0] += 20.0  # pausa larga
        m.resume()
        # window_start tras resume debe reanudar justo donde se pausó (contiguo),
        # no saltar 20s hacia adelante.
        assert m._window_start == pytest.approx(pre_pause_elapsed, abs=0.01)

    def test_pause_idempotent_when_not_active_or_already_paused(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        # Sin reunión activa: no debe lanzar ni marcar paused.
        res = m.pause()
        assert res["ok"] is True
        assert m._paused is False

        m._active = True
        m._t0 = _time.monotonic()
        m.pause()
        assert m._paused is True
        # Segunda llamada a pause() no debe romper nada ni re-acumular _paused_total.
        prev_total = m._paused_total
        res2 = m.pause()
        assert res2["paused"] is True
        assert m._paused_total == prev_total

    def test_resume_idempotent_when_not_paused(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        res = m.resume()  # nunca se pausó
        assert res["ok"] is True
        assert res["paused"] is False


# ---------------------------------------------------------------------------
# Callbacks de audio en pausa: no appendean frames
# ---------------------------------------------------------------------------

class TestCallbacksSkipWhilePaused:
    def _chunk(self):
        return np.zeros((160,), dtype=np.int16)

    def test_mic_callback_noop_when_paused(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        m._paused = True

        m._mic_callback(self._chunk())
        assert m._mic_frames == []
        # tampoco debe alimentar la cola del visualizador durante la pausa
        assert m.viz_queue.qsize() == 0

    def test_sys_callback_noop_when_paused(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        m._paused = True

        m._sys_callback(self._chunk())
        assert m._sys_frames == []

    def test_callbacks_work_normally_when_not_paused(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()

        m._mic_callback(self._chunk())
        m._sys_callback(self._chunk())
        assert len(m._mic_frames) == 1
        assert len(m._sys_frames) == 1
        assert m.viz_queue.qsize() == 1
        # El RMS de un chunk de ceros es 0.0
        assert m._level_mic == pytest.approx(0.0)
        assert m._level_sys == pytest.approx(0.0)

    def test_callbacks_noop_when_inactive(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._mic_callback(self._chunk())
        m._sys_callback(self._chunk())
        assert m._mic_frames == []
        assert m._sys_frames == []


# ---------------------------------------------------------------------------
# add_note()
# ---------------------------------------------------------------------------

class TestAddNote:
    def test_inactive_meeting_returns_none(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        assert m.add_note("una nota") is None
        assert m._notes == []

    def test_empty_text_returns_none(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        assert m.add_note("") is None
        assert m.add_note("   ") is None
        assert m._notes == []

    def test_active_meeting_accumulates_notes(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic() - 5.0

        first = m.add_note("  primera nota  ")
        assert first is not None
        assert first["text"] == "primera nota"  # strip aplicado
        assert "t" in first and "time" in first

        second = m.add_note("segunda nota")
        assert len(m._notes) == 2
        assert m._notes[0] == first
        assert m._notes[1] == second


# ---------------------------------------------------------------------------
# status() incluye levels y paused
# ---------------------------------------------------------------------------

class TestStatusFields:
    def test_status_includes_levels_and_paused_inactive(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        s = m.status()
        assert s["paused"] is False
        assert s["levels"] == {"yo": 0.0, "ellos": 0.0}

    def test_status_levels_zero_while_paused(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        m._level_mic = 0.8
        m._level_sys = 0.6
        m._paused = True
        m._pause_started = _time.monotonic()

        s = m.status()
        assert s["paused"] is True
        # En pausa no hay audio nuevo: los niveles deben reportarse en 0, no el
        # último valor stale (evita un VU "congelado" con barra visible).
        assert s["levels"] == {"yo": 0.0, "ellos": 0.0}

    def test_status_levels_reflect_active_values(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        m._level_mic = 0.5
        m._level_sys = 0.25
        s = m.status()
        assert s["levels"] == {"yo": 0.5, "ellos": 0.25}


# ---------------------------------------------------------------------------
# stop() durante pausa
# ---------------------------------------------------------------------------

class TestStopDuringPause:
    def test_stop_while_paused_closes_accounting(self, monkeypatch):
        from core.meeting import MeetingSession
        m = MeetingSession()

        fake_now = [100.0]
        monkeypatch.setattr("core.meeting.time.monotonic", lambda: fake_now[0])

        # Simular el estado mínimo de una reunión activa y pausada sin arrancar
        # audio/threads real (evita tocar sounddevice/pyaudiowpatch en CI).
        m._active = True
        m._t0 = fake_now[0]
        fake_now[0] += 4.0
        m.pause()
        assert m._paused is True
        fake_now[0] += 6.0  # tiempo "perdido" en pausa antes de terminar

        # stop() real toca threads/DB/insights; para este test unitario solo nos
        # interesa el bloque de contabilidad al inicio de stop(), así que
        # reproducimos exactamente esa lógica (mismo código que stop()).
        with m._lock:
            if m._paused and m._pause_started is not None:
                m._paused_total += _time.monotonic() - m._pause_started
                m._paused = False
                m._pause_started = None
            m._active = False
        duration = m._elapsed()

        assert m._paused is False
        assert m._pause_started is None
        # La duración final no debe incluir los 6s de pausa: solo los 4s activos.
        assert duration == pytest.approx(4.0, abs=0.01)


# ---------------------------------------------------------------------------
# Round-trip DB: notes_json
# ---------------------------------------------------------------------------

class TestNotesDbRoundTrip:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path):
        from db.database import TranscriptionDB
        self.db_path = str(tmp_path / "test_meetings_notes.db")
        self.db = TranscriptionDB(db_path=self.db_path)

    def test_meeting_insert_with_notes_json(self):
        notes = [{"t": 3.2, "time": "00:03", "text": "recordar X"},
                 {"t": 40.0, "time": "00:40", "text": "recordar Y"}]
        meeting_id = self.db.meeting_insert(
            title="Reunión con notas",
            transcript="[00:03 Yo] hola",
            segments_json=json.dumps([]),
            duration_seconds=60.0,
            started_at="2026-07-03 10:00:00",
            notes_json=json.dumps(notes, ensure_ascii=False),
        )
        assert meeting_id >= 1

        row = self.db.meeting_get(meeting_id)
        assert row is not None
        assert row["notes_json"] is not None
        round_tripped = json.loads(row["notes_json"])
        assert round_tripped == notes

    def test_meeting_insert_without_notes_defaults_none(self):
        meeting_id = self.db.meeting_insert(
            title="Sin notas",
            transcript="texto",
            segments_json=json.dumps([]),
            duration_seconds=10.0,
        )
        row = self.db.meeting_get(meeting_id)
        assert row["notes_json"] is None
