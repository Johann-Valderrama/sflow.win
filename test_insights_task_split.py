"""test_insights_task_split.py — Pruebas sin red para la selección de backend por tarea.

Casos:
  1. Precedencia de _resolve_backend
  2. Routing por task en _chat (dispatch a la rama correcta)
  3. Funciones públicas pasan el task correcto a _chat
  4. _model(task) devuelve el modelo del backend resuelto para esa tarea

Ejecutar: python test_insights_task_split.py  (exit 0 = PASS, exit 1 = FAIL)
"""
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stubs de dependencias opcionales
# ---------------------------------------------------------------------------
if "groq" not in sys.modules:
    groq_stub = types.ModuleType("groq")
    groq_stub.Groq = MagicMock()
    sys.modules["groq"] = groq_stub

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.post = MagicMock()

    class _RequestException(Exception):
        pass

    requests_stub.RequestException = _RequestException
    sys.modules["requests"] = requests_stub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PASS = "PASS"
FAIL = "FAIL"
_failures: list[str] = []


def ok(name: str, condition: bool, note: str = "") -> None:
    status = PASS if condition else FAIL
    suffix = f"  ({note})" if note else ""
    print(f"  [{status}] {name}{suffix}")
    if not condition:
        _failures.append(name)


# Env manager to set/restore env vars cleanly
class _EnvPatch:
    def __init__(self, **kw):
        self._kw = kw
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self._kw.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *_):
        for k, orig in self._saved.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig


# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Clean env before importing
_INSIGHT_KEYS = [
    "INSIGHTS_BACKEND", "INSIGHTS_BACKEND_LIVE", "INSIGHTS_BACKEND_BATCH",
    "INSIGHTS_ENABLED", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
    "INSIGHTS_MODEL", "INSIGHTS_ENDPOINT_MODEL", "INSIGHTS_ENDPOINT_URL",
]
_orig_env = {k: os.environ.get(k) for k in _INSIGHT_KEYS}
for k in _INSIGHT_KEYS:
    os.environ.pop(k, None)

import core.insights as insights  # noqa: E402


def _restore_env():
    for k, v in _orig_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Caso 1 — Precedencia de _resolve_backend
# ---------------------------------------------------------------------------
print("Caso 1: Precedencia _resolve_backend")

with _EnvPatch(INSIGHTS_BACKEND=None, INSIGHTS_BACKEND_LIVE=None, INSIGHTS_BACKEND_BATCH=None):
    # (a) sin ninguna env -> ambas "groq"
    ok("1a live sin env -> groq", insights._resolve_backend("live") == "groq",
       note=insights._resolve_backend("live"))
    ok("1a batch sin env -> groq", insights._resolve_backend("batch") == "groq",
       note=insights._resolve_backend("batch"))

with _EnvPatch(INSIGHTS_BACKEND="endpoint", INSIGHTS_BACKEND_LIVE=None, INSIGHTS_BACKEND_BATCH=None):
    # (b) solo INSIGHTS_BACKEND="endpoint" -> ambas heredan
    ok("1b live hereda global endpoint", insights._resolve_backend("live") == "endpoint",
       note=insights._resolve_backend("live"))
    ok("1b batch hereda global endpoint", insights._resolve_backend("batch") == "endpoint",
       note=insights._resolve_backend("batch"))

with _EnvPatch(INSIGHTS_BACKEND=None, INSIGHTS_BACKEND_LIVE="groq", INSIGHTS_BACKEND_BATCH="openrouter"):
    # (c) per-task configurados directamente
    ok("1c live=groq (per-task)", insights._resolve_backend("live") == "groq",
       note=insights._resolve_backend("live"))
    ok("1c batch=openrouter (per-task)", insights._resolve_backend("batch") == "openrouter",
       note=insights._resolve_backend("batch"))

with _EnvPatch(INSIGHTS_BACKEND="groq", INSIGHTS_BACKEND_LIVE=None, INSIGHTS_BACKEND_BATCH="openrouter"):
    # (d) per-task gana sobre global
    ok("1d batch per-task gana sobre global", insights._resolve_backend("batch") == "openrouter",
       note=insights._resolve_backend("batch"))
    ok("1d live hereda global groq", insights._resolve_backend("live") == "groq",
       note=insights._resolve_backend("live"))

# ---------------------------------------------------------------------------
# Caso 2 — Routing por task en _chat
# ---------------------------------------------------------------------------
print("\nCaso 2: Routing por task en _chat")

openrouter_called = {"count": 0}


def stub_openrouter(messages, *, task="live", json_mode=False, temperature=0.2, max_tokens=1024, reasoning=False):
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


