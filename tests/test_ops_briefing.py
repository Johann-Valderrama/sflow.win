"""Tests para la unidad 7.1 'Copiloto con contexto OPS — briefing v1' (Ola 7).

Cubre:
  - core/ops_briefing.py: get_briefing()/invalidate(), TTL 60s, fail-open total
    (ausente, >8KB, no-UTF8, directorio), privacidad (nunca loguea contenido).
  - core/assistant.py build_context_live(): inyección condicional del briefing,
    válvula de sacrificio (transcript > briefing), gate de modo "silent".

Alcance v1 (no se toca aquí): SOLO el chat pull (answer_live/build_context_live).
El insight stream (core/insights.py) está fuera de esta unidad.

No llama a ningún LLM real ni arranca audio: MeetingSession se simula a mano
(mismo patrón que tests/test_assistant_live.py) y las llamadas a insights.chat_memory
se monkeypatchean cuando hace falta.
"""
import logging
import os

import pytest

from core import assistant, ops_briefing
from core.meeting import MeetingSession


# ---------------------------------------------------------------------------
# Helpers (mismo patrón que tests/test_assistant_live.py)
# ---------------------------------------------------------------------------

def _make_meeting(segments=None, insights=None, active=True):
    m = MeetingSession()
    m._active = active
    m._started_at = "2026-07-03 10:00:00"
    m._segments = list(segments or [])
    if insights is not None:
        m._insights = dict(insights)
    return m


def _seg(t, speaker, text):
    return {"t": float(t), "speaker": speaker, "text": text}


_EMPTY_INSIGHTS = {"temas": [], "pendientes": [], "propuestas": [], "citas": []}


@pytest.fixture(autouse=True)
def _reset_briefing_cache():
    """Cada test arranca con la caché del módulo invalidada (aislamiento entre tests)."""
    ops_briefing.invalidate()
    yield
    ops_briefing.invalidate()


@pytest.fixture(autouse=True)
def _copilot_mode(monkeypatch):
    """Por defecto el modo proactivo es 'copilot' (no gatea el briefing), salvo
    que un test lo sobreescriba explícitamente a 'silent'."""
    monkeypatch.setenv("PROACTIVE_MODE", "copilot")


# ---------------------------------------------------------------------------
# core/ops_briefing.py — API pública
# ---------------------------------------------------------------------------

