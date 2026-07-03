"""Tests para la unidad 1.3 'Highlight AltGr+H' (plan de olas de Vflow).

Cubre:
  - HotkeyListener: AltGr+H emite highlight_pressed una sola vez por hold (supresión
    de auto-repeat), un release+press nuevo vuelve a emitir, y no hay regresión en
    AltGr+T / AltGr+R / modo 1 (Ctrl+Alt) / H suelto sin AltGr.
  - MeetingSession.add_highlight(): inactiva -> None; activa -> dict con t/time y
    acumulación en self._highlights.
  - Round-trip de highlights_json en TranscriptionDB.meeting_insert/meeting_get
    (DB temporal, nunca la del usuario).

No arranca audio real, no llama a ningún LLM, no usa pynput.Listener real: los
eventos de teclado se inyectan llamando directamente a _on_press/_on_release con
objetos de tecla sintéticos (mismo patrón que usaría un script de pynput real).
"""

import json
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Teclas sintéticas (evitan depender de un backend real de pynput/X11/Win32)
# ---------------------------------------------------------------------------

class _FakeKey:
    """Objeto mínimo compatible con lo que HotkeyListener espera de una tecla.

    Las teclas especiales (ctrl/alt/alt_gr/shift) se representan con las
    constantes reales de pynput.keyboard.Key (comparadas por identidad `in`).
    Las teclas de letra (T/R/H) se representan con un vk propio, como hace
    pynput en Windows.
    """

    def __init__(self, vk=None):
        self.vk = vk


def _vk_key(vk):
    return _FakeKey(vk=vk)


@pytest.fixture
def listener():
    from core.hotkey import HotkeyListener
    hl = HotkeyListener()
    # No arrancamos el listener real de pynput (sin hilo de teclado real);
    # solo ejercitamos _on_press/_on_release directamente.
    hl.pressed = MagicMock()
    hl.released = MagicMock()
    hl.translate_pressed = MagicMock()
    hl.meeting_toggle = MagicMock()
    hl.highlight_pressed = MagicMock()
    return hl


def _press_alt_gr(hl):
    from pynput import keyboard
    hl._on_press(keyboard.Key.alt_gr)


def _release_alt_gr(hl):
    from pynput import keyboard
    hl._on_release(keyboard.Key.alt_gr)


class TestHighlightHotkey:
    def test_altgr_h_emits_once_despite_autorepeat(self, listener):
        """20 on_press repetidos de H (auto-repeat de Windows) con AltGr sostenido
        deben emitir highlight_pressed UNA sola vez mientras no haya release."""
        _press_alt_gr(listener)
        h_key = _vk_key(0x48)
        for _ in range(20):
            listener._on_press(h_key)
        assert listener.highlight_pressed.emit.call_count == 1

    def test_release_then_press_emits_again(self, listener):
        _press_alt_gr(listener)
        h_key = _vk_key(0x48)
        listener._on_press(h_key)
        listener._on_press(h_key)
        assert listener.highlight_pressed.emit.call_count == 1

        listener._on_release(h_key)
        listener._on_press(h_key)
        assert listener.highlight_pressed.emit.call_count == 2

    def test_altgr_t_no_regression(self, listener):
        """AltGr+T sigue funcionando como toggle (inicia y detiene)."""
        _press_alt_gr(listener)
        t_key = _vk_key(0x54)
        listener._on_press(t_key)
        assert listener.translate_pressed.emit.call_count == 1
        assert listener._recording is True

        listener._on_press(t_key)  # segunda pulsación: detiene
        assert listener.released.emit.call_count == 1
        assert listener._recording is False

    def test_altgr_r_no_regression(self, listener):
        """AltGr+R sigue disparando meeting_toggle sin auto-repeat suppression propia
        (es un toggle idempotente manejado por el slot, no por el listener)."""
        _press_alt_gr(listener)
        r_key = _vk_key(0x52)
        listener._on_press(r_key)
        listener._on_press(r_key)
        assert listener.meeting_toggle.emit.call_count == 2  # sin supresión, como antes

    def test_ctrl_alt_arming_plus_h_no_spurious_trigger(self, listener):
        """Ctrl+Alt armado (modo 1) + H no debe disparar ni dictado ni highlight
        de forma espuria. H sin AltGr no coincide con el bloque de highlight;
        al no ser un modificador reconocido, cancela el armado de Ctrl+Alt
        (comportamiento existente y correcto, no se toca)."""
        from pynput import keyboard
        import core.hotkey as hotkey_module
        # hotkey.py hace `from config import ARMING_DELAY` (copia de import-time);
        # parchear config.ARMING_DELAY no lo afecta — hay que parchear el nombre
        # local en core.hotkey. Se restaura en el finally para no filtrar estado
        # a otros tests (test_sflow.py también usa ARMING_DELAY).
        original_arming_delay = hotkey_module.ARMING_DELAY
        hotkey_module.ARMING_DELAY = 0  # disparo inmediato, sin depender de un timer real
        try:
            listener._on_press(keyboard.Key.ctrl_l)
            listener._on_press(keyboard.Key.alt_l)
            # Sin AltGr sostenido: el bloque de highlight no aplica (is_h and alt_gr_held es False)
            h_key = _vk_key(0x48)
            listener._on_press(h_key)

            assert listener.highlight_pressed.emit.call_count == 0
            # El modo 1 ya se disparó por Ctrl+Alt antes de que H fuera evaluado (ARMING_DELAY=0);
            # lo relevante para esta unidad es que H nunca dispara highlight sin AltGr.
            assert listener.pressed.emit.call_count <= 1
        finally:
            hotkey_module.ARMING_DELAY = original_arming_delay

    def test_h_without_altgr_does_nothing(self, listener):
        """H suelto (sin AltGr) no dispara highlight ni ningún otro modo."""
        h_key = _vk_key(0x48)
        listener._on_press(h_key)
        listener._on_release(h_key)
        assert listener.highlight_pressed.emit.call_count == 0
        assert listener.pressed.emit.call_count == 0
        assert listener.meeting_toggle.emit.call_count == 0

    def test_reset_clears_h_held_flag(self, listener):
        """reset() (safety timer externo) limpia _h_held para no dejar el flag
        pegado tras un auto-stop externo."""
        _press_alt_gr(listener)
        h_key = _vk_key(0x48)
        listener._on_press(h_key)
        assert listener._h_held is True
        listener.reset()
        assert listener._h_held is False
        # Tras el reset, una nueva pulsación debe volver a emitir
        listener._on_press(h_key)
        assert listener.highlight_pressed.emit.call_count == 2


