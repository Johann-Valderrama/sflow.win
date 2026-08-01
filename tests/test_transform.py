"""Tests de Transform sobre selección (Ola 3 de PLAN-DICTADO).

Unidad 3a: captura de la selección de la app en foco por portapapeles.

Todo es HERMÉTICO: nunca se toca el portapapeles real ni se simula un Ctrl+C de
verdad. Las tres puertas al sistema (`_get_clipboard_text`, `_set_clipboard_text`,
`_send_ctrl_c`) y el número de secuencia se sustituyen con dobles, igual que
`tests/test_local_backend_device.py` hace con `WhisperModel`.

Lo que estos tests vigilan no es "la función devuelve un string": es la regla de
privacidad de la unidad 3z, que dice que **nunca se transforma el contenido
PREVIO del portapapeles**. Ese es el fallo silencioso caro de esta ola, y el que
la clase `TestNuncaCaeAlPortapapelesPrevio` existe para cazar.
"""
import pytest

from core import clipboard


class _FakeClipboard:
    """Portapapeles simulado con número de secuencia, como el de Windows."""

    def __init__(self, text=None, seq=100, seq_available=True):
        self.text = text
        self.seq = seq
        self.seq_available = seq_available
        self.sets = []          # historial de escrituras (para verificar la restauración)
        self.ctrl_c_sent = 0
        self._copies_on_ctrl_c = None   # qué "selecciona" la app al recibir Ctrl+C

    # --- dobles de las puertas al sistema ---
    def get_text(self):
        return self.text

    def set_text(self, text):
        self.sets.append(text)
        self.text = text
        self.seq += 1

    def sequence(self):
        return self.seq if self.seq_available else 0

    def send_ctrl_c(self):
        self.ctrl_c_sent += 1
        if self._copies_on_ctrl_c is not None:
            # La app en foco escribe la selección: cambia el contenido Y la secuencia.
            self.text = self._copies_on_ctrl_c
            self.seq += 1
        return True

    def selection_is(self, text):
        self._copies_on_ctrl_c = text


@pytest.fixture
def fake(monkeypatch):
    fc = _FakeClipboard()
    monkeypatch.setattr(clipboard, "_get_clipboard_text", fc.get_text)
    monkeypatch.setattr(clipboard, "_set_clipboard_text", fc.set_text)
    monkeypatch.setattr(clipboard, "_clipboard_sequence", fc.sequence)
    monkeypatch.setattr(clipboard, "_send_ctrl_c", fc.send_ctrl_c)
    monkeypatch.setattr(clipboard, "save_frontmost_app", lambda: None)
    return fc


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------
class TestCapturaNormal:
    def test_devuelve_la_seleccion(self, fake):
        fake.text = "portapapeles viejo"
        fake.selection_is("el texto que seleccioné")
        text, status = clipboard.capture_selection()
        assert status == "ok"
        assert text == "el texto que seleccioné"

    def test_guarda_la_ventana_destino_antes_de_copiar(self, monkeypatch):
        """save_frontmost_app() va ANTES del Ctrl+C: después el foco puede haber
        cambiado y el pegado de 3c/3d no tendría a dónde volver."""
        orden = []
        fc = _FakeClipboard()
        fc.selection_is("hola")
        monkeypatch.setattr(clipboard, "_get_clipboard_text", fc.get_text)
        monkeypatch.setattr(clipboard, "_set_clipboard_text", fc.set_text)
        monkeypatch.setattr(clipboard, "_clipboard_sequence", fc.sequence)
        monkeypatch.setattr(clipboard, "save_frontmost_app", lambda: orden.append("save"))

        def _ctrl_c():
            orden.append("ctrl_c")
            return fc.send_ctrl_c()

        monkeypatch.setattr(clipboard, "_send_ctrl_c", _ctrl_c)
        clipboard.capture_selection()
        assert orden == ["save", "ctrl_c"]

    def test_captura_aunque_la_seleccion_sea_igual_al_portapapeles(self, fake):
        """El caso que una comparación de textos no distingue: el usuario ya tenía
        copiado lo mismo que acaba de seleccionar. El número de secuencia sí lo ve."""
        fake.text = "mismo texto"
        fake.selection_is("mismo texto")
        text, status = clipboard.capture_selection()
        assert (text, status) == ("mismo texto", "ok")


