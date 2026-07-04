"""Tests para la unidad 4.1 'Fusión de notas del usuario en el acta' (Ola 4, patrón Granola).

Cubre, SIN llamar a ningún LLM real (``insights._chat`` monkeypatcheado):
  - Con notas: el prompt de usuario incluye el bloque "NOTAS DEL USUARIO" con las
    líneas "- [mm:ss] texto" y la instrucción condicional de prioridad.
  - Sin notas: el prompt es idéntico al actual — NO aparece ni el bloque ni la
    instrucción de prioridad (corrección O7: la prioridad solo viaja cuando hay notas).
  - El system prompt permanente NO contiene la instrucción de prioridad.
  - Post-proceso (_reconcile_user_notes vía generate_minutes): una nota reescrita por
    el LLM se restaura al texto literal original; una nota omitida se añade con
    contexto ""; ítems inventados por el LLM se descartan.
  - El bloque de notas cuenta en el presupuesto pero nunca se trunca.
  - Render markdown (meeting_export) tolera notas_usuario ausente y las renderiza
    con el formato "- **mm:ss** nota / - _IA: contexto_" cuando están.
  - _format_acta (assistant) incluye "Notas del usuario:" cuando hay notas.
"""

import json

import pytest

from core import insights


_MINUTES_JSON_OK = (
    '{"resumen": "ok", "decisiones": [], "temas": [], '
    '"pendientes": [], "propuestas": [], "citas": []}'
)


@pytest.fixture
def batch_groq(monkeypatch):
    monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "groq")
    monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
    monkeypatch.setattr(insights, "is_available", lambda task="live": True)


def _capture_chat(monkeypatch, response=_MINUTES_JSON_OK):
    captured = {}

    def _fake_chat(messages, **kwargs):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        return response

    monkeypatch.setattr(insights, "_chat", _fake_chat)
    return captured


_NOTES = [
    {"t": 65.0, "time": "01:05", "text": "ojo con esto"},
    {"t": 130.0, "time": "02:10", "text": "preguntar precio"},
]

_TRANSCRIPT = "[00:10 Yo] hola\n[01:05 Ellos] el servidor está caído\n[02:10 Yo] cuánto cuesta"


# ---------------------------------------------------------------------------
# Prompt: con notas / sin notas
# ---------------------------------------------------------------------------

class TestNotesPrompt:
    def test_notes_block_and_priority_instruction_in_user_prompt(self, batch_groq, monkeypatch):
        captured = _capture_chat(monkeypatch)
        insights.generate_minutes(_TRANSCRIPT, notes=_NOTES)
        user = captured["user"]
        assert "NOTAS DEL USUARIO (escritas en vivo durante la reunión):" in user
        assert "- [01:05] ojo con esto" in user
        assert "- [02:10] preguntar precio" in user
        assert "dales prioridad en el resumen y las decisiones" in user
        assert "NUNCA modifiques, resumas ni corrijas el texto literal de las notas" in user
        # La especificación de la clave vive en el system (condicional-suave).
        assert '"notas_usuario"' in captured["system"]

    def test_no_notes_prompt_identical_and_no_priority_instruction(self, batch_groq, monkeypatch):
        captured = _capture_chat(monkeypatch)
        insights.generate_minutes(_TRANSCRIPT)
        baseline_user = captured["user"]
        baseline_system = captured["system"]

        captured2 = _capture_chat(monkeypatch)
        insights.generate_minutes(_TRANSCRIPT, notes=None)
        assert captured2["user"] == baseline_user
        assert captured2["system"] == baseline_system

        captured3 = _capture_chat(monkeypatch)
        insights.generate_minutes(_TRANSCRIPT, notes=[])
        assert captured3["user"] == baseline_user

        # Sin notas: ni bloque ni instrucción de prioridad en el prompt de usuario.
        assert "NOTAS DEL USUARIO" not in baseline_user
        assert "dales prioridad" not in baseline_user

    def test_priority_instruction_not_in_permanent_system(self):
        # Corrección O7: la instrucción de prioridad NO vive en el system permanente.
        assert "dales prioridad" not in insights._MINUTES_SYSTEM
        assert "prioridad" not in insights._MINUTES_SYSTEM

    def test_notes_with_empty_text_are_skipped(self, batch_groq, monkeypatch):
        captured = _capture_chat(monkeypatch)
        insights.generate_minutes(_TRANSCRIPT, notes=[{"t": 1.0, "time": "00:01", "text": "  "}])
        assert "NOTAS DEL USUARIO" not in captured["user"]

    def test_notes_block_counts_in_budget_but_never_truncated(self, batch_groq, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "endpoint")  # 18KB
        lines = []
        for i in range(500):
            mm, ss = divmod(i * 10, 60)
            lines.append(f"[{mm:02d}:{ss:02d} Yo] intervención número {i} " + "bla " * 40)
        transcript = "\n".join(lines)
        captured = _capture_chat(monkeypatch)
        insights.generate_minutes(transcript, notes=_NOTES)
        user = captured["user"]
        assert len(user) <= 18000
        assert "[transcript truncado" in user
        # El bloque de notas sobrevive intacto aunque el transcript se trunque.
        assert "- [01:05] ojo con esto" in user
        assert "- [02:10] preguntar precio" in user


# ---------------------------------------------------------------------------
# Post-proceso: el humano manda
# ---------------------------------------------------------------------------