class TestGetBriefing:
    def test_empty_path_returns_empty(self, monkeypatch):
        monkeypatch.delenv("OPS_BRIEFING_PATH", raising=False)
        assert ops_briefing.get_briefing() == ""

    def test_valid_file_returns_formatted_block(self, tmp_path, monkeypatch):
        f = tmp_path / "briefing.md"
        f.write_text("Proyecto VelOS: cierre Q3.", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        block = ops_briefing.get_briefing()
        assert "Proyecto VelOS: cierre Q3." in block
        assert "<<<BRIEFING" in block
        assert ">>>" in block
        assert "CONTEXTO DEL USUARIO" in block

    def test_missing_file_returns_empty_no_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(tmp_path / "no-existe.md"))
        assert ops_briefing.get_briefing() == ""

    def test_oversized_file_returns_empty_and_warns_without_content(self, tmp_path, monkeypatch, caplog):
        f = tmp_path / "grande.md"
        secret_marker = "SECRETO-QUE-NUNCA-DEBE-APARECER-EN-LOGS"
        f.write_text(secret_marker + ("x" * 9000), encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        with caplog.at_level(logging.DEBUG):
            result = ops_briefing.get_briefing()
        assert result == ""
        for record in caplog.records:
            assert secret_marker not in record.getMessage()

    def test_non_utf8_file_returns_empty_no_exception(self, tmp_path, monkeypatch):
        f = tmp_path / "binario.md"
        f.write_bytes(b"\xff\xfe\x00\xd8\x00\xdc\xff\xff\x80\x81")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        assert ops_briefing.get_briefing() == ""

    def test_directory_path_returns_empty_no_exception(self, tmp_path, monkeypatch):
        d = tmp_path / "una_carpeta"
        d.mkdir()
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(d))
        assert ops_briefing.get_briefing() == ""

    def test_empty_file_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "vacio.md"
        f.write_text("", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        assert ops_briefing.get_briefing() == ""

    def test_utf8_bom_is_stripped(self, tmp_path, monkeypatch):
        f = tmp_path / "bom.md"
        f.write_bytes(b"\xef\xbb\xbf" + "Contenido con BOM".encode("utf-8"))
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        block = ops_briefing.get_briefing()
        assert "Contenido con BOM" in block
        assert "﻿" not in block

    def test_quoted_path_is_tolerated(self, tmp_path, monkeypatch):
        f = tmp_path / "quoted.md"
        f.write_text("hola", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", f'"{f}"')
        assert "hola" in ops_briefing.get_briefing()


# ---------------------------------------------------------------------------
# Recarga: TTL 60s + invalidate()
# ---------------------------------------------------------------------------

class TestReload:
    def test_change_not_visible_until_ttl_or_invalidate(self, tmp_path, monkeypatch):
        f = tmp_path / "briefing.md"
        f.write_text("version 1", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))

        fake_now = [1000.0]
        monkeypatch.setattr(ops_briefing.time, "monotonic", lambda: fake_now[0])

        assert "version 1" in ops_briefing.get_briefing()

        # Cambia el archivo pero el reloj apenas avanzó: sigue viendo la v1 (caché).
        f.write_text("version 2", encoding="utf-8")
        fake_now[0] += 10.0
        assert "version 1" in ops_briefing.get_briefing()
        assert "version 2" not in ops_briefing.get_briefing()

        # Tras invalidate() explícito, se ve el cambio de inmediato (sin esperar TTL).
        ops_briefing.invalidate()
        assert "version 2" in ops_briefing.get_briefing()

    def test_change_visible_after_ttl_expires(self, tmp_path, monkeypatch):
        f = tmp_path / "briefing.md"
        f.write_text("version 1", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))

        fake_now = [2000.0]
        monkeypatch.setattr(ops_briefing.time, "monotonic", lambda: fake_now[0])
        assert "version 1" in ops_briefing.get_briefing()

        f.write_text("version 2", encoding="utf-8")
        fake_now[0] += 61.0  # supera el TTL de 60s
        assert "version 2" in ops_briefing.get_briefing()


# ---------------------------------------------------------------------------
# build_context_live — inyección condicional + válvula + gate
# ---------------------------------------------------------------------------