# ---------------------------------------------------------------------------
# MeetingSession.add_highlight()
# ---------------------------------------------------------------------------

class TestAddHighlight:
    def test_inactive_meeting_returns_none(self):
        from core.meeting import MeetingSession
        m = MeetingSession()
        assert m.is_active() is False
        assert m.add_highlight() is None
        assert m._highlights == []

    def test_active_meeting_accumulates_highlights(self):
        from core.meeting import MeetingSession
        import time as _time
        m = MeetingSession()
        # Simular el estado mínimo de una reunión activa sin arrancar audio real.
        m._active = True
        m._t0 = _time.monotonic() - 5.0  # ~5s transcurridos

        first = m.add_highlight()
        assert first is not None
        assert "t" in first and "time" in first
        assert first["time"].count(":") == 1

        second = m.add_highlight()
        assert second is not None
        assert len(m._highlights) == 2
        assert m._highlights[0] == first
        assert m._highlights[1] == second


# ---------------------------------------------------------------------------
# Round-trip DB: highlights_json
# ---------------------------------------------------------------------------

class TestHighlightsDbRoundTrip:
    @pytest.fixture(autouse=True)
    def db(self, tmp_path):
        from db.database import TranscriptionDB
        self.db_path = str(tmp_path / "test_meetings.db")
        self.db = TranscriptionDB(db_path=self.db_path)

    def test_meeting_insert_with_highlights_json(self):
        highlights = [{"t": 12.3, "time": "00:12"}, {"t": 65.0, "time": "01:05"}]
        meeting_id = self.db.meeting_insert(
            title="Reunión de prueba",
            transcript="[00:12 Yo] hola\n[01:05 Ellos] ok",
            segments_json=json.dumps([]),
            duration_seconds=90.0,
            started_at="2026-07-03 10:00:00",
            highlights_json=json.dumps(highlights, ensure_ascii=False),
        )
        assert meeting_id >= 1

        row = self.db.meeting_get(meeting_id)
        assert row is not None
        assert row["highlights_json"] is not None
        round_tripped = json.loads(row["highlights_json"])
        assert round_tripped == highlights

    def test_meeting_insert_without_highlights_defaults_none(self):
        meeting_id = self.db.meeting_insert(
            title="Sin highlights",
            transcript="texto",
            segments_json=json.dumps([]),
            duration_seconds=10.0,
        )
        row = self.db.meeting_get(meeting_id)
        assert row["highlights_json"] is None
