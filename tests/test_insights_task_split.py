"""Tests sin red para la selección de backend por tarea (live vs batch).

Reescrito desde el script huérfano test_insights_task_split.py (raíz) para la
unidad 2.2 del PLAN-MEJORAS. Protege el ruteo backend/modelo live vs batch de
core/insights.py, superficie sin cobertura equivalente en tests/:
  1. Precedencia de _resolve_backend (global vs per-task).
  2. Routing por task en _chat (dispatch a la rama correcta: openrouter/groq).
  3. Funciones públicas (update_state, consolidate, generate_minutes,
     chat_memory) pasan el task correcto a _chat.
  4. _model(task) devuelve el modelo del backend resuelto para esa tarea.
"""
import json
import os
from unittest.mock import MagicMock

import pytest

import core.insights as insights

_INSIGHT_KEYS = [
    "INSIGHTS_BACKEND", "INSIGHTS_BACKEND_LIVE", "INSIGHTS_BACKEND_BATCH",
    "INSIGHTS_ENABLED", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
    "INSIGHTS_MODEL", "INSIGHTS_ENDPOINT_MODEL", "INSIGHTS_ENDPOINT_URL",
]


@pytest.fixture(autouse=True)
def _clean_insight_env(monkeypatch):
    """Limpia todas las env vars relevantes de insights antes de cada test."""
    for k in _INSIGHT_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield


def _env(monkeypatch, **kw):
    for k, v in kw.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Caso 1 — Precedencia de _resolve_backend
# ---------------------------------------------------------------------------

class TestResolveBackendPrecedence:
    def test_no_env_defaults_both_to_groq(self, monkeypatch):
        assert insights._resolve_backend("live") == "groq"
        assert insights._resolve_backend("batch") == "groq"

    def test_global_backend_inherited_by_both(self, monkeypatch):
        _env(monkeypatch, INSIGHTS_BACKEND="endpoint")
        assert insights._resolve_backend("live") == "endpoint"
        assert insights._resolve_backend("batch") == "endpoint"

    def test_per_task_configured_directly(self, monkeypatch):
        _env(monkeypatch, INSIGHTS_BACKEND_LIVE="groq", INSIGHTS_BACKEND_BATCH="openrouter")
        assert insights._resolve_backend("live") == "groq"
        assert insights._resolve_backend("batch") == "openrouter"

    def test_per_task_wins_over_global(self, monkeypatch):
        _env(monkeypatch, INSIGHTS_BACKEND="groq", INSIGHTS_BACKEND_BATCH="openrouter")
        assert insights._resolve_backend("batch") == "openrouter"
        assert insights._resolve_backend("live") == "groq"


# ---------------------------------------------------------------------------
# Caso 2 — Routing por task en _chat
# ---------------------------------------------------------------------------

class TestChatRoutingByTask:
    def test_live_routes_to_openrouter_batch_routes_to_groq(self, monkeypatch):
        _env(monkeypatch, INSIGHTS_BACKEND_LIVE="openrouter", INSIGHTS_BACKEND_BATCH="groq",
             INSIGHTS_BACKEND=None, GROQ_API_KEY="gsk-fake", OPENROUTER_API_KEY="sk-test")

        openrouter_called = {"count": 0}

        def stub_openrouter(messages, *, task="live", json_mode=False, temperature=0.2,
                             max_tokens=1024, reasoning=False):
            openrouter_called["count"] += 1
            return "[OR]"

        groq_called = {"count": 0}

        def stub_groq_client():
            fake_msg = MagicMock()
            fake_msg.content = "[GROQ]"
            fake_choice = MagicMock()
            fake_choice.message = fake_msg
            fake_completion = MagicMock()
            fake_completion.choices = [fake_choice]
            client = MagicMock()
            client.chat.completions.create.return_value = fake_completion
            groq_called["count"] += 1
            return client

        monkeypatch.setattr(insights, "_chat_openrouter", stub_openrouter)
        monkeypatch.setattr(insights, "_get_groq_client", stub_groq_client)

        result_live = insights._chat([{"role": "user", "content": "x"}], task="live")
        assert result_live == "[OR]"
        assert openrouter_called["count"] == 1
        assert groq_called["count"] == 0

        result_batch = insights._chat([{"role": "user", "content": "x"}], task="batch")
        assert result_batch == "[GROQ]"
        assert openrouter_called["count"] == 1  # no incrementó
        assert groq_called["count"] == 1


# ---------------------------------------------------------------------------
# Caso 3 — Funciones públicas pasan el task correcto
# ---------------------------------------------------------------------------

class TestPublicFunctionsPassCorrectTask:
    _VALID_STATE_JSON = json.dumps({
        "temas": ["t1"], "pendientes": [], "propuestas": [], "citas": []
    })
    _VALID_MINUTES_JSON = json.dumps({
        "resumen": "r", "decisiones": [], "temas": [], "pendientes": [], "propuestas": [], "citas": []
    })

    @pytest.fixture()
    def captured_tasks(self, monkeypatch):
        captured = []

        def stub_chat_capture_task(messages, *, task="live", json_mode=False,
                                    temperature=0.2, max_tokens=1024, reasoning=False):
            captured.append(task)
            if json_mode:
                return self._VALID_STATE_JSON
            return "[stub]"

        _env(monkeypatch, INSIGHTS_BACKEND_LIVE="groq", INSIGHTS_BACKEND_BATCH="groq",
             INSIGHTS_BACKEND=None, GROQ_API_KEY="gsk-fake", INSIGHTS_ENABLED="true")
        monkeypatch.setattr(insights, "_chat", stub_chat_capture_task)
        return captured

    def test_update_state_uses_task_live(self, captured_tasks):
        insights.update_state({"temas": [], "pendientes": [], "propuestas": [], "citas": []},
                               "algo de transcript")
        assert captured_tasks and captured_tasks[-1] == "live"

    def test_consolidate_uses_task_live(self, captured_tasks):
        insights.consolidate("algo de transcript",
                              {"temas": [], "pendientes": [], "propuestas": [], "citas": []})
        assert captured_tasks and captured_tasks[-1] == "live"

    def test_generate_minutes_uses_task_batch(self, captured_tasks):
        insights.generate_minutes("algo de transcript")
        assert captured_tasks and captured_tasks[-1] == "batch"

    def test_chat_memory_uses_task_batch(self, captured_tasks):
        insights.chat_memory([{"role": "user", "content": "pregunta"}])
        assert captured_tasks and captured_tasks[-1] == "batch"


# ---------------------------------------------------------------------------
# Caso 4 — _model(task) devuelve el modelo del backend resuelto
# ---------------------------------------------------------------------------

class TestModelPerTask:
    def test_model_follows_resolved_backend_per_task(self, monkeypatch):
        _env(monkeypatch, INSIGHTS_BACKEND_LIVE="groq", INSIGHTS_BACKEND_BATCH="openrouter",
             INSIGHTS_BACKEND=None, OPENROUTER_MODEL="x/y",
             INSIGHTS_MODEL="llama-3.3-70b-versatile")
        assert insights._model("live") == "llama-3.3-70b-versatile"
        assert insights._model("batch") == "x/y"
