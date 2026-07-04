"""Tests para la unidad 4.3 'Plantillas por tipo de reunión' (Ola 4).

Cubre, SIN llamar a ningún LLM real (``insights._chat`` monkeypatcheado):
  - TEMPLATES bien formadas: exactamente 4, campos completos, prompts compactos.
  - MeetingSession.set_template: válido aplica, inválido no aplica (conserva previo).
  - start() usa la plantilla activa y NO la resetea entre reuniones del proceso.
  - status() expone template/template_label.
  - generate_minutes con template="ventas" añade el bloque BANT al system prompt;
    sin template, el system es idéntico al de antes (_MINUTES_SYSTEM sin cambios).
  - La clave "bant" devuelta por el LLM sobrevive el post-proceso.
  - Round-trip de la columna `template` en la DB (meeting_insert/meeting_get).
"""

import json

import pytest

from core import insights
from core import meeting_templates as templates
from core.meeting import MeetingSession


_SEGMENTS = [
    {"t": 10.0, "time": "00:10", "speaker": "Yo", "text": "hola"},
    {"t": 65.0, "time": "01:05", "speaker": "Ellos", "text": "el presupuesto es de 10k"},
]

_TRANSCRIPT = (
    "[00:10 Yo] hola\n"
    "[01:05 Ellos] el presupuesto es de 10k"
)


@pytest.fixture
def batch_groq(monkeypatch):
    monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "groq")
    monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.setattr(insights, "is_available", lambda task="live": True)


def _fake_chat_capture(monkeypatch, minutes_dict):
    """Monkeypatchea insights._chat para devolver minutes_dict y capturar los
    mensajes enviados (para inspeccionar el system prompt)."""
    captured = {}

    def _fake_chat(messages, **kwargs):
        captured["messages"] = messages
        return json.dumps(minutes_dict, ensure_ascii=False)

    monkeypatch.setattr(insights, "_chat", _fake_chat)
    return captured


