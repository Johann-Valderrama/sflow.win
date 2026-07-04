"""Tests para la unidad 4.2 'Lupa de trazabilidad del acta' (Ola 4).

Cubre, SIN llamar a ningún LLM real (``insights._chat`` monkeypatcheado):
  - Decisiones devueltas como strings planos por el LLM se normalizan a
    {"texto": str} (formato viejo, sin "t").
  - Decisiones con "time" válido reciben "t" snapeado al segmento más cercano
    (caso exacto y caso desviado unos segundos).
  - "time" no parseable (o ausente) => sin "t".
  - Sin "segments" pasados a generate_minutes => nunca hay "t" aunque el LLM
    devuelva "time".
  - Pendientes conservan sus campos existentes (texto/responsable/fecha/hora)
    y ganan "t" cuando aplica.
  - Render tolera string|dict en los 4 consumidores (meeting_export,
    assistant._format_acta) sin romper con actas viejas.
"""

import json

import pytest

from core import insights


_SEGMENTS = [
    {"t": 10.0, "time": "00:10", "speaker": "Yo", "text": "hola"},
    {"t": 65.0, "time": "01:05", "speaker": "Ellos", "text": "el servidor está caído"},
    {"t": 130.0, "time": "02:10", "speaker": "Yo", "text": "cuánto cuesta, decidimos usar Groq"},
]

_TRANSCRIPT = (
    "[00:10 Yo] hola\n"
    "[01:05 Ellos] el servidor está caído\n"
    "[02:10 Yo] cuánto cuesta, decidimos usar Groq"
)


@pytest.fixture
def batch_groq(monkeypatch):
    monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "groq")
    monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.setattr(insights, "is_available", lambda task="live": True)


def _fake_chat_with(monkeypatch, minutes_dict):
    def _fake_chat(messages, **kwargs):
        return json.dumps(minutes_dict, ensure_ascii=False)
    monkeypatch.setattr(insights, "_chat", _fake_chat)


def _base_minutes(decisiones=None, pendientes=None):
    return {
        "resumen": "ok",
        "decisiones": decisiones if decisiones is not None else [],
        "temas": [],
        "pendientes": pendientes if pendientes is not None else [],
        "propuestas": [],
        "citas": [],
    }


class TestDecisionesNormalizacion:
    def test_string_decisions_normalized_to_dict_without_t(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(decisiones=["Se decidió usar Groq"]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        assert result["decisiones"] == [{"texto": "Se decidió usar Groq"}]

    def test_exact_time_snaps_to_matching_segment(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(
            decisiones=[{"texto": "Se decidió usar Groq", "time": "02:10"}]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        dec = result["decisiones"][0]
        assert dec["texto"] == "Se decidió usar Groq"
        assert dec["t"] == 130.0

    def test_slightly_off_time_snaps_to_nearest_segment(self, batch_groq, monkeypatch):
        # 02:13 (133s) no existe como segmento real; el más cercano es 02:10 (130s).
        _fake_chat_with(monkeypatch, _base_minutes(
            decisiones=[{"texto": "Se decidió usar Groq", "time": "02:13"}]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        assert result["decisiones"][0]["t"] == 130.0

    def test_unparseable_time_yields_no_t(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(
            decisiones=[{"texto": "Se decidió usar Groq", "time": "no-time"}]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        dec = result["decisiones"][0]
        assert dec == {"texto": "Se decidió usar Groq"}
        assert "t" not in dec

    def test_null_time_yields_no_t(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(
            decisiones=[{"texto": "Se decidió usar Groq", "time": None}]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        assert "t" not in result["decisiones"][0]

    def test_no_segments_passed_never_yields_t(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(
            decisiones=[{"texto": "Se decidió usar Groq", "time": "02:10"}]))
        result = insights.generate_minutes(_TRANSCRIPT)  # sin segments=
        dec = result["decisiones"][0]
        assert dec == {"texto": "Se decidió usar Groq"}
        assert "t" not in dec

    def test_empty_texto_dropped(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(
            decisiones=[{"texto": "  ", "time": "02:10"}, {"texto": "válida"}]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        assert result["decisiones"] == [{"texto": "válida"}]


class TestPendientesTrazabilidad:
    def test_pendiente_preserves_existing_fields_and_gains_t(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(pendientes=[
            {"texto": "Enviar propuesta", "responsable": "Juan", "fecha": "2026-07-10",
             "hora": "15:00", "time": "01:05"},
        ]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        pend = result["pendientes"][0]
        assert pend["texto"] == "Enviar propuesta"
        assert pend["responsable"] == "Juan"
        assert pend["fecha"] == "2026-07-10"
        assert pend["hora"] == "15:00"
        assert pend["t"] == 65.0

    def test_pendiente_without_time_has_no_t(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(pendientes=[
            {"texto": "Enviar propuesta", "responsable": "Juan"},
        ]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        pend = result["pendientes"][0]
        assert "t" not in pend
        assert pend["texto"] == "Enviar propuesta"

    def test_pendiente_string_tolerated(self, batch_groq, monkeypatch):
        _fake_chat_with(monkeypatch, _base_minutes(pendientes=["pendiente viejo en string"]))
        result = insights.generate_minutes(_TRANSCRIPT, segments=_SEGMENTS)
        assert result["pendientes"] == ["pendiente viejo en string"]


class TestRenderTolerance:
    def test_meeting_export_tolerates_string_decisions(self):
        from core.meeting_export import meeting_markdown
        minutes = {"resumen": "r", "decisiones": ["decisión vieja en string"]}
        meeting = {"id": 1, "started_at": "2026-07-01 10:00:00",
                   "minutes_json": json.dumps(minutes, ensure_ascii=False)}
        md = meeting_markdown(meeting)
        assert "- decisión vieja en string" in md

    def test_meeting_export_renders_dict_decision_with_time_suffix(self):
        from core.meeting_export import meeting_markdown
        minutes = {"resumen": "r", "decisiones": [{"texto": "Usar Groq", "t": 130.0}]}
        meeting = {"id": 1, "started_at": "2026-07-01 10:00:00",
                   "minutes_json": json.dumps(minutes, ensure_ascii=False)}
        md = meeting_markdown(meeting)
        assert "- Usar Groq (2:10)" in md

    def test_meeting_export_pendiente_with_time_suffix(self):
        from core.meeting_export import meeting_markdown
        minutes = {"resumen": "r", "pendientes": [
            {"texto": "Enviar propuesta", "t": 65.0}]}
        meeting = {"id": 1, "started_at": "2026-07-01 10:00:00",
                   "minutes_json": json.dumps(minutes, ensure_ascii=False)}
        md = meeting_markdown(meeting)
        assert "- [ ] Enviar propuesta (1:05)" in md

    def test_format_acta_tolerates_string_and_dict_decisions(self):
        from core.assistant import _format_acta
        txt = _format_acta({"resumen": "r", "decisiones": [
            "decisión vieja", {"texto": "Usar Groq", "t": 130.0}]})
        assert "- decisión vieja" in txt
        assert "- Usar Groq (2:10)" in txt

    def test_format_acta_pendiente_with_time_suffix(self):
        from core.assistant import _format_acta
        txt = _format_acta({"resumen": "r", "pendientes": [
            {"texto": "Enviar propuesta", "t": 65.0}]})
        assert "- Enviar propuesta (1:05)" in txt
