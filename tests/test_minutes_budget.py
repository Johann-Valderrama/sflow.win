"""Tests para la unidad 4.0 'Presupuesto de contexto para el acta' (Ola 4).

Cubre el helper compartido ``insights.budget_chars`` y su aplicación en
``generate_minutes``/``generate_chapters``:
  - Transcript corto (cabe en el presupuesto) pasa intacto, sin aviso de truncado.
  - Transcript enorme contra un presupuesto chico (backend "endpoint", 18KB) se
    trunca por el PRINCIPIO (se conserva el final, contiguo), el prompt final
    cabe en el presupuesto, y se antepone el aviso con los minutos correctos.
  - Backend "claude-cli" (40KB): un transcript de 30KB pasa intacto.
  - ``assistant._budget_chars`` sigue delegando en el mismo helper (incluido
    en test_assistant_live.py; aquí solo se verifica la delegación puntual).

No llama a ningún LLM real: ``insights._chat`` se monkeypatchea y captura el
prompt de usuario que habría recibido.
"""

import pytest

from core import insights


def _make_transcript_lines(n: int, words_per_line: int = 20) -> str:
    """Genera un transcript sintético con marcadores [mm:ss] crecientes."""
    lines = []
    for i in range(n):
        secs = i * 10
        mm, ss = divmod(secs, 60)
        speaker = "Yo" if i % 2 == 0 else "Ellos"
        text = f"intervención número {i} " + ("bla " * words_per_line)
        lines.append(f"[{mm:02d}:{ss:02d} {speaker}] {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# budget_chars — resuelve por backend, igual que el _budget_chars original
# ---------------------------------------------------------------------------

class TestBudgetChars:
    def test_endpoint_backend_18k(self, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "endpoint")
        monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
        assert insights.budget_chars(task="batch") == 18000

    def test_claude_cli_backend_40k(self, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "claude-cli")
        monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
        assert insights.budget_chars(task="batch") == 40000

    def test_groq_backend_80k(self, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "groq")
        monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
        assert insights.budget_chars(task="batch") == 80000

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "endpoint")
        monkeypatch.setenv("ASSISTANT_CONTEXT_BUDGET_CHARS", "1234")
        assert insights.budget_chars(task="batch") == 1234


# ---------------------------------------------------------------------------
# generate_minutes — truncado por el principio + aviso
# ---------------------------------------------------------------------------

