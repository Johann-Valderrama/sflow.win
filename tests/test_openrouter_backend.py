"""Tests sin red para el backend OpenRouter de insights.

Casos:
  1. _chat despacha a _chat_openrouter, captura URL/header/body y devuelve el stub.
  2. is_available() con backend=openrouter: True si key presente, False si no.
  3. _model() devuelve OPENROUTER_MODEL cuando backend=openrouter.
  4. No regresión: con backend=groq, _chat_openrouter NO se invoca.

Ejecutar: python test_openrouter_backend.py  (exit 0 = PASS, exit 1 = FAIL)
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stubs de dependencias opcionales que pueden no estar instaladas en el env
# de test (ej. groq SDK). Solo se stubbean si de verdad FALTA el paquete real
# (try/except ImportError), nunca por un "if 'x' not in sys.modules" — ese
# check es frágil dentro de una suite pytest compartida (tests/): el orden de
# colección determina si el paquete real ya fue importado por OTRO archivo, y
# si este módulo se colecciona antes, el check reemplaza permanentemente el
# 'requests' real por un MagicMock para el resto de la sesión (rompía
# tests/test_webhook.py, que necesita un requests.post real). Fix U2.2.
# ---------------------------------------------------------------------------

try:
    import groq  # noqa: F401
except ImportError:
    groq_stub = types.ModuleType("groq")
    groq_stub.Groq = MagicMock()
    sys.modules["groq"] = groq_stub

try:
    import requests  # noqa: F401
except ImportError:
    requests_stub = types.ModuleType("requests")
    requests_stub.post = MagicMock()

    class _RequestException(Exception):
        pass

    requests_stub.RequestException = _RequestException
    sys.modules["requests"] = requests_stub

try:
    import config  # noqa: F401
except ImportError:
    config_stub = types.ModuleType("config")
    sys.modules["config"] = config_stub

# Ahora importamos insights con el env limpio
import importlib

# Asegurar que insights se reimporta desde cero en cada test que cambie el backend
def _reload_insights():
    import core.insights as m
    importlib.reload(m)
    return m


# ---------------------------------------------------------------------------
# Helper: respuesta OpenAI-shape devuelta por el stub HTTP
# ---------------------------------------------------------------------------

def _openai_response(content: str):
    """Devuelve un MagicMock que imita requests.Response con shape OpenAI."""
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Entorno base: vars que NO deben interferir entre tests
# ---------------------------------------------------------------------------

BASE_ENV = {
    "GROQ_API_KEY": "",
    "INSIGHTS_ENABLED": "true",
    "OPENROUTER_API_KEY": "",
    "OPENROUTER_MODEL": "google/gemini-2.5-flash",
    "OPENROUTER_BASE_URL": "https://openrouter.ai/api/v1",
    "INSIGHTS_BACKEND": "groq",
    # Vacíos a propósito: en el entorno real de este proyecto el .env configura
    # INSIGHTS_BACKEND_LIVE/BATCH="claude-cli" (uso de la suscripción Claude Max,
    # ver memoria del proyecto). _resolve_backend() da precedencia al per-task
    # sobre el global — sin limpiar esto aquí, estos tests terminan invocando el
    # CLI real de Claude en vez del backend mockeado que cada caso pretende
    # ejercitar. Fix U2.2 (hermeticidad al mover este archivo a tests/).
    "INSIGHTS_BACKEND_LIVE": "",
    "INSIGHTS_BACKEND_BATCH": "",
    "INSIGHTS_ENDPOINT_URL": "http://localhost:1234/v1",
    "INSIGHTS_ENDPOINT_KEY": "lm-studio",
    "INSIGHTS_ENDPOINT_MODEL": "qwen/qwen2.5-vl-7b",
    "INSIGHTS_MODEL": "llama-3.3-70b-versatile",
}


class TestOpenRouterBackend(unittest.TestCase):

    def setUp(self):
        """Guarda el entorno original y aplica BASE_ENV."""
        self._orig_env = {k: os.environ.get(k) for k in BASE_ENV}
        for k, v in BASE_ENV.items():
            os.environ[k] = v

    def tearDown(self):
        """Restaura el entorno original."""
        for k, orig in self._orig_env.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig

    # ------------------------------------------------------------------
    # Caso 1: _chat despacha correctamente a OpenRouter
    # ------------------------------------------------------------------

    def test_chat_dispatches_to_openrouter_and_returns_stub(self):
        os.environ["INSIGHTS_BACKEND"] = "openrouter"
        os.environ["OPENROUTER_API_KEY"] = "sk-test"
        os.environ["OPENROUTER_MODEL"] = "test/model"

        import core.insights as insights

        captured = {}

        def fake_post(url, *, json=None, headers=None, timeout=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["body"] = json or {}
            return _openai_response("[stub-openrouter]")

        # Parchear el módulo requests que insights importa internamente
        import requests as _requests_mod
        with patch.object(_requests_mod, "post", side_effect=fake_post):
            result = insights._chat([{"role": "user", "content": "hola"}])

        # (a) URL apunta a openrouter.ai
        self.assertIn("openrouter.ai/api/v1/chat/completions", captured["url"],
                      f"URL inesperada: {captured['url']}")

        # (b) Authorization header correcto
        auth = captured["headers"].get("Authorization", "")
        self.assertEqual(auth, "Bearer sk-test",
                         f"Header Authorization inesperado: {auth!r}")

        # (c) Modelo en el body
        self.assertEqual(captured["body"].get("model"), "test/model",
                         f"Modelo en body inesperado: {captured['body'].get('model')!r}")

        # (d) Resultado
        self.assertEqual(result, "[stub-openrouter]",
                         f"Resultado inesperado: {result!r}")

        print("PASS caso 1 — dispatch a OpenRouter, URL/header/body/resultado OK")

    # ------------------------------------------------------------------
    # Caso 2: is_available() con backend=openrouter
    # ------------------------------------------------------------------

    def test_is_available_openrouter(self):
        import core.insights as insights

        os.environ["INSIGHTS_BACKEND"] = "openrouter"

        # Con key presente → True
        os.environ["OPENROUTER_API_KEY"] = "sk-real"
        self.assertTrue(insights.is_available(),
                        "is_available() debería ser True con OPENROUTER_API_KEY presente")

        # Sin key → False
        os.environ["OPENROUTER_API_KEY"] = ""
        self.assertFalse(insights.is_available(),
                         "is_available() debería ser False sin OPENROUTER_API_KEY")

        print("PASS caso 2 — is_available() True/False según OPENROUTER_API_KEY")

    # ------------------------------------------------------------------
    # Caso 3: _model() devuelve OPENROUTER_MODEL
    # ------------------------------------------------------------------

    def test_model_returns_openrouter_model(self):
        import core.insights as insights

        os.environ["INSIGHTS_BACKEND"] = "openrouter"
        os.environ["OPENROUTER_MODEL"] = "anthropic/claude-sonnet-4"

        result = insights._model()
        self.assertEqual(result, "anthropic/claude-sonnet-4",
                         f"_model() devolvió {result!r} en lugar de 'anthropic/claude-sonnet-4'")

        print("PASS caso 3 — _model() devuelve OPENROUTER_MODEL")

    # ------------------------------------------------------------------
    # Caso 4: no regresión — backend=groq NO llama a _chat_openrouter
    # ------------------------------------------------------------------

    def test_no_regression_groq_does_not_call_openrouter(self):
        os.environ["INSIGHTS_BACKEND"] = "groq"
        os.environ["GROQ_API_KEY"] = "gsk-fake"

        import core.insights as insights

        openrouter_called = {"flag": False}
        original_openrouter = insights._chat_openrouter

        def spy_openrouter(*args, **kwargs):
            openrouter_called["flag"] = True
            return original_openrouter(*args, **kwargs)

        # Stub del cliente Groq para que no falle
        fake_msg = MagicMock()
        fake_msg.content = "groq-response"
        fake_choice = MagicMock()
        fake_choice.message = fake_msg
        fake_completion = MagicMock()
        fake_completion.choices = [fake_choice]
        fake_groq_client = MagicMock()
        fake_groq_client.chat.completions.create.return_value = fake_completion

        with patch.object(insights, "_chat_openrouter", side_effect=spy_openrouter), \
             patch.object(insights, "_get_groq_client", return_value=fake_groq_client):
            result = insights._chat([{"role": "user", "content": "test"}])

        self.assertFalse(openrouter_called["flag"],
                         "_chat_openrouter fue invocada con backend=groq (no debería)")
        self.assertEqual(result, "groq-response",
                         f"Resultado groq inesperado: {result!r}")

        print("PASS caso 4 — backend=groq no toca _chat_openrouter")

    # ------------------------------------------------------------------
    # Caso 5: sin OPENROUTER_API_KEY lanza InsightsUnavailable
    # ------------------------------------------------------------------

    def test_missing_key_raises_insightsunavailable(self):
        os.environ["INSIGHTS_BACKEND"] = "openrouter"
        os.environ["OPENROUTER_API_KEY"] = ""

        import core.insights as insights

        with self.assertRaises(insights.InsightsUnavailable):
            insights._chat_openrouter(
                [{"role": "user", "content": "test"}],
                json_mode=False, temperature=0.2, max_tokens=100
            )

        print("PASS caso 5 — InsightsUnavailable sin OPENROUTER_API_KEY")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestOpenRouterBackend)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("\n=== ALL PASS ===")
        sys.exit(0)
    else:
        print(f"\n=== FAILED ({len(result.failures)} failures, {len(result.errors)} errors) ===")
        sys.exit(1)