with _EnvPatch(INSIGHTS_BACKEND_LIVE="openrouter", INSIGHTS_BACKEND_BATCH="groq",
               INSIGHTS_BACKEND=None, GROQ_API_KEY="gsk-fake",
               OPENROUTER_API_KEY="sk-test"):
    with patch.object(insights, "_chat_openrouter", side_effect=stub_openrouter), \
         patch.object(insights, "_get_groq_client", side_effect=stub_groq_client):
        # live -> openrouter
        openrouter_called["count"] = 0
        groq_called["count"] = 0
        result_live = insights._chat([{"role": "user", "content": "x"}], task="live")
        ok("2 live -> openrouter devuelve [OR]", result_live == "[OR]",
           note=repr(result_live))
        ok("2 live -> _chat_openrouter invocado", openrouter_called["count"] == 1,
           note=f"count={openrouter_called['count']}")
        ok("2 live -> groq NO invocado", groq_called["count"] == 0,
           note=f"count={groq_called['count']}")

        # batch -> groq
        openrouter_called["count"] = 0
        groq_called["count"] = 0
        result_batch = insights._chat([{"role": "user", "content": "x"}], task="batch")
        ok("2 batch -> groq devuelve [GROQ]", result_batch == "[GROQ]",
           note=repr(result_batch))
        ok("2 batch -> _chat_openrouter NO invocado", openrouter_called["count"] == 0,
           note=f"count={openrouter_called['count']}")
        ok("2 batch -> groq client invocado", groq_called["count"] == 1,
           note=f"count={groq_called['count']}")

# ---------------------------------------------------------------------------
# Caso 3 — Funciones públicas pasan el task correcto
# ---------------------------------------------------------------------------
print("\nCaso 3: Funciones públicas pasan task correcto a _chat")

captured_tasks: list[str] = []

# JSON mínimo para update_state y consolidate
_VALID_STATE_JSON = json.dumps({
    "temas": ["t1"], "pendientes": [], "propuestas": [], "citas": []
})
# JSON mínimo para generate_minutes
_VALID_MINUTES_JSON = json.dumps({
    "resumen": "r", "decisiones": [], "temas": [], "pendientes": [], "propuestas": [], "citas": []
})


def stub_chat_capture_task(messages, *, task="live", json_mode=False,
                           temperature=0.2, max_tokens=1024, reasoning=False):
    captured_tasks.append(task)
    # Devuelve un JSON válido mínimo para las funciones que lo parsean
    if json_mode:
        return _VALID_STATE_JSON
    return "[stub]"


with _EnvPatch(INSIGHTS_BACKEND_LIVE="groq", INSIGHTS_BACKEND_BATCH="groq",
               INSIGHTS_BACKEND=None, GROQ_API_KEY="gsk-fake", INSIGHTS_ENABLED="true"):
    with patch.object(insights, "_chat", side_effect=stub_chat_capture_task):
        # update_state -> task="live"
        captured_tasks.clear()
        insights.update_state({"temas": [], "pendientes": [], "propuestas": [], "citas": []},
                               "algo de transcript")
        ok("3 update_state usa task=live",
           len(captured_tasks) > 0 and captured_tasks[-1] == "live",
           note=f"tasks={captured_tasks}")

        # consolidate -> task="live"
        captured_tasks.clear()
        insights.consolidate("algo de transcript",
                              {"temas": [], "pendientes": [], "propuestas": [], "citas": []})
        ok("3 consolidate usa task=live",
           len(captured_tasks) > 0 and captured_tasks[-1] == "live",
           note=f"tasks={captured_tasks}")

        # generate_minutes -> task="batch"
        captured_tasks.clear()
        insights.generate_minutes("algo de transcript")
        ok("3 generate_minutes usa task=batch",
           len(captured_tasks) > 0 and captured_tasks[-1] == "batch",
           note=f"tasks={captured_tasks}")

        # chat_memory -> task="batch"
        captured_tasks.clear()
        insights.chat_memory([{"role": "user", "content": "pregunta"}])
        ok("3 chat_memory usa task=batch",
           len(captured_tasks) > 0 and captured_tasks[-1] == "batch",
           note=f"tasks={captured_tasks}")

# ---------------------------------------------------------------------------
# Caso 4 — _model(task) devuelve el modelo del backend resuelto
# ---------------------------------------------------------------------------
print("\nCaso 4: _model(task) devuelve modelo del backend correcto")

with _EnvPatch(INSIGHTS_BACKEND_LIVE="groq", INSIGHTS_BACKEND_BATCH="openrouter",
               INSIGHTS_BACKEND=None,
               OPENROUTER_MODEL="x/y",
               INSIGHTS_MODEL="llama-3.3-70b-versatile"):
    model_live = insights._model("live")
    model_batch = insights._model("batch")
    ok("4 _model(live) -> groq model",
       model_live == "llama-3.3-70b-versatile",
       note=repr(model_live))
    ok("4 _model(batch) -> openrouter model x/y",
       model_batch == "x/y",
       note=repr(model_batch))

# ---------------------------------------------------------------------------
# Cleanup + resultado
# ---------------------------------------------------------------------------
_restore_env()

print()
if _failures:
    print(f"RESULTADO: {len(_failures)} caso(s) FALLARON: {_failures}")
    sys.exit(1)
else:
    print("RESULTADO: Todos los casos pasaron.")
    sys.exit(0)