def _base_minutes(**extra):
    base = {
        "resumen": "ok",
        "decisiones": [],
        "temas": [],
        "pendientes": [],
        "propuestas": [],
        "citas": [],
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# TEMPLATES bien formadas
# ---------------------------------------------------------------------------

class TestTemplatesShape:
    def test_exactly_four_templates(self):
        assert set(templates.TEMPLATES.keys()) == {"general", "ventas", "one_on_one", "clase"}

    def test_each_template_has_complete_fields(self):
        for name, tpl in templates.TEMPLATES.items():
            assert isinstance(tpl.get("label"), str) and tpl["label"], name
            assert "acta_extra" in tpl and isinstance(tpl["acta_extra"], str), name
            chips = tpl.get("chips")
            assert isinstance(chips, list) and len(chips) == 3, name
            assert all(isinstance(c, str) and c for c in chips), name
            assert "live_extra" in tpl and isinstance(tpl["live_extra"], str), name

    def test_prompts_are_compact(self):
        # <600 tokens es aproximadamente <2400 chars (heurística ~4 chars/token).
        for name, tpl in templates.TEMPLATES.items():
            assert len(tpl["acta_extra"]) < 2400, f"{name}: acta_extra demasiado largo"
            assert len(tpl["live_extra"]) < 2400, f"{name}: live_extra demasiado largo"

    def test_general_is_behaviorally_neutral(self):
        general = templates.TEMPLATES["general"]
        assert general["acta_extra"] == ""
        assert general["live_extra"] == ""
        assert general["chips"] == [
            "¿Puntos clave hasta ahora?",
            "¿Qué me falta preguntar?",
            "Pendientes y responsables",
        ]

    def test_is_valid(self):
        assert templates.is_valid("ventas")
        assert not templates.is_valid("marketing")
        assert not templates.is_valid("")

    def test_get_falls_back_to_general(self):
        assert templates.get("no-existe") == templates.get("general")

    def test_chips_map_has_all_four(self):
        cm = templates.chips_map()
        assert set(cm.keys()) == {"general", "ventas", "one_on_one", "clase"}
        assert all(len(v) == 3 for v in cm.values())


# ---------------------------------------------------------------------------
# MeetingSession.set_template / start() / status()
# ---------------------------------------------------------------------------

class TestMeetingSessionTemplate:
    def test_default_template_is_general(self):
        m = MeetingSession()
        assert m.get_template() == "general"

    def test_set_template_valid_applies(self):
        m = MeetingSession()
        assert m.set_template("ventas") is True
        assert m.get_template() == "ventas"

    def test_set_template_invalid_rejected_keeps_previous(self):
        m = MeetingSession()
        m.set_template("clase")
        assert m.set_template("no-existe") is False
        assert m.get_template() == "clase"

    def test_status_exposes_template_and_label(self):
        m = MeetingSession()
        m.set_template("one_on_one")
        s = m.status()
        assert s["template"] == "one_on_one"
        assert s["template_label"] == templates.TEMPLATES["one_on_one"]["label"]

    def test_start_does_not_reset_template(self, monkeypatch):
        """CORRECCIÓN DE DEBATE: la plantilla persiste ENTRE reuniones del proceso;
        start() no debe resetearla a 'general'."""
        m = MeetingSession()
        m.set_template("ventas")

        # Evitar tocar hardware real: forzar fallo de mic (aborta start() temprano,
        # pero el bloque bajo lock que resetea el estado ya corrió).
        class _FailingMic:
            def start(self, cb):
                raise RuntimeError("sin micrófono en test")

        monkeypatch.setattr("core.meeting.MicSource", _FailingMic)
        res = m.start()
        assert res["ok"] is False
        # La plantilla debe seguir siendo la elegida antes del intento de start().
        assert m.get_template() == "ventas"


# ---------------------------------------------------------------------------
# generate_minutes con template
# ---------------------------------------------------------------------------

class TestGenerateMinutesTemplate:
    def test_no_template_system_prompt_unchanged(self, batch_groq, monkeypatch):
        captured = _fake_chat_capture(monkeypatch, _base_minutes())
        insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        system_msg = captured["messages"][0]
        assert system_msg["role"] == "system"
        assert system_msg["content"] == insights._MINUTES_SYSTEM

    def test_general_template_system_prompt_unchanged(self, batch_groq, monkeypatch):
        captured = _fake_chat_capture(monkeypatch, _base_minutes())
        insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS, template="general")
        assert captured["messages"][0]["content"] == insights._MINUTES_SYSTEM

    def test_ventas_template_appends_bant_block_to_system(self, batch_groq, monkeypatch):
        captured = _fake_chat_capture(monkeypatch, _base_minutes())
        insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS, template="ventas")
        system_msg = captured["messages"][0]["content"]
        assert system_msg.startswith(insights._MINUTES_SYSTEM)
        assert "bant" in system_msg.lower()
        assert "BUDGET" in system_msg.upper()  # menciona los 4 campos BANT

    def test_one_on_one_template_appends_extra_no_bant(self, batch_groq, monkeypatch):
        captured = _fake_chat_capture(monkeypatch, _base_minutes())
        insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS, template="one_on_one")
        system_msg = captured["messages"][0]["content"]
        assert "1:1" in system_msg or "acuerdos personales".upper() in system_msg.upper()
        assert '"bant"' not in system_msg

    def test_bant_survives_post_processing(self, batch_groq, monkeypatch):
        _fake_chat_capture(monkeypatch, _base_minutes(
            bant={"budget": "10k", "authority": "", "need": "automatizar reportes", "timeline": ""}
        ))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS, template="ventas")
        assert "bant" in result
        assert result["bant"]["budget"] == "10k"
        assert result["bant"]["need"] == "automatizar reportes"
        # Campos vacíos no sobreviven (fail-safe: no rellenar con "" sin evidencia)
        assert "authority" not in result["bant"]
        assert "timeline" not in result["bant"]

    def test_bant_omitted_entirely_when_no_evidence(self, batch_groq, monkeypatch):
        _fake_chat_capture(monkeypatch, _base_minutes(
            bant={"budget": "", "authority": "", "need": "", "timeline": ""}
        ))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS, template="ventas")
        assert "bant" not in result

    def test_bant_absent_when_llm_omits_key(self, batch_groq, monkeypatch):
        _fake_chat_capture(monkeypatch, _base_minutes())  # sin clave "bant"
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS, template="ventas")
        assert "bant" not in result

    def test_invalid_template_name_behaves_like_no_template(self, batch_groq, monkeypatch):
        captured = _fake_chat_capture(monkeypatch, _base_minutes())
        insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS, template="no-existe")
        # get() cae a "general" (acta_extra=""), así que el system queda igual.
        assert captured["messages"][0]["content"] == insights._MINUTES_SYSTEM


# ---------------------------------------------------------------------------
# Round-trip de la columna `template` en la DB
# ---------------------------------------------------------------------------

class TestDatabaseTemplateColumn:
    def test_meeting_insert_and_get_roundtrip_template(self, tmp_path, monkeypatch):
        from db.database import TranscriptionDB

        db_path = str(tmp_path / "test_templates.db")
        db = TranscriptionDB(db_path=db_path)
        meeting_id = db.meeting_insert(
            title="Reunión de prueba",
            transcript="[00:10 Yo] hola",
            segments_json=json.dumps(_SEGMENTS, ensure_ascii=False),
            duration_seconds=90.0,
            started_at="2026-07-03 10:00:00",
            template="ventas",
        )
        row = db.meeting_get(meeting_id)
        assert row["template"] == "ventas"

    def test_meeting_insert_without_template_is_null(self, tmp_path):
        from db.database import TranscriptionDB

        db_path = str(tmp_path / "test_templates2.db")
        db = TranscriptionDB(db_path=db_path)
        meeting_id = db.meeting_insert(
            title="Reunión sin plantilla",
            transcript="[00:10 Yo] hola",
            segments_json="[]",
            duration_seconds=10.0,
        )
        row = db.meeting_get(meeting_id)
        assert row["template"] is None
