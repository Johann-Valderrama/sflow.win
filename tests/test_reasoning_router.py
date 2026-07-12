# -*- coding: utf-8 -*-
"""test_reasoning_router.py -- Tests para el router de razonamiento de 2 niveles.

Sin red: monkeypatch de requests.post e insights.chat_memory/_chat.
Ejecutar: python test_reasoning_router.py
Exit code 0 = todos PASS, != 0 = algun FAIL.
"""
from __future__ import annotations

import os
import sys
import unittest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pass(name: str) -> None:
    print("  PASS  " + name)


def _fail(name: str, reason: str = "") -> None:
    msg = "  FAIL  " + name
    if reason:
        msg += ": " + reason
    print(msg)


# ---------------------------------------------------------------------------
# Fake OpenAI-shape response
# ---------------------------------------------------------------------------

def _make_openrouter_response(content: str = "respuesta canned"):
    """Objeto que imita requests.Response con json() valido."""

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    return _Resp()


# ---------------------------------------------------------------------------
# Suite 1: payload reasoning en _chat_openrouter
# ---------------------------------------------------------------------------

class TestReasoningPayload(unittest.TestCase):

    _ENV_KEYS = (
        "INSIGHTS_BACKEND_BATCH", "INSIGHTS_BACKEND", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
    )

    def setUp(self) -> None:
        # Fix U2.2 (hermeticidad al mover este archivo a tests/): el setUp original
        # no tenía tearDown, así que estas env vars quedaban puestas para el resto
        # de la sesión de pytest y contaminaban archivos que corren después
        # alfabéticamente (p. ej. tests/test_webhook.py).
        self._orig_env = {k: os.environ.get(k) for k in self._ENV_KEYS}
        os.environ["INSIGHTS_BACKEND_BATCH"] = "openrouter"
        os.environ["INSIGHTS_BACKEND"] = "openrouter"
        os.environ["OPENROUTER_API_KEY"] = "fake-key-test"
        os.environ["OPENROUTER_MODEL"] = "google/gemini-3.1-flash-lite"

    def tearDown(self) -> None:
        for k, orig in self._orig_env.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig

    def _run_with_fake_requests(self, reasoning_flag: bool):
        import builtins
        import core.insights as ins

        captured: list[dict] = []
        original_import = builtins.__import__

        class FakeRequests:
            @staticmethod
            def post(url, *, json=None, headers=None, timeout=None):
                captured.append(dict(json or {}))
                return _make_openrouter_response()

        def mock_import(name, *args, **kwargs):
            if name == "requests":
                return FakeRequests
            return original_import(name, *args, **kwargs)

        builtins.__import__ = mock_import
        try:
            ins._chat(
                [{"role": "user", "content": "test"}],
                task="batch",
                reasoning=reasoning_flag,
            )
        finally:
            builtins.__import__ = original_import

        return captured

    def test_reasoning_true_adds_key(self) -> None:
        """Con reasoning=True, el payload incluye la clave 'reasoning' con enabled=True."""
        captured = self._run_with_fake_requests(True)
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertIn("reasoning", payload)
        self.assertTrue(payload["reasoning"].get("enabled") is True)
        _pass("reasoning=True -> payload tiene 'reasoning' con enabled=True")

    def test_reasoning_false_no_key(self) -> None:
        """Con reasoning=False, el payload NO incluye la clave 'reasoning'."""
        captured = self._run_with_fake_requests(False)
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertNotIn("reasoning", payload)
        _pass("reasoning=False -> payload NO tiene clave 'reasoning'")


# ---------------------------------------------------------------------------
# Suite 2: _needs_reasoning heuristica
# ---------------------------------------------------------------------------