class TestBuildContextLiveInjection:
    def test_disabled_by_default_no_block_in_context(self, monkeypatch):
        monkeypatch.delenv("OPS_BRIEFING_PATH", raising=False)
        m = _make_meeting([_seg(5, "Yo", "hola equipo")], _EMPTY_INSIGHTS)
        ctx, meta = assistant.build_context_live("q", meeting=m)
        assert "CONTEXTO DEL USUARIO" not in ctx
        assert meta["briefing_included"] is False

    def test_valid_path_injects_block(self, tmp_path, monkeypatch):
        f = tmp_path / "briefing.md"
        f.write_text("Compromiso: entregar demo el viernes.", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        m = _make_meeting([_seg(5, "Yo", "hola equipo")], _EMPTY_INSIGHTS)
        ctx, meta = assistant.build_context_live("q", meeting=m)
        assert "Compromiso: entregar demo el viernes." in ctx
        assert meta["briefing_included"] is True

    def test_silent_mode_gates_briefing_even_if_file_valid(self, tmp_path, monkeypatch):
        f = tmp_path / "briefing.md"
        f.write_text("Dato privado que no debe salir en pantalla compartida.", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        m = _make_meeting([_seg(5, "Yo", "hola equipo")], _EMPTY_INSIGHTS)
        ctx, meta = assistant.build_context_live("q", meeting=m)
        assert "Dato privado" not in ctx
        assert "CONTEXTO DEL USUARIO" not in ctx
        assert meta["briefing_included"] is False

    def test_small_budget_sacrifices_briefing_keeps_transcript(self, tmp_path, monkeypatch):
        """Válvula de sacrificio: budget chico + transcript grande → el briefing se
        descarta, el transcript reciente se conserva, avail nunca queda negativo."""
        f = tmp_path / "briefing.md"
        f.write_text("Briefing largo: " + ("contexto " * 200), encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        segs = [_seg(i * 10, "Yo", f"intervención número {i} " + ("bla " * 25))
                for i in range(500)]
        m = _make_meeting(segs, _EMPTY_INSIGHTS)
        ctx, meta = assistant.build_context_live("resumen", budget=2000, meeting=m)
        assert len(ctx) <= 2000
        assert meta["briefing_included"] is False
        assert "CONTEXTO DEL USUARIO" not in ctx
        # El transcript manda: se conserva la cola (más reciente).
        assert "intervención número 499" in ctx

    def test_generous_budget_includes_briefing_and_truncates_transcript(self, tmp_path, monkeypatch):
        """Con presupuesto holgado, el briefing SÍ entra y es el transcript el que
        se recorta (nunca al revés)."""
        f = tmp_path / "briefing.md"
        f.write_text("Proyecto activo: VelOS fase 2.", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        segs = [_seg(i * 10, "Yo", f"intervención número {i} " + ("bla " * 25))
                for i in range(500)]
        m = _make_meeting(segs, _EMPTY_INSIGHTS)
        ctx, meta = assistant.build_context_live("resumen", budget=18000, meeting=m)
        assert len(ctx) <= 18000
        assert meta["briefing_included"] is True
        assert "Proyecto activo: VelOS fase 2." in ctx
        assert meta["truncated"] is True
        assert "intervención número 499" in ctx
        assert "intervención número 0 " not in ctx

    def test_empty_transcript_does_not_force_llm_even_with_briefing(self, tmp_path, monkeypatch):
        """El briefing no debe forzar una llamada LLM cuando no hay transcript."""
        f = tmp_path / "briefing.md"
        f.write_text("Contexto del usuario.", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))

        def _boom(*a, **k):
            raise AssertionError("chat_memory NO debe llamarse con transcript vacío")
        monkeypatch.setattr(assistant.insights, "chat_memory", _boom)

        m = _make_meeting([], _EMPTY_INSIGHTS)
        res = assistant.answer_live("¿algo?", meeting=m)
        assert res["ok"] is True
        assert res["empty"] is True


class TestAnswerLiveConditionalInstruction:
    def test_instruction_added_when_briefing_included(self, tmp_path, monkeypatch):
        f = tmp_path / "briefing.md"
        f.write_text("Meta: cerrar Q3.", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["system"] = messages[0]["content"]
            return "ok"

        monkeypatch.setattr(assistant.insights, "chat_memory", _fake_chat)
        m = _make_meeting([_seg(5, "Yo", "hablamos del cierre")], _EMPTY_INSIGHTS)
        res = assistant.answer_live("q", meeting=m)
        assert res["ok"] is True
        assert "CONECTAR lo hablado" in captured["system"]
        assert "Meta: cerrar Q3." in captured["system"]

    def test_instruction_absent_when_briefing_disabled(self, monkeypatch):
        monkeypatch.delenv("OPS_BRIEFING_PATH", raising=False)
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["system"] = messages[0]["content"]
            return "ok"

        monkeypatch.setattr(assistant.insights, "chat_memory", _fake_chat)
        m = _make_meeting([_seg(5, "Yo", "hola")], _EMPTY_INSIGHTS)
        res = assistant.answer_live("q", meeting=m)
        assert res["ok"] is True
        assert "CONECTAR lo hablado" not in captured["system"]

    def test_instruction_absent_when_gated_by_silent_mode(self, tmp_path, monkeypatch):
        f = tmp_path / "briefing.md"
        f.write_text("Meta privada.", encoding="utf-8")
        monkeypatch.setenv("OPS_BRIEFING_PATH", str(f))
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        captured = {}

        def _fake_chat(messages, **kwargs):
            captured["system"] = messages[0]["content"]
            return "ok"

        monkeypatch.setattr(assistant.insights, "chat_memory", _fake_chat)
        m = _make_meeting([_seg(5, "Yo", "hola")], _EMPTY_INSIGHTS)
        res = assistant.answer_live("q", meeting=m)
        assert res["ok"] is True
        assert "CONECTAR lo hablado" not in captured["system"]
        assert "Meta privada." not in captured["system"]
