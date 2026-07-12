"""Tests para la unidad 5.2 'Contexto personal en prompts' (identidad del usuario).

Cubre, SIN llamar a ningún LLM real (``insights._chat``/``insights.chat_memory``
monkeypatcheados, igual que tests/test_meeting_templates.py y tests/test_ops_briefing.py):

  - core.insights.user_identity_line(): "" si USER_NAME vacío; formato con
    USER_ROLE/USER_DOMAIN opcionales; coletilla anti-atribución siempre presente.
  - core.insights.update_state (Insight Stream EN VIVO): la línea se inyecta SOLO
    si el modo proactivo no es "silent" (mismo gate de privacidad que el briefing
    OPS); ausente con USER_NAME vacío.
  - core.insights.generate_minutes (acta batch): la línea se inyecta SIEMPRE que
    USER_NAME esté seteado (sin gate de modo — la acta ya es persistida).
  - core.assistant.answer / answer_live: la línea se inyecta SIEMPRE que USER_NAME
    esté seteado (sin gate de modo, distinto del briefing OPS).
  - Sincronía de config.ENV_CATALOG: cubierta genéricamente por
    tests/test_env_catalog.py (AST scan de todos los os.getenv literales).
"""
import json

import pytest

from core import assistant, insights
from core.meeting import MeetingSession
from db.database import TranscriptionDB

_IDENTITY_ENV_KEYS = ("USER_NAME", "USER_ROLE", "USER_DOMAIN")


@pytest.fixture(autouse=True)
def _clean_identity_env(monkeypatch):
    """Aislamiento: cada test arranca sin USER_NAME/USER_ROLE/USER_DOMAIN heredadas
    de un .env real de desarrollo (mismo patrón que test_insights_task_split.py)."""
    for k in _IDENTITY_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PROACTIVE_MODE", "copilot")


_ANTI_ATTRIBUTION = 'NO atribuyas nada a nadie sin evidencia explícita en el transcript'


# ---------------------------------------------------------------------------
# core.insights.user_identity_line()
# ---------------------------------------------------------------------------

class TestUserIdentityLine:
    def test_empty_when_no_name(self):
        assert insights.user_identity_line() == ""

    def test_role_and_domain_alone_do_not_activate(self, monkeypatch):
        monkeypatch.setenv("USER_ROLE", "Fundador")
        monkeypatch.setenv("USER_DOMAIN", "SaaS ambiental")
        assert insights.user_identity_line() == ""

    def test_name_only(self, monkeypatch):
        monkeypatch.setenv("USER_NAME", "Johann")
        line = insights.user_identity_line()
        assert "Johann" in line
        assert "El usuario se llama Johann" in line
        assert _ANTI_ATTRIBUTION in line

    def test_name_role_and_domain(self, monkeypatch):
        monkeypatch.setenv("USER_NAME", "Johann")
        monkeypatch.setenv("USER_ROLE", "Fundador")
        monkeypatch.setenv("USER_DOMAIN", "SaaS ambiental")
        line = insights.user_identity_line()
        assert "Johann" in line
        assert "Fundador" in line
        assert "SaaS ambiental" in line
        assert _ANTI_ATTRIBUTION in line

    def test_anti_attribution_coletilla_always_present_when_active(self, monkeypatch):
        monkeypatch.setenv("USER_NAME", "Ana")
        assert _ANTI_ATTRIBUTION in insights.user_identity_line()


# ---------------------------------------------------------------------------
# core.insights.update_state — Insight Stream EN VIVO (gate por modo)
# ---------------------------------------------------------------------------

_VALID_STATE_JSON = json.dumps({
    "temas": [], "pendientes": [], "propuestas": [], "citas": [],
})


def _fake_chat_capture(monkeypatch):
    captured = {}

    def _fake_chat(messages, **kwargs):
        captured["system"] = messages[0]["content"]
        return _VALID_STATE_JSON

    monkeypatch.setattr(insights, "_chat", _fake_chat)
    monkeypatch.setattr(insights, "is_available", lambda task="live": True)
    return captured


