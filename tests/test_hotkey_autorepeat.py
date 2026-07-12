"""Tests para la unidad 0.3 'Guards de auto-repeat en AltGr+R / AltGr+T' (bug F10).

Antes de esta unidad, AltGr+R (meeting_toggle) y AltGr+T (translate_pressed /
toggle interno _alt_gr_t_mode) no tenian guard de auto-repeat, a diferencia de
AltGr+H/A/M que ya usaban el patron `_h_held`/`_a_held`/`_m_held`. Mantener la
tecla presionada un instante de mas hace que Windows dispare on_press repetidos
a ~30 Hz:
  - R: cada repeat emitia meeting_toggle -> start/stop de reunion encadenados.
  - T: el toggle interno alternaba en cada repeat -> grabaciones de traduccion
    arrancadas y paradas a 30 Hz (el primer repeat entraba por la rama
    "detener" y el siguiente por "iniciar", porque `not self._recording` ya
    era True).

Este archivo cubre (a) y (b) del brief: R y T dejan de disparar en cada
repeat, pero el toggle legitimo (press-release-press) sigue alternando. La
regresion de H/A/M ya esta cubierta en tests/test_highlight.py (no se duplica
aqui); test_altgr_r_no_regression en ese archivo tambien se actualizo para
reflejar el nuevo comportamiento guardado.

Mismo estilo que test_highlight.py: eventos de teclado inyectados llamando
directamente a _on_press/_on_release con objetos de tecla sinteticos, sin
pynput.Listener real.
"""

from unittest.mock import MagicMock

import pytest


class _FakeKey:
    """Objeto minimo compatible con lo que HotkeyListener espera de una tecla."""

    def __init__(self, vk=None):
        self.vk = vk


def _vk_key(vk):
    return _FakeKey(vk=vk)


@pytest.fixture
def listener():
    from core.hotkey import HotkeyListener
    hl = HotkeyListener()
    hl.pressed = MagicMock()
    hl.released = MagicMock()
    hl.translate_pressed = MagicMock()
    hl.meeting_toggle = MagicMock()
    hl.highlight_pressed = MagicMock()
    hl.hud_toggle = MagicMock()
    hl.lost_pressed = MagicMock()
    return hl


def _press_alt_gr(hl):
    from pynput import keyboard
    hl._on_press(keyboard.Key.alt_gr)


class TestMeetingToggleAutorepeat:
    def test_altgr_r_held_emits_once_despite_autorepeat(self, listener):
        """(a) Mantener R (N eventos press sin release) emite meeting_toggle
        UNA sola vez mientras no haya release."""
        _press_alt_gr(listener)
        r_key = _vk_key(0x52)
        for _ in range(20):
            listener._on_press(r_key)
        assert listener.meeting_toggle.emit.call_count == 1

    def test_altgr_r_release_then_press_toggles_again(self, listener):
        """(a) release + press de nuevo -> segundo emit (toggle legitimo intacto)."""
        _press_alt_gr(listener)
        r_key = _vk_key(0x52)
        listener._on_press(r_key)
        listener._on_press(r_key)
        listener._on_press(r_key)
        assert listener.meeting_toggle.emit.call_count == 1

        listener._on_release(r_key)
        listener._on_press(r_key)
        assert listener.meeting_toggle.emit.call_count == 2

        # Un tercer ciclo release+press confirma que el patron es estable, no un fluke.
        listener._on_release(r_key)
        listener._on_press(r_key)
        assert listener.meeting_toggle.emit.call_count == 3

    def test_reset_clears_r_held_flag(self, listener):
        _press_alt_gr(listener)
        r_key = _vk_key(0x52)
        listener._on_press(r_key)
        assert listener._r_held is True
        listener.reset()
        assert listener._r_held is False
        listener._on_press(r_key)
        assert listener.meeting_toggle.emit.call_count == 2


class TestTranslateToggleAutorepeat:
    def test_altgr_t_held_emits_once_and_state_does_not_flap(self, listener):
        """(b) Mantener T emite translate_pressed UNA sola vez y el estado
        _alt_gr_t_mode/_recording NO alterna con los repeats."""
        _press_alt_gr(listener)
        t_key = _vk_key(0x54)
        for _ in range(20):
            listener._on_press(t_key)

        assert listener.translate_pressed.emit.call_count == 1
        assert listener.released.emit.call_count == 0
        assert listener._alt_gr_t_mode is True
        assert listener._recording is True

    def test_altgr_t_legit_sequence_starts_and_stops(self, listener):
        """(b) Secuencia legitima press-release-press: arranca y detiene como
        siempre (2 emisiones correctas: translate_pressed y luego released)."""
        _press_alt_gr(listener)
        t_key = _vk_key(0x54)

        listener._on_press(t_key)  # primera pulsacion -> inicia
        assert listener.translate_pressed.emit.call_count == 1
        assert listener._alt_gr_t_mode is True
        assert listener._recording is True

        listener._on_release(t_key)  # solo limpia _t_held; no detiene (toggle, no hold)
        assert listener._recording is True

        listener._on_press(t_key)  # segunda pulsacion -> detiene
        assert listener.released.emit.call_count == 1
        assert listener._alt_gr_t_mode is False
        assert listener._recording is False

        listener._on_release(t_key)
        listener._on_press(t_key)  # tercera pulsacion -> vuelve a iniciar
        assert listener.translate_pressed.emit.call_count == 2
        assert listener._recording is True

    def test_reset_clears_t_held_flag(self, listener):
        _press_alt_gr(listener)
        t_key = _vk_key(0x54)
        listener._on_press(t_key)
        assert listener._t_held is True
        listener.reset()
        assert listener._t_held is False
