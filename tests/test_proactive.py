"""Tests para la unidad 5.3 'Timing, superficies y HUD' (core/proactive.py).

Módulo puro (sin Qt, sin threading, sin LLM): se ejercita con timestamps
sintéticos (parámetro `now`) para no depender de sleeps reales.
"""
import os

import pytest

from core import proactive


# ---------------------------------------------------------------------------
# get_mode
# ---------------------------------------------------------------------------

class TestGetMode:
    def test_default_is_copilot(self, monkeypatch):
        monkeypatch.delenv("PROACTIVE_MODE", raising=False)
        assert proactive.get_mode() == "copilot"

    def test_override_silent(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        assert proactive.get_mode() == "silent"

    def test_override_trainer(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "trainer")
        assert proactive.get_mode() == "trainer"

    def test_invalid_value_falls_back_to_copilot(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "bogus")
        assert proactive.get_mode() == "copilot"


# ---------------------------------------------------------------------------
# ProactiveGate.should_push / mark_pushed
# ---------------------------------------------------------------------------

class TestShouldPush:
    def test_pendiente_always_true(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "silent")  # incluso en silent
        gate = proactive.ProactiveGate()
        assert gate.should_push("pendiente") is True

    def test_silent_mode_blocks_nonpendiente(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "silent")
        gate = proactive.ProactiveGate()
        assert gate.should_push("deteccion") is False

    def test_budget_300s_between_nonpendiente_pushes(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        gate = proactive.ProactiveGate()
        t0 = 1000.0
        assert gate.should_push("deteccion", now=t0) is True
        gate.mark_pushed("deteccion", now=t0)
        # 100s después: dentro del presupuesto, bloqueado
        assert gate.should_push("coaching", now=t0 + 100) is False
        # 300s después: presupuesto liberado
        assert gate.should_push("coaching", now=t0 + 300) is True

    def test_mark_pushed_pendiente_no_consume_budget(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        gate = proactive.ProactiveGate()
        gate.mark_pushed("pendiente", now=1000.0)
        # Un pendiente no consume el presupuesto de no-pendientes.
        assert gate.should_push("deteccion", now=1000.1) is True


# ---------------------------------------------------------------------------
# Cola de lull (enqueue / pop_deliverable)
# ---------------------------------------------------------------------------

class TestQueue:
    def test_delivers_on_lull(self):
        gate = proactive.ProactiveGate()
        gate.enqueue({"key": "k1", "tipo": "deteccion", "texto": "x"}, now=0.0)
        assert gate.pop_deliverable(lull=False, now=1.0) is None
        card = gate.pop_deliverable(lull=True, now=1.0)
        assert card is not None
        assert card["key"] == "k1"

    def test_forced_delivery_after_60s(self):
        gate = proactive.ProactiveGate()
        gate.enqueue({"key": "k1", "tipo": "deteccion", "texto": "x"}, now=0.0)
        assert gate.pop_deliverable(lull=False, now=59.9) is None
        card = gate.pop_deliverable(lull=False, now=60.0)
        assert card is not None
        assert card["key"] == "k1"

    def test_expires_after_180s(self):
        gate = proactive.ProactiveGate()
        # Encolar y purgar manualmente sin pasar por pop_deliverable (que a los
        # >=60s SIEMPRE entrega): se ejercita _purge_expired vía una segunda
        # tarjeta que queda detrás y expira sin ser nunca la más vieja entregable.
        gate._queue.append({"key": "k1", "tipo": "deteccion", "texto": "x", "queued_at": 0.0})
        gate._purge_expired(now=179.9)
        assert gate.queue_size() == 1
        gate._purge_expired(now=180.0)
        assert gate.queue_size() == 0
        assert "k1" in gate._discarded_keys

    def test_dedup_by_key_delivered(self):
        gate = proactive.ProactiveGate()
        gate.enqueue({"key": "k1", "tipo": "deteccion", "texto": "x"}, now=0.0)
        card = gate.pop_deliverable(lull=True, now=1.0)
        assert card is not None
        # Re-encolar la misma key tras entregada: se ignora.
        gate.enqueue({"key": "k1", "tipo": "deteccion", "texto": "x"}, now=2.0)
        assert gate.queue_size() == 0
        assert gate.pop_deliverable(lull=True, now=3.0) is None

    def test_dedup_by_key_discarded(self):
        gate = proactive.ProactiveGate()
        gate.enqueue({"key": "k1", "tipo": "deteccion", "texto": "x"}, now=0.0)
        gate.pop_deliverable(lull=False, now=200.0)  # expira, descartada
        assert gate.queue_size() == 0
        gate.enqueue({"key": "k1", "tipo": "deteccion", "texto": "x"}, now=201.0)
        assert gate.queue_size() == 0  # nunca reaparece

    def test_reset_clears_everything(self, monkeypatch):
        monkeypatch.setenv("PROACTIVE_MODE", "copilot")
        gate = proactive.ProactiveGate()
        t0 = 1000.0
        gate.enqueue({"key": "k1", "tipo": "deteccion", "texto": "x"}, now=t0)
        gate.mark_pushed("deteccion", now=t0)
        assert gate.should_push("coaching", now=t0 + 0.1) is False  # dentro del presupuesto, antes del reset
        gate.reset()
        assert gate.queue_size() == 0
        # Tras reset, el cooldown de presupuesto también se limpia: un `now` muy
        # posterior al t0 original (aquí 0.0, tras el reset) ya no está bloqueado.
        assert gate.should_push("coaching", now=t0 + 0.1) is True


# ---------------------------------------------------------------------------
# LullDetector
# ---------------------------------------------------------------------------

class TestLullDetector:
    def test_requires_two_ticks(self):
        det = proactive.LullDetector()
        assert det.update(0.001, 0.001) is False  # 1er tick: aún no
        assert det.update(0.001, 0.001) is True   # 2do tick: lull sostenido

    def test_noise_resets_counter(self):
        det = proactive.LullDetector()
        assert det.update(0.001, 0.001) is False
        assert det.update(0.5, 0.001) is False  # alguien habla: resetea
        assert det.update(0.001, 0.001) is False  # necesita 2 ticks de nuevo
        assert det.update(0.001, 0.001) is True

    def test_is_lull_pure_function(self):
        assert proactive.is_lull(0.001, 0.001) is True
        assert proactive.is_lull(0.5, 0.001) is False
        assert proactive.is_lull(0.001, 0.5) is False


# ---------------------------------------------------------------------------
# MonologueWatch
# ---------------------------------------------------------------------------

class TestMonologueWatch:
    def test_fires_after_90_ticks(self):
        watch = proactive.MonologueWatch()
        card = None
        for _ in range(90):
            card = watch.update(0.5, 0.001)  # yo hablo, ellos callan
        assert card is not None
        assert card["tipo"] == "coaching"

    def test_fires_only_once_per_episode(self):
        watch = proactive.MonologueWatch()
        cards = [watch.update(0.5, 0.001) for _ in range(120)]
        fired = [c for c in cards if c is not None]
        assert len(fired) == 1

    def test_reset_on_sustained_yield(self):
        watch = proactive.MonologueWatch()
        for _ in range(90):
            watch.update(0.5, 0.001)
        # Ceder la palabra sostenidamente (>=5 ticks) reinicia el episodio.
        for _ in range(5):
            watch.update(0.001, 0.5)
        cards = [watch.update(0.5, 0.001) for _ in range(90)]
        fired = [c for c in cards if c is not None]
        assert len(fired) == 1  # dispara de nuevo tras el reset

    def test_brief_silence_does_not_reset(self):
        watch = proactive.MonologueWatch()
        for _ in range(50):
            watch.update(0.5, 0.001)
        for _ in range(3):
            watch.update(0.001, 0.001)  # silencio breve de ambos, no cede la palabra
        card = None
        for _ in range(40):
            card = watch.update(0.5, 0.001) or card
        assert card is not None  # el conteo total llegó a 90 igual

    def test_no_talk_never_fires(self):
        watch = proactive.MonologueWatch()
        cards = [watch.update(0.001, 0.001) for _ in range(200)]
        assert all(c is None for c in cards)