class TestGenerateMinutesBudget:
    def _fake_available(self, monkeypatch, backend: str):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", backend)
        monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
        monkeypatch.setattr(insights, "is_available", lambda task="live": True)

    def test_short_transcript_passes_intact_no_notice(self, monkeypatch):
        self._fake_available(monkeypatch, "groq")  # 80KB de presupuesto
        transcript = _make_transcript_lines(20)
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["user"] = messages[1]["content"]
            return '{"resumen": "ok", "decisiones": [], "temas": [], ' \
                   '"pendientes": [], "propuestas": [], "citas": []}'

        monkeypatch.setattr(insights, "_chat", _fake_chat)
        result = insights.generate_minutes(transcript)
        assert result["resumen"] == "ok"
        assert "[transcript truncado" not in captured["user"]
        assert "intervención número 0 " in captured["user"]
        assert "intervención número 19 " in captured["user"]

    def test_huge_transcript_endpoint_backend_truncates_with_notice(self, monkeypatch):
        self._fake_available(monkeypatch, "endpoint")  # 18KB de presupuesto
        # ~100KB de transcript sintético (500 líneas largas)
        transcript = _make_transcript_lines(500, words_per_line=60)
        assert len(transcript) > 100_000
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["user"] = messages[1]["content"]
            return '{"resumen": "ok", "decisiones": [], "temas": [], ' \
                   '"pendientes": [], "propuestas": [], "citas": []}'

        monkeypatch.setattr(insights, "_chat", _fake_chat)
        result = insights.generate_minutes(transcript)
        assert result["resumen"] == "ok"

        user_prompt = captured["user"]
        # El prompt final cabe en el presupuesto (18000, con margen).
        assert len(user_prompt) <= 18000
        # Se conserva el FINAL del transcript...
        assert "intervención número 499 " in user_prompt
        # ...y el aviso está presente.
        assert "[transcript truncado por límite del modelo: faltan los primeros" in user_prompt
        # ...con los primeros índices recortados.
        assert "intervención número 0 " not in user_prompt

        # El aviso indica X minutos: calculado del primer [mm:ss] que sobrevive.
        import re
        notice_match = re.search(r"faltan los primeros (\d+) minutos", user_prompt)
        assert notice_match is not None
        x_minutes = int(notice_match.group(1))

        first_ts_match = re.search(r"\[(\d{2}):(\d{2})", user_prompt[len("TRANSCRIPCIÓN:\n"):])
        assert first_ts_match is not None
        expected_minutes = int(first_ts_match.group(1))
        assert x_minutes == expected_minutes

    def test_claude_cli_backend_30kb_passes_intact(self, monkeypatch):
        self._fake_available(monkeypatch, "claude-cli")  # 40KB de presupuesto
        # Construir un transcript de ~30KB
        transcript = _make_transcript_lines(150, words_per_line=35)
        assert 25_000 < len(transcript) < 35_000
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["user"] = messages[1]["content"]
            return '{"resumen": "ok", "decisiones": [], "temas": [], ' \
                   '"pendientes": [], "propuestas": [], "citas": []}'

        monkeypatch.setattr(insights, "_chat", _fake_chat)
        result = insights.generate_minutes(transcript)
        assert result["resumen"] == "ok"
        assert "[transcript truncado" not in captured["user"]
        assert "intervención número 0 " in captured["user"]
        assert "intervención número 149 " in captured["user"]

    def test_allowance_non_positive_returns_empty_not_full_transcript(self):
        """F13: cuando los bloques fijos ya agotan el presupuesto seguro
        (allowance <= 0), la función debe descartar el transcript por completo
        ("") en vez de devolverlo COMPLETO (bug original: justo el caso que más
        necesita truncar terminaba desbordando el contexto sin recortar nada)."""
        transcript = "[00:00 Yo] " + ("bla " * 500)  # ~2500 chars, no vacío
        # other_len >= safe_budget (budget*0.9): allowance queda <= 0.
        result = insights._truncate_transcript_to_budget(
            transcript, other_len=100_000, budget=18000,
        )
        assert result == ""
        assert result != transcript

    def test_allowance_exactly_zero_returns_empty(self):
        budget = 1000
        safe_budget = int(budget * 0.9)  # 900
        result = insights._truncate_transcript_to_budget(
            "algo de transcript", other_len=safe_budget, budget=budget,
        )
        assert result == ""

    def test_generate_minutes_survives_allowance_non_positive(self, monkeypatch):
        """El flujo completo de generate_minutes no revienta cuando el presupuesto
        queda agotado por bloques fijos: el transcript llega vacío al prompt, pero
        la llamada se completa y el acta se genera igual."""
        self._fake_available(monkeypatch, "endpoint")  # 18KB
        # Un bloque "extra" (highlights) enorme agota el presupuesto seguro por sí
        # solo, forzando allowance <= 0 dentro de generate_minutes.
        transcript = _make_transcript_lines(50)
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["user"] = messages[1]["content"]
            return '{"resumen": "ok", "decisiones": [], "temas": [], ' \
                   '"pendientes": [], "propuestas": [], "citas": []}'

        monkeypatch.setattr(insights, "_chat", _fake_chat)
        huge_highlights = [{"time": f"{i:02d}:00"} for i in range(20000)]
        result = insights.generate_minutes(transcript, highlights=huge_highlights)
        assert result["resumen"] == "ok"
        # El transcript quedó vacío (descartado), pero el prompt no truena y el
        # bloque de highlights (parte fija) sigue presente completo.
        assert "MOMENTOS DESTACADOS" in captured["user"]
        assert "intervención número 0 " not in captured["user"]

    def test_insights_and_highlights_never_truncated(self, monkeypatch):
        """Los bloques insights/highlights son pequeños y no deben truncarse aunque
        el transcript sí lo sea."""
        self._fake_available(monkeypatch, "endpoint")
        transcript = _make_transcript_lines(500)
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["user"] = messages[1]["content"]
            return '{"resumen": "ok", "decisiones": [], "temas": [], ' \
                   '"pendientes": [], "propuestas": [], "citas": []}'

        monkeypatch.setattr(insights, "_chat", _fake_chat)
        demo_insights = {"temas": ["Presupuesto Q3"], "pendientes": [], "propuestas": [], "citas": []}
        demo_highlights = [{"t": 5.0, "time": "00:05"}]
        insights.generate_minutes(transcript, insights=demo_insights, highlights=demo_highlights)
        assert "Presupuesto Q3" in captured["user"]
        assert "00:05" in captured["user"]
        assert "ANÁLISIS EN VIVO DETECTADO" in captured["user"]
        assert "MOMENTOS DESTACADOS" in captured["user"]


# ---------------------------------------------------------------------------
# generate_chapters — mismo helper, mismo recorte
# ---------------------------------------------------------------------------

class TestGenerateChaptersBudget:
    def test_huge_transcript_endpoint_backend_truncates(self, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "endpoint")
        monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
        monkeypatch.setattr(insights, "is_available", lambda task="live": True)
        transcript = _make_transcript_lines(500)
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["user"] = messages[1]["content"]
            return '{"capitulos": []}'

        monkeypatch.setattr(insights, "_chat", _fake_chat)
        insights.generate_chapters(transcript)
        assert len(captured["user"]) <= 18000
        assert "intervención número 499 " in captured["user"]
        assert "intervención número 0 " not in captured["user"]

    def test_short_transcript_passes_intact(self, monkeypatch):
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "groq")
        monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
        monkeypatch.setattr(insights, "is_available", lambda task="live": True)
        transcript = _make_transcript_lines(10)
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["user"] = messages[1]["content"]
            return '{"capitulos": []}'

        monkeypatch.setattr(insights, "_chat", _fake_chat)
        insights.generate_chapters(transcript)
        assert "intervención número 0 " in captured["user"]


# ---------------------------------------------------------------------------
# assistant._budget_chars sigue delegando en insights.budget_chars
# ---------------------------------------------------------------------------

class TestAssistantDelegation:
    def test_assistant_budget_chars_delegates(self, monkeypatch):
        from core import assistant
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "endpoint")
        monkeypatch.delenv("ASSISTANT_CONTEXT_BUDGET_CHARS", raising=False)
        assert assistant._budget_chars() == 18000
        monkeypatch.setenv("INSIGHTS_BACKEND_BATCH", "claude-cli")
        assert assistant._budget_chars() == 40000
