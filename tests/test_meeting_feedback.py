"""Tests para la unidad 2.2 'Push mínimo de pendientes ✓/✗ + persistencia de feedback' (Ola 2).

Cubre el soporte añadido en core/meeting.py:
  - add_feedback(): inactiva -> None; value inválido -> None; activa -> acumula;
    dedup por key -> reemplaza el value en vez de duplicar.
  - Round-trip de feedback_json en TranscriptionDB.meeting_insert/meeting_get.

No arranca audio real ni llama a ningún LLM: el estado mínimo de una reunión
activa se simula directamente sobre los atributos internos (mismo patrón que
tests/test_meeting_live.py).
"""

import json
import time as _time

import pytest


# ---------------------------------------------------------------------------
# add_feedback()
# ---------------------------------------------------------------------------

class TestAddFeedback:
    def test_inactive_meeting_returns_none(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        assert m.add_feedback("p123", "pendiente", "revisar contrato", 1) is None
        assert m._feedback == []

    def test_invalid_value_returns_none(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        assert m.add_feedback("p123", "pendiente", "revisar contrato", 0) is None
        assert m.add_feedback("p123", "pendiente", "revisar contrato", 2) is None
        assert m.add_feedback("p123", "pendiente", "revisar contrato", None) is None
        assert m._feedback == []

    def test_empty_key_returns_none(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()
        assert m.add_feedback("", "pendiente", "revisar contrato", 1) is None
        assert m.add_feedback("   ", "pendiente", "revisar contrato", -1) is None
        assert m._feedback == []

    def test_active_meeting_accumulates_feedback(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic() - 5.0

        first = m.add_feedback("p111", "pendiente", "revisar contrato", 1)
        assert first is not None
        assert first["key"] == "p111"
        assert first["tipo"] == "pendiente"
        assert first["texto"] == "revisar contrato"
        assert first["value"] == 1
        assert "t" in first and "time" in first

        second = m.add_feedback("p222", "pendiente", "enviar propuesta", -1)
        assert len(m._feedback) == 2
        assert m._feedback[0] == first
        assert m._feedback[1] == second

    def test_dedup_by_key_replaces_value_instead_of_duplicating(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        m._active = True
        m._t0 = _time.monotonic()

        m.add_feedback("p111", "pendiente", "revisar contrato", 1)
        assert len(m._feedback) == 1

        updated = m.add_feedback("p111", "pendiente", "revisar contrato", -1)
        assert len(m._feedback) == 1  # no duplica, reemplaza
        assert updated["value"] == -1
        assert m._feedback[0]["value"] == -1
        assert m._feedback[0]["key"] == "p111"


# ---------------------------------------------------------------------------
# Round-trip DB: feedback_json
# ---------------------------------------------------------------------------

class TestFeedbackDbRoundTrip:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path):
        from db.database import TranscriptionDB
        self.db_path = str(tmp_path / "test_meetings_feedback.db")
        self.db = TranscriptionDB(db_path=self.db_path)

    def test_meeting_insert_with_feedback_json(self):
        feedback = [
            {"key": "p111", "tipo": "pendiente", "texto": "revisar contrato", "value": 1, "t": 3.2, "time": "00:03"},
            {"key": "p222", "tipo": "pendiente", "texto": "enviar propuesta", "value": -1, "t": 40.0, "time": "00:40"},
        ]
        meeting_id = self.db.meeting_insert(
            title="Reunión con feedback",
            transcript="[00:03 Yo] hola",
            segments_json=json.dumps([]),
            duration_seconds=60.0,
            started_at="2026-07-03 10:00:00",
            feedback_json=json.dumps(feedback, ensure_ascii=False),
        )
        assert meeting_id >= 1

        row = self.db.meeting_get(meeting_id)
        assert row is not None
        assert row["feedback_json"] is not None
        round_tripped = json.loads(row["feedback_json"])
        assert round_tripped == feedback

    def test_meeting_insert_without_feedback_defaults_none(self):
        meeting_id = self.db.meeting_insert(
            title="Sin feedback",
            transcript="texto",
            segments_json=json.dumps([]),
            duration_seconds=10.0,
        )
        row = self.db.meeting_get(meeting_id)
        assert row["feedback_json"] is None