# ---------------------------------------------------------------------------
# LA regla de 3z: nunca transformar lo que ya estaba en el portapapeles
# ---------------------------------------------------------------------------
class TestNuncaCaeAlPortapapelesPrevio:
    def test_sin_seleccion_devuelve_empty_y_no_el_contenido_previo(self, fake):
        fake.text = "SECRETO que el usuario copió hace media hora"
        fake.selection_is(None)          # el Ctrl+C no copia nada: no había selección
        text, status = clipboard.capture_selection(timeout=0.1)
        assert status == "empty"
        assert text is None

    def test_sin_numero_de_secuencia_tambien_aborta(self, fake):
        """Sin GetClipboardSequenceNumber solo queda comparar textos, y ahí la
        función se pone MÁS estricta, no más laxa: si el texto no cambió, aborta."""
        fake.seq_available = False
        fake.text = "SECRETO previo"
        fake.selection_is(None)
        text, status = clipboard.capture_selection(timeout=0.1)
        assert (text, status) == (None, "empty")

    def test_seleccion_en_blanco_no_pasa(self, fake):
        fake.text = "previo"
        fake.selection_is("   \n  ")
        text, status = clipboard.capture_selection(timeout=0.1)
        assert (text, status) == (None, "empty")


# ---------------------------------------------------------------------------
# Portapapeles: restauración y tope de tamaño
# ---------------------------------------------------------------------------
class TestPortapapeles:
    def test_restaura_el_contenido_previo_de_texto(self, fake):
        fake.text = "lo que el usuario tenía copiado"
        fake.selection_is("la selección")
        clipboard.capture_selection()
        assert fake.sets[-1] == "lo que el usuario tenía copiado"
        assert fake.text == "lo que el usuario tenía copiado"

    def test_sin_previo_textual_no_vacia_el_portapapeles(self, fake):
        """Enmienda de 3z: un portapapeles no textual (una imagen) no se puede
        restaurar con esta API. Se prefiere dejar la selección (igual que un Ctrl+C
        manual) antes que destruirle al usuario la imagen que había copiado."""
        fake.text = None
        fake.selection_is("la selección")
        text, status = clipboard.capture_selection()
        assert status == "ok"
        assert fake.sets == []          # no se escribió nada de vuelta
        assert fake.text == "la selección"

    def test_restaura_aunque_la_seleccion_no_sirva(self, fake):
        """La restauración va en un finally: una selección demasiado larga se
        rechaza, pero el portapapeles del usuario vuelve a su sitio igual."""
        fake.text = "previo"
        fake.selection_is("x" * (clipboard.CAPTURE_MAX_CHARS + 1))
        text, status = clipboard.capture_selection()
        assert (text, status) == (None, "too_long")
        assert fake.text == "previo"

    def test_tope_duro_de_tamano(self, fake):
        fake.selection_is("y" * (clipboard.CAPTURE_MAX_CHARS + 5))
        assert clipboard.capture_selection()[1] == "too_long"

    def test_justo_en_el_tope_pasa(self, fake):
        fake.selection_is("z" * clipboard.CAPTURE_MAX_CHARS)
        text, status = clipboard.capture_selection()
        assert status == "ok"
        assert len(text) == clipboard.CAPTURE_MAX_CHARS


class TestFallos:
    def test_ctrl_c_fallido_devuelve_failed(self, fake, monkeypatch):
        monkeypatch.setattr(clipboard, "_send_ctrl_c", lambda: False)
        assert clipboard.capture_selection() == (None, "failed")

    def test_ctrl_c_fallido_no_toca_el_portapapeles(self, fake, monkeypatch):
        fake.text = "previo"
        monkeypatch.setattr(clipboard, "_send_ctrl_c", lambda: False)
        clipboard.capture_selection()
        assert fake.sets == []
        assert fake.text == "previo"


# ---------------------------------------------------------------------------
# Guarda de privacidad: el contenido capturado NO entra a los logs (3z, decisión 4)
# ---------------------------------------------------------------------------
class TestNoLogueaContenido:
    def test_ni_el_texto_capturado_ni_el_previo_aparecen_en_los_logs(self, fake, caplog):
        import logging

        caplog.set_level(logging.DEBUG, logger="core.clipboard")
        fake.text = "PREVIO-CONFIDENCIAL-9271"
        fake.selection_is("SELECCION-CONFIDENCIAL-4416")
        clipboard.capture_selection()
        registrado = "\n".join(r.getMessage() for r in caplog.records)
        assert "SELECCION-CONFIDENCIAL-4416" not in registrado
        assert "PREVIO-CONFIDENCIAL-9271" not in registrado

    def test_el_rechazo_por_tamano_tampoco_loguea_el_texto(self, fake, caplog):
        import logging

        caplog.set_level(logging.DEBUG, logger="core.clipboard")
        fake.selection_is("SECRETO-LARGO-7788" * 20000)
        clipboard.capture_selection()
        registrado = "\n".join(r.getMessage() for r in caplog.records)
        assert "SECRETO-LARGO-7788" not in registrado
