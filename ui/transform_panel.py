"""Panel de previsualización de Transform (G1-A). Ventana propia, CON foco.

**Por qué es una ventana aparte y no un modo del HUD, contra lo que decía el
hallazgo E6 del plan.** E6 proponía reusar `ui/hud_widget.py` argumentando que ya
"acepta con Enter y descarta con Esc". Se implementó así, y el PRIMER USO REAL lo
tumbó: Johann seleccionó texto, pulsó el atajo y al teclear el número del prompt
**se le borró el texto**. Medido después (banco en el scratchpad de la sesión): el
HUD tiene `WindowDoesNotAcceptFocus` de forma permanente y su cabecera prohíbe
togglear ese flag en caliente, así que `activateWindow()` no lo enfoca y **las
teclas siguen llegando a la aplicación del usuario**, que todavía tiene el texto
SELECCIONADO. Teclear "7" ahí lo reemplaza.

O sea que el Enter/Esc del HUD solo funciona dentro de su propio input cuando el
usuario lo clica con el mouse. Un panel que tiene que leer el teclado NECESITA
foco, y el HUD está diseñado para no tenerlo nunca: los dos requisitos son
incompatibles y no hay reutilización posible sin romper el HUD.

Se descartó la alternativa de capturar 1-8/Enter/Esc globalmente y suprimirlas con
pynput: evita robar el foco, pero su modo de fallo (tragarse teclas en todo el
sistema si el estado se queda pegado) es invisible y grave, mientras que el de
esta ventana (el foco se mueve a Vflow, visible) es benigno. Misma lógica que el
resto de la ola: se prefiere el fallo que se ve.

Tomar el foco aquí no rompe nada del flujo: la selección ya está capturada en
memoria antes de abrir el panel, y el pegado vuelve a la ventana destino por su
HWND guardado (`paste_text(hwnd=...)`), no por el foco.
"""
import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
    QApplication,
)

logger = logging.getLogger(__name__)

_WIDTH = 460
_HEIGHT = 340