class TestNotesPostProcess:
    def _run(self, monkeypatch, llm_notas):
        data = json.loads(_MINUTES_JSON_OK)
        data["notas_usuario"] = llm_notas
        _capture_chat(monkeypatch, response=json.dumps(data, ensure_ascii=False))
        return insights.generate_minutes(_TRANSCRIPT, notes=_NOTES)

    def test_rewritten_note_restored_to_literal(self, batch_groq, monkeypatch):
        result = self._run(monkeypatch, [
            {"time": "01:05", "nota": "Atención con este punto",  # reescrita por el LLM
             "contexto": "Se hablaba del servidor caído."},
            {"time": "02:10", "nota": "preguntar precio", "contexto": "Se preguntó el costo."},
        ])
        nus = result["notas_usuario"]
        assert [n["nota"] for n in nus] == ["ojo con esto", "preguntar precio"]
        # El contexto de la nota reescrita se conserva (matcheada por timestamp).
        assert nus[0]["contexto"] == "Se hablaba del servidor caído."
        assert nus[1]["contexto"] == "Se preguntó el costo."

    def test_omitted_note_added_with_empty_context(self, batch_groq, monkeypatch):
        result = self._run(monkeypatch, [
            {"time": "01:05", "nota": "ojo con esto", "contexto": "Servidor caído."},
            # el LLM omitió "preguntar precio"
        ])
        nus = result["notas_usuario"]
        assert len(nus) == 2
        assert nus[1] == {"time": "02:10", "nota": "preguntar precio", "contexto": ""}

    def test_invented_items_are_dropped(self, batch_groq, monkeypatch):
        result = self._run(monkeypatch, [
            {"time": "01:05", "nota": "ojo con esto", "contexto": "Servidor caído."},
            {"time": "02:10", "nota": "preguntar precio", "contexto": ""},
            {"time": "05:00", "nota": "nota inventada por el LLM", "contexto": "bla"},
        ])
        nus = result["notas_usuario"]
        assert len(nus) == 2
        assert all(n["nota"] in ("ojo con esto", "preguntar precio") for n in nus)

    def test_llm_omits_key_entirely_all_notes_present(self, batch_groq, monkeypatch):
        _capture_chat(monkeypatch)  # respuesta sin notas_usuario
        result = insights.generate_minutes(_TRANSCRIPT, notes=_NOTES)
        assert result["notas_usuario"] == [
            {"time": "01:05", "nota": "ojo con esto", "contexto": ""},
            {"time": "02:10", "nota": "preguntar precio", "contexto": ""},
        ]

    def test_garbage_llm_value_tolerated(self, batch_groq, monkeypatch):
        result = self._run(monkeypatch, "no soy una lista")
        assert len(result["notas_usuario"]) == 2
        assert all(n["contexto"] == "" for n in result["notas_usuario"])

    def test_match_by_normalized_text_case_and_spaces(self, batch_groq, monkeypatch):
        result = self._run(monkeypatch, [
            {"time": "01:05", "nota": "  Ojo   con esto ", "contexto": "ctx"},
        ])
        assert result["notas_usuario"][0] == {
            "time": "01:05", "nota": "ojo con esto", "contexto": "ctx"}

    def test_no_notes_no_key(self, batch_groq, monkeypatch):
        _capture_chat(monkeypatch)
        result = insights.generate_minutes(_TRANSCRIPT)
        assert "notas_usuario" not in result


# ---------------------------------------------------------------------------
# Render: markdown (meeting_export) y _format_acta (assistant)
# ---------------------------------------------------------------------------

class TestNotesRender:
    def _meeting(self, minutes):
        return {
            "id": 1,
            "started_at": "2026-07-01 10:00:00",
            "duration_seconds": 600,
            "transcript": "[00:10 Yo] hola",
            "minutes_json": json.dumps(minutes, ensure_ascii=False),
        }

    def test_markdown_tolerates_absent_notas_usuario(self):
        from core.meeting_export import meeting_markdown
        md = meeting_markdown(self._meeting({"resumen": "r", "decisiones": ["d"]}))
        assert "Notas del usuario" not in md
        assert "## Decisiones" in md

    def test_markdown_renders_notes_with_context(self):
        from core.meeting_export import meeting_markdown
        minutes = {"resumen": "r", "notas_usuario": [
            {"time": "01:05", "nota": "ojo con esto", "contexto": "Servidor caído."},
            {"time": "02:10", "nota": "preguntar precio", "contexto": ""},
        ]}
        md = meeting_markdown(self._meeting(minutes))
        assert "## 📝 Notas del usuario" in md
        assert "- **01:05** ojo con esto\n  - _IA: Servidor caído._" in md
        # Sin contexto: nota sola, sin línea "IA:".
        assert "- **02:10** preguntar precio" in md
        assert "_IA: _" not in md

    def test_format_acta_includes_notes(self):
        from core.assistant import _format_acta
        txt = _format_acta({"resumen": "r", "notas_usuario": [
            {"time": "01:05", "nota": "ojo con esto", "contexto": "Servidor caído."},
        ]})
        assert "Notas del usuario:" in txt
        assert "- 01:05 ojo con esto (IA: Servidor caído.)" in txt

    def test_format_acta_tolerates_absent_notes(self):
        from core.assistant import _format_acta
        txt = _format_acta({"resumen": "r"})
        assert "Notas del usuario" not in txt