class TestNeedsReasoning(unittest.TestCase):

    def _check(self, msg: str, expected: bool, label: str | None = None) -> None:
        from core.assistant import _needs_reasoning

        result = _needs_reasoning(msg)
        lbl = label or msg[:60]
        if result == expected:
            _pass("_needs_reasoning(" + repr(lbl) + ") -> " + str(expected))
        else:
            _fail(
                "_needs_reasoning(" + repr(lbl) + ")",
                "esperado " + str(expected) + ", got " + str(result),
            )
            self.fail("Valor inesperado para: " + lbl)

    # --- analiticas -> True ---
    def test_compara(self) -> None:
        self._check("compara las reuniones del cliente", True)

    def test_compara_acento(self) -> None:
        self._check("compára las reuniones del cliente", True, "compara (acento)")

    def test_estrategia(self) -> None:
        self._check("qué estrategia recomiendas", True)

    def test_pros_contras(self) -> None:
        self._check("analiza los pros y contras de esta opción", True)

    def test_por_que(self) -> None:
        self._check("por qué falló el proyecto", True)

    def test_plan_accion(self) -> None:
        self._check("redáctame un plan de acción para el cliente", True)

    # --- simples -> False ---
    def test_lista_pendientes(self) -> None:
        self._check("lista mis pendientes", False)

    def test_resume(self) -> None:
        self._check("resume la última reunión", False)

    def test_cuales_citas(self) -> None:
        self._check("cuáles son mis citas de esta semana", False)

    def test_que_decidimos(self) -> None:
        self._check("qué decidimos en la reunión del lunes", False)


# ---------------------------------------------------------------------------
# Suite 3: resolucion en answer()
# ---------------------------------------------------------------------------

class TestAnswerResolution(unittest.TestCase):

    def _make_db_stub(self):
        class _DB:
            def meetings_index(self):
                return []

            def meeting_get(self, mid):
                return None

            def meetings_search(self, q, limit=3):
                return []

        return _DB()

    def _patch_chat_memory(self, captured: list):
        import core.insights as ins

        original = ins.chat_memory

        def fake_chat_memory(
            messages, *, max_tokens=1024, temperature=0.3, reasoning=False
        ):
            captured.append(
                {"max_tokens": max_tokens, "temperature": temperature, "reasoning": reasoning}
            )
            return "respuesta canned"

        ins.chat_memory = fake_chat_memory
        return original

    def _restore_chat_memory(self, original) -> None:
        import core.insights as ins

        ins.chat_memory = original

    def test_reasoning_true_forced(self) -> None:
        """answer(reasoning=True) -> chat_memory recibe reasoning=True y result['reasoned'] is True."""
        import core.assistant as asst

        captured: list[dict] = []
        orig = self._patch_chat_memory(captured)
        try:
            result = asst.answer(
                self._make_db_stub(), "lista mis pendientes", reasoning=True
            )
        finally:
            self._restore_chat_memory(orig)

        self.assertTrue(captured[0]["reasoning"] is True)
        self.assertTrue(result.get("reasoned") is True)
        _pass("answer(reasoning=True) -> chat_memory=True, result.reasoned=True")

    def test_reasoning_false_forced(self) -> None:
        """answer(reasoning=False) -> chat_memory recibe reasoning=False y result['reasoned'] is False."""
        import core.assistant as asst

        captured: list[dict] = []
        orig = self._patch_chat_memory(captured)
        try:
            result = asst.answer(
                self._make_db_stub(), "analiza la estrategia", reasoning=False
            )
        finally:
            self._restore_chat_memory(orig)

        self.assertFalse(captured[0]["reasoning"])
        self.assertFalse(result.get("reasoned"))
        _pass("answer(reasoning=False) -> chat_memory=False, result.reasoned=False")

    def test_reasoning_auto_analytical(self) -> None:
        """answer(reasoning='auto') + mensaje analitico -> chat_memory=True."""
        import core.assistant as asst

        captured: list[dict] = []
        orig = self._patch_chat_memory(captured)
        try:
            result = asst.answer(
                self._make_db_stub(),
                "compara las reuniones del cliente y recomienda estrategia",
                reasoning="auto",
            )
        finally:
            self._restore_chat_memory(orig)

        self.assertTrue(captured[0]["reasoning"] is True)
        self.assertTrue(result.get("reasoned") is True)
        _pass("answer(auto, analitico) -> reasoning=True")

    def test_reasoning_auto_simple(self) -> None:
        """answer(reasoning='auto') + mensaje simple -> chat_memory=False."""
        import core.assistant as asst

        captured: list[dict] = []
        orig = self._patch_chat_memory(captured)
        try:
            result = asst.answer(
                self._make_db_stub(),
                "lista mis pendientes de la semana",
                reasoning="auto",
            )
        finally:
            self._restore_chat_memory(orig)

        self.assertFalse(captured[0]["reasoning"])
        self.assertFalse(result.get("reasoned"))
        _pass("answer(auto, simple) -> reasoning=False")