class TransformPanel(QWidget):
    """Previsualización de G1-A: elegir prompt, ver el resultado, aplicar o descartar."""

    accepted = pyqtSignal(str)              # lleva el texto: el panel es su único dueño
    discarded = pyqtSignal()
    copy_original_requested = pyqtSignal(str)
    prompt_chosen = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        # SIN WindowDoesNotAcceptFocus, que es la diferencia entera con el HUD.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(_WIDTH, _HEIGHT)

        self._picker = []
        self._original = None
        self._result = None
        self._drag_pos = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        self.title = QLabel("Transform")
        self.title.setStyleSheet(
            "color: rgba(255,255,255,0.75); font-size: 13px; font-weight: 600; "
            "border: none; background: transparent;"
        )
        root.addWidget(self.title)

        self.hint = QLabel("")
        self.hint.setStyleSheet(
            "color: rgba(255,255,255,0.45); font-size: 11.5px; border: none; background: transparent;"
        )
        root.addWidget(self.hint)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # el teclado lo lee la ventana
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,70); border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.body.setStyleSheet(
            "color: rgba(255,255,255,235); font-size: 12.5px; padding: 4px; background: transparent;"
        )
        self.scroll.setWidget(self.body)
        root.addWidget(self.scroll, 1)

        acciones = QHBoxLayout()
        acciones.setSpacing(6)
        self.apply_btn = QPushButton("Aplicar · Enter")
        self.apply_btn.setFixedHeight(30)
        self.apply_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_btn.setStyleSheet(
            "QPushButton { background-color: rgba(16,185,129,0.18); color: #a7f3d0; "
            "border: 1px solid rgba(16,185,129,0.45); border-radius: 8px; "
            "font-size: 12px; font-weight: 600; padding: 0px; }"
            "QPushButton:hover { background-color: rgba(16,185,129,0.3); }"
            "QPushButton:disabled { color: rgba(255,255,255,0.25); border-color: rgba(255,255,255,0.12); "
            "background-color: transparent; }"
        )
        self.apply_btn.clicked.connect(self._on_apply)
        acciones.addWidget(self.apply_btn, 1)

        self.discard_btn = QPushButton("Descartar · Esc")
        self.discard_btn.setFixedHeight(30)
        self.discard_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.discard_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.discard_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(255,255,255,0.55); "
            "border: 1px solid rgba(255,255,255,0.18); border-radius: 8px; "
            "font-size: 12px; font-weight: 600; padding: 0px; }"
            "QPushButton:hover { color: rgba(255,255,255,0.9); }"
        )
        self.discard_btn.clicked.connect(self._on_discard)
        acciones.addWidget(self.discard_btn, 1)
        root.addLayout(acciones)

        self.copy_btn = QPushButton("Copiar el texto original")
        self.copy_btn.setFixedHeight(24)
        self.copy_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(255,255,255,0.4); "
            "border: none; font-size: 11px; padding: 0px; }"
            "QPushButton:hover { color: rgba(255,255,255,0.75); }"
        )
        self.copy_btn.clicked.connect(self._on_copy_original)
        root.addWidget(self.copy_btn)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(
            "color: #fbbf24; font-size: 11.5px; border: none; background: transparent;"
        )
        self.status.setVisible(False)
        root.addWidget(self.status)

    # ------------------------------------------------------------------
    # API pública (la llama main.py)
    # ------------------------------------------------------------------

    def open_picker(self, original: str, opciones: list):
        """Paso 1: elegir cuál de los prompts aplicar, con su número."""
        self._picker = list(opciones or [])[:9]
        self._original = original
        self._result = None
        self.title.setText("Transform · elige qué hacer")
        self.hint.setText("Pulsa el número · Esc cancela")
        self.body.setText("<br>".join(
            f"<b>{i + 1}</b> · {o['label']} "
            f"<span style='color:rgba(255,255,255,0.45)'>{o['cuando']}</span>"
            for i, o in enumerate(self._picker)
        ))
        self.status.setVisible(False)
        self.apply_btn.setEnabled(False)
        self.copy_btn.setVisible(False)
        self._present()

    def open_waiting(self, prompt_label: str, original: "str | None" = None):
        """Paso 2: el modelo está trabajando.

        ``original`` solo hace falta cuando se entra por la bandeja (que salta el
        selector): sin él, "copiar el texto original" no tendría qué copiar.
        """
        self._picker = []
        self._result = None
        if original is not None:
            self._original = original
        self.title.setText(f"Transform · {prompt_label}")
        self.hint.setText("Transformando…")
        self.body.setText("")
        self.status.setVisible(False)
        self.apply_btn.setEnabled(False)
        self.copy_btn.setVisible(True)
        self._present()

    def show_result(self, text: str):
        """Paso 3: el resultado, que NO se aplica hasta que el usuario diga."""
        if not self.isVisible():
            return
        self._result = text
        self.hint.setText("Revisa antes de aplicar")
        self.body.setText(text)
        self.apply_btn.setEnabled(True)
        self._present()

    def show_error(self, message: str):
        if not self.isVisible():
            return
        self._result = None
        self.hint.setText("No se pudo transformar")
        self.status.setText(message)
        self.status.setVisible(True)
        self.apply_btn.setEnabled(False)

    def close_panel(self):
        """Cierra y BORRA de memoria el original y el resultado (unidad 3z)."""
        self._picker = []
        self._original = None
        self._result = None
        self.body.setText("")
        self.status.setVisible(False)
        self.hide()

    def is_open(self) -> bool:
        return self.isVisible()

    def is_picker(self) -> bool:
        return bool(self._picker)

    # ------------------------------------------------------------------

    def _present(self):
        if not self.isVisible():
            screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                self.move(geo.center().x() - _WIDTH // 2, geo.center().y() - _HEIGHT // 2)
            self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_apply(self):
        text = self._result
        if not text:
            return
        self.close_panel()
        self.accepted.emit(text)

    def _on_discard(self):
        self.close_panel()
        self.discarded.emit()

    def _on_copy_original(self):
        if not self._original:
            return
        self.copy_original_requested.emit(self._original)
        self.status.setText("Texto original copiado al portapapeles.")
        self.status.setVisible(True)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._on_discard()
            event.accept()
            return
        if self._picker:
            # Enter NO elige nada aquí a propósito: no hay resultado que aplicar y
            # un Enter perdido no debe volverse una elección que el usuario no hizo.
            indice = key - Qt.Key.Key_1
            if 0 <= indice < len(self._picker):
                elegido = self._picker[indice]["key"]
                self._picker = []
                self.prompt_chosen.emit(elegido)
                event.accept()
                return
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_apply()
            event.accept()
            return
        super().keyPressEvent(event)

    # Arrastre desde cualquier parte que no sea un botón.
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(self.width()), float(self.height()), 14.0, 14.0)
        painter.fillPath(path, QColor(17, 18, 22, 244))
        painter.setPen(QColor(255, 255, 255, 28))
        painter.drawPath(path)
