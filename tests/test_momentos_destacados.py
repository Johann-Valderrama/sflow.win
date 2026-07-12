"""Tests para el bug F12 (auditoría 2026-07-06): gateo y normalización de
'momentos_destacados' en el acta (``core.insights.generate_minutes``).

Cubre, SIN llamar a ningún LLM real (``insights._chat`` monkeypatcheado):
  - Sin highlights REALES: la clave nunca entra al acta, aunque el LLM la
    devuelva (alucinada) con contenido válido.
  - Con highlights reales y el LLM devuelve una lista válida de dicts
    {"time", "texto"}: passthrough normalizado (comportamiento de hoy).
  - El LLM devuelve un string plano en vez de la lista esperada: se descarta
    (nunca se itera por caracteres en los renders).
  - La lista trae elementos de tipos mezclados (dict válido, string, int,
    None, dict vacío): se filtra/normaliza tolerante, sin reventar.
  - Renders (``_format_acta``, ``meeting_export.meeting_markdown``) siguen
    tolerando el resultado normalizado.
"""

import json

import pytest

from core import insights


_MINUTES_BASE = {
    "resumen": "ok", "decisiones": [], "temas": [],
    "pendientes": [], "propuestas": [], "citas": [],
}

_TRANSCRIPT = "[00:10 Yo] hola\n[01:05 Ellos] el servidor está caído"

_HIGHLIGHTS = [{"t": 65.0, "time": "01:05"}]


@pytest.fixture
def batch_groq(monkeypatch):
    monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "groq")
    monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.setattr(insights, "is_available", lambda task="live": True)


def _run(monkeypatch, *, momentos_from_llm, highlights):
    data = dict(_MINUTES_BASE)
    if momentos_from_llm is not None:
        data["momentos_destacados"] = momentos_from_llm

    def _fake_chat(messages, **kwargs):
        return json.dumps(data, ensure_ascii=False)

    monkeypatch.setattr(insights, "_chat", _fake_chat)
    return insights.generate_minutes(_TRANSCRIPT, highlights=highlights)


class TestMomentosGateoPorHighlightsReales:
    def test_sin_highlights_reales_clave_ausente_aunque_llm_alucine(self, batch_groq, monkeypatch):
        result = _run(
            monkeypatch,
            momentos_from_llm=[{"time": "01:05", "texto": "el LLM inventó esto"}],
            highlights=None,
        )
        assert "momentos_destacados" not in result

    def test_highlights_vacios_clave_ausente(self, batch_groq, monkeypatch):
        result = _run(
            monkeypatch,
            momentos_from_llm=[{"time": "01:05", "texto": "inventado"}],
            highlights=[],
        )
        assert "momentos_destacados" not in result

    def test_con_highlights_reales_y_lista_valida_passthrough_normalizado(self, batch_groq, monkeypatch):
        result = _run(
            monkeypatch,
            momentos_from_llm=[{"time": "01:05", "texto": "servidor caído"}],
            highlights=_HIGHLIGHTS,
        )
        assert result["momentos_destacados"] == [{"time": "01:05", "texto": "servidor caído"}]

    def test_con_highlights_reales_pero_llm_omite_la_clave_no_hay_clave(self, batch_groq, monkeypatch):
        result = _run(monkeypatch, momentos_from_llm=None, highlights=_HIGHLIGHTS)
        assert "momentos_destacados" not in result


class TestMomentosNormalizaTipos:
    def test_string_plano_se_descarta_sin_iterar_caracteres(self, batch_groq, monkeypatch):
        result = _run(monkeypatch, momentos_from_llm="no soy una lista", highlights=_HIGHLIGHTS)
        assert "momentos_destacados" not in result

    def test_tipo_no_lista_no_string_se_descarta(self, batch_groq, monkeypatch):
        result = _run(monkeypatch, momentos_from_llm=42, highlights=_HIGHLIGHTS)
        assert "momentos_destacados" not in result

    def test_lista_con_tipos_mezclados_se_filtra_tolerante(self, batch_groq, monkeypatch):
        result = _run(
            monkeypatch,
            momentos_from_llm=[
                {"time": "01:05", "texto": "válido"},
                "momento como string suelto",
                42,
                None,
                {},  # dict sin time ni texto -> se descarta
                {"time": "", "texto": "  "},  # ambos vacíos -> se descarta
            ],
            highlights=_HIGHLIGHTS,
        )
        assert result["momentos_destacados"] == [
            {"time": "01:05", "texto": "válido"},
            {"time": "", "texto": "momento como string suelto"},
        ]

    def test_direct_unit_normalize_momentos(self):
        # Prueba directa del helper, sin pasar por generate_minutes/_chat.
        assert insights._normalize_momentos(None, None) == []
        assert insights._normalize_momentos([{"time": "1", "texto": "x"}], None) == []
        assert insights._normalize_momentos([{"time": "1", "texto": "x"}], []) == []
        assert insights._normalize_momentos("string suelto", _HIGHLIGHTS) == []
        assert insights._normalize_momentos(
            [{"time": "01:05", "texto": "x"}], _HIGHLIGHTS,
        ) == [{"time": "01:05", "texto": "x"}]


class TestMomentosRenderToleraNormalizado:
    def test_format_acta_renderiza_momentos_normalizados(self):
        from core.assistant import _format_acta
        txt = _format_acta({"resumen": "r", "momentos_destacados": [
            {"time": "01:05", "texto": "servidor caído"},
        ]})
        assert "Momentos destacados:" in txt
        assert "- 01:05 servidor caído" in txt

    def test_format_acta_sin_momentos_no_seccion(self):
        from core.assistant import _format_acta
        txt = _format_acta({"resumen": "r"})
        assert "Momentos destacados" not in txt

    def test_meeting_markdown_renderiza_momentos_normalizados(self):
        from core.meeting_export import meeting_markdown
        meeting = {
            "id": 1,
            "started_at": "2026-07-01 10:00:00",
            "duration_seconds": 600,
            "transcript": "[00:10 Yo] hola",
            "minutes_json": json.dumps({
                "resumen": "r",
                "momentos_destacados": [{"time": "01:05", "texto": "servidor caído"}],
            }, ensure_ascii=False),
        }
        md = meeting_markdown(meeting)
        assert "Momentos destacados" in md