# ---------------------------------------------------------------------------
# Suite 4: holgura de tokens en chat_memory
# ---------------------------------------------------------------------------

class TestTokenSlack(unittest.TestCase):

    def _patch_chat(self, captured: list):
        import core.insights as ins

        original = ins._chat

        def fake_chat(
            messages,
            *,
            task="live",
            json_mode=False,
            temperature=0.2,
            max_tokens=1024,
            reasoning=False,
        ):
            captured.append({"max_tokens": max_tokens, "reasoning": reasoning})
            return "ok"

        ins._chat = fake_chat
        return original

    def _restore_chat(self, original) -> None:
        import core.insights as ins

        ins._chat = original

    def test_reasoning_true_slack(self) -> None:
        """chat_memory(reasoning=True, max_tokens=1024) -> _chat recibe max_tokens >= 2048."""
        import core.insights as ins

        captured: list[dict] = []
        orig = self._patch_chat(captured)
        try:
            ins.chat_memory(
                [{"role": "user", "content": "test"}],
                reasoning=True,
                max_tokens=1024,
            )
        finally:
            self._restore_chat(orig)

        actual = captured[0]["max_tokens"]
        self.assertGreaterEqual(actual, 2048)
        _pass(
            "chat_memory(reasoning=True, max_tokens=1024) -> _chat max_tokens="
            + str(actual)
            + " >= 2048"
        )

    def test_reasoning_false_no_slack(self) -> None:
        """chat_memory(reasoning=False, max_tokens=1024) -> _chat recibe max_tokens == 1024."""
        import core.insights as ins

        captured: list[dict] = []
        orig = self._patch_chat(captured)
        try:
            ins.chat_memory(
                [{"role": "user", "content": "test"}],
                reasoning=False,
                max_tokens=1024,
            )
        finally:
            self._restore_chat(orig)

        actual = captured[0]["max_tokens"]
        self.assertEqual(actual, 1024)
        _pass(
            "chat_memory(reasoning=False, max_tokens=1024) -> _chat max_tokens="
            + str(actual)
            + " == 1024"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("test_reasoning_router.py")
    print("=" * 60)

    loader = unittest.TestLoader()
    suites = [
        ("Suite 1: payload reasoning (_chat_openrouter)", TestReasoningPayload),
        ("Suite 2: _needs_reasoning heuristica", TestNeedsReasoning),
        ("Suite 3: resolucion en answer()", TestAnswerResolution),
        ("Suite 4: holgura de tokens (chat_memory)", TestTokenSlack),
    ]

    total_failures = 0
    for suite_name, suite_class in suites:
        print("\n" + suite_name)
        suite = loader.loadTestsFromTestCase(suite_class)
        result = unittest.TestResult()
        suite.run(result)
        total_failures += len(result.failures) + len(result.errors)
        for _, tb in result.errors:
            print("  ERROR: " + tb.splitlines()[-1])
        for _, tb in result.failures:
            print("  FAIL detail: " + tb.splitlines()[-1])

    print("\n" + "=" * 60)
    if total_failures == 0:
        print("ALL PASS")
    else:
        print("FAILURES: " + str(total_failures))
    print("=" * 60)
    sys.exit(0 if total_failures == 0 else 1)