class TestUpdateStateInjection:
    def test_identity_present_in_copilot_mode(self, monkeypatch):
        monkeypatch.setenv("USER_NAME", "Johann")
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        captured = _fake_chat_capture(monkeypatch)
        insights.update_state({"temas": [], "pendientes": [], "propuestas": [], "citas": []},
                               "algo de transcript nuevo")
        assert "Johann" in captured["system"]
        assert _ANTI_ATTRIBUTION in captured["system"]

    def test_identity_absent_in_silent_mode(self, monkeypatch):
        monkeypatch.setenv("USER_NAME", "Johann")
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        captured = _fake_chat_capture(monkeypatch)
        insights.update_state({"temas": [], "pendientes": [], "propuestas": [], "citas": []},
                               "algo de transcript nuevo")
        assert "Johann" not in captured["system"]

    def test_identity_absent_when_name_empty(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        captured = _fake_chat_capture(monkeypatch)
        insights.update_state({"temas": [], "pendientes": [], "propuestas": [], "citas": []},
                               "algo de transcript nuevo")
        assert _ANTI_ATTRIBUTION not in captured["system"]


# ---------------------------------------------------------------------------
# core.insights.generate_minutes — acta batch (sin gate de modo)
# ---------------------------------------------------------------------------

_SEGMENTS = [{"t": 10.0, "time": "00:10", "speaker": "Yo", "text": "hola"}]
_TRANSCRIPT = "[00:10 Yo] hola"
_MINUTES_JSON = json.dumps({
    "resumen": "ok", "decisiones": [], "temas": [], "pendientes": [],
    "propuestas": [], "citas": [],
})


def _fake_minutes_chat_capture(monkeypatch):
    captured = {}

    def _fake_chat(messages, **kwargs):
        captured["system"] = messages[0]["content"]
        return _MINUTES_JSON

    monkeypatch.setattr(insights, "_chat", _fake_chat)
    monkeypatch.setattr(insights, "is_available", lambda task="live": True)
    return captured


class TestGenerateMinutesInjection:
    def test_identity_present_when_name_set_even_in_silent_mode(self, monkeypatch):
        """SIEMPRE que USER_NAME esté seteado (el acta persistida no gatea por modo,
        a diferencia del Insight Stream en vivo)."""
        monkeypatch.setenv("USER_NAME", "Johann")
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        captured = _fake_minutes_chat_capture(monkeypatch)
        insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        assert "Johann" in captured["system"]
        assert _ANTI_ATTRIBUTION in captured["system"]

    def test_identity_absent_when_name_empty(self, monkeypatch):
        captured = _fake_minutes_chat_capture(monkeypatch)
        insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        assert _ANTI_ATTRIBUTION not in captured["system"]


# ---------------------------------------------------------------------------
# core.assistant.answer — chat BATCH sobre DB persistida
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    return TranscriptionDB(db_path=str(tmp_path / "test.db"))


class TestAnswerBatchInjection:
    def test_identity_present_when_name_set(self, db, monkeypatch):
        monkeypatch.setenv("USER_NAME", "Johann")
        captured = {}

        def _fake_chat_memory(messages, **kw):
            captured["system"] = messages[0]["content"]
            return "[stub]"

        monkeypatch.setattr(insights, "chat_memory", _fake_chat_memory)
        result = assistant.answer(db, "¿Qué se habló?")
        assert result["ok"] is True
        assert "Johann" in captured["system"]
        assert _ANTI_ATTRIBUTION in captured["system"]

    def test_identity_absent_when_name_empty(self, db, monkeypatch):
        captured = {}

        def _fake_chat_memory(messages, **kw):
            captured["system"] = messages[0]["content"]
            return "[stub]"

        monkeypatch.setattr(insights, "chat_memory", _fake_chat_memory)
        result = assistant.answer(db, "¿Qué se habló?")
        assert result["ok"] is True
        assert _ANTI_ATTRIBUTION not in captured["system"]
        # El resto del system prompt sigue intacto (no se rompió nada aditivo).
        assert assistant.ASSISTANT_SYSTEM[:30] in captured["system"]


# ---------------------------------------------------------------------------
# core.assistant.answer_live — chat EN VIVO (sin gate de modo, a diferencia
# del briefing OPS)
# ---------------------------------------------------------------------------

def _make_meeting(segments=None, insights_state=None, active=True):
    m = MeetingSession()
    m._active = active
    m._started_at = "2026-07-12 10:00:00"
    m._segments = list(segments or [])
    if insights_state is not None:
        m._insights = dict(insights_state)
    return m


def _seg(t, speaker, text):
    return {"t": float(t), "speaker": speaker, "text": text}


_EMPTY_LIVE_INSIGHTS = {"temas": [], "pendientes": [], "propuestas": [], "citas": []}


class TestAnswerLiveInjection:
    def test_identity_present_when_name_set(self, monkeypatch):
        monkeypatch.setenv("USER_NAME", "Johann")
        captured = {}

        def _fake_chat_memory(messages, **kw):
            captured["system"] = messages[0]["content"]
            return "ok"

        monkeypatch.setattr(insights, "chat_memory", _fake_chat_memory)
        m = _make_meeting([_seg(5, "Yo", "hola equipo")], _EMPTY_LIVE_INSIGHTS)
        res = assistant.answer_live("q", meeting=m)
        assert res["ok"] is True
        assert "Johann" in captured["system"]
        assert _ANTI_ATTRIBUTION in captured["system"]

    def test_identity_present_even_in_silent_mode(self, monkeypatch):
        """A diferencia del briefing OPS, la identidad del usuario NO se gatea por
        el modo proactivo en el chat (solo el Insight Stream lo hace)."""
        monkeypatch.setenv("USER_NAME", "Johann")
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        captured = {}

        def _fake_chat_memory(messages, **kw):
            captured["system"] = messages[0]["content"]
            return "ok"

        monkeypatch.setattr(insights, "chat_memory", _fake_chat_memory)
        m = _make_meeting([_seg(5, "Yo", "hola equipo")], _EMPTY_LIVE_INSIGHTS)
        res = assistant.answer_live("q", meeting=m)
        assert res["ok"] is True
        assert "Johann" in captured["system"]

    def test_identity_absent_when_name_empty(self, monkeypatch):
        captured = {}

        def _fake_chat_memory(messages, **kw):
            captured["system"] = messages[0]["content"]
            return "ok"

        monkeypatch.setattr(insights, "chat_memory", _fake_chat_memory)
        m = _make_meeting([_seg(5, "Yo", "hola equipo")], _EMPTY_LIVE_INSIGHTS)
        res = assistant.answer_live("q", meeting=m)
        assert res["ok"] is True
        assert _ANTI_ATTRIBUTION not in captured["system"]
