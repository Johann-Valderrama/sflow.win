"""HUD proactivo (unidad 5.3) — ventana Qt nativa, sin QtWebEngine.

Superficie nueva para el Proactivo v2: tarjetas de push (pendientes/detecciones/
coaching), confirmación de highlight, "Me perdí" y una pregunta libre sobre la
reunión en curso. Se alimenta EN-PROCESO desde main.py (nunca HTTP/sockets):
main.py le empuja datos en el tick de 1s del ``_meeting_sync_timer`` existente
(corrección O9 del debate: lecturas del HUD hacia MEETING deben ser mínimas y
cortas; mejor que main.py empuje).

Flags de ventana FIJOS (corrección O10): jamás togglear WindowDoesNotAcceptFocus
ni ningún otro flag en caliente — mismo patrón verificado en ui/pill_widget.py
(los clicks llegan sin robar foco con esta combinación). Para escribir en el
mini-input de pregunta, el foco se activa DELIBERADAMENTE (patrón de
core/clipboard.py: guardar HWND frontal, SetForegroundWindow, restaurar al salir).
"""
import ctypes
import ctypes.wintypes
import logging

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QFrame, QApplication,
)

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
_user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
_user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
_user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
_user32.IsWindow.restype = ctypes.wintypes.BOOL

# Estilo por tipo de tarjeta: (color de título, etiqueta).
CARD_STYLES = {
    "pendiente": ("#d97706", "Pendiente"),          # ámbar — mismo acento que el dashboard
    "coaching": ("#22d3ee", "Coaching"),             # verde-azulado (cian)
    "deteccion": ("#a78bfa", "Detección"),           # violeta (default para tipos futuros)
    "cruzada": ("#a78bfa", "Memoria cruzada"),
}
_DEFAULT_STYLE = ("#a78bfa", "Nota")

CARD_TTL_MS = 180_000  # 180s de caducidad visual (contrato con proactive.py)
_MAX_CARDS = 3


class _Card(QFrame):
    """Una tarjeta individual dentro de la pila del HUD."""

    feedback = pyqtSignal(str, str, str, int)  # key, tipo, texto, value

    def __init__(self, card: dict, parent=None):
        super().__init__(parent)
        self.key = card.get("key", "")
        self.tipo = card.get("tipo", "")
        self.texto = card.get("texto", "")
        color, label = CARD_STYLES.get(self.tipo, _DEFAULT_STYLE)

        self.setStyleSheet(
            f"QFrame {{ background-color: rgba(255,255,255,18); "
            f"border: 1px solid {color}; border-radius: 8px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        title = QLabel(label)
        title.setStyleSheet(f"color: {color}; font-weight: 600; font-size: 11px; border: none;")
        layout.addWidget(title)

        body = QLabel(self.texto)
        body.setWordWrap(True)
        body.setStyleSheet("color: rgba(255,255,255,220); font-size: 12px; border: none;")
        layout.addWidget(body)

        detail = card.get("detail")
        if detail:
            detail_lbl = QLabel(str(detail))
            detail_lbl.setWordWrap(True)
            detail_lbl.setStyleSheet("color: rgba(255,255,255,140); font-size: 11px; border: none;")
            layout.addWidget(detail_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        ok_btn = QPushButton("✓")
        ok_btn.setFixedSize(24, 20)
        ok_btn.clicked.connect(lambda: self.feedback.emit(self.key, self.tipo, self.texto, 1))
        bad_btn = QPushButton("✗")
        bad_btn.setFixedSize(24, 20)
        bad_btn.clicked.connect(lambda: self.feedback.emit(self.key, self.tipo, self.texto, -1))
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(bad_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self._ttl_timer = QTimer(self)
        self._ttl_timer.setSingleShot(True)
        self._ttl_timer.timeout.connect(self._expire)
        self._ttl_timer.start(CARD_TTL_MS)

    def _expire(self):
        self.feedback.emit(self.key, self.tipo, self.texto, 0)  # 0 = caducó, no es ni +1 ni -1
        self.setParent(None)
        self.deleteLater()


class HudWidget(QWidget):
    """Ventana flotante nativa del HUD proactivo."""

    feedback_requested = pyqtSignal(str, str, str, int)  # key, tipo, texto, value
    lost_requested = pyqtSignal()                         # botón "Me perdí"
    ask_requested = pyqtSignal(str)                        # texto de la pregunta libre

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(280)
        self.setStyleSheet(
            "background-color: rgba(20,20,20,235); border-radius: 10px;"
        )

        self._drag_pos = None
        self._saved_hwnd = None  # HWND frontal guardado al activar el input (foco deliberado)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # (1) Pila de tarjetas (máx 3)
        self.cards_container = QVBoxLayout()
        self.cards_container.setSpacing(6)
        root.addLayout(self.cards_container)
        self._cards: list[_Card] = []

        # (2) Línea de confirmación de highlight (se desvanece a los 2s)
        self.highlight_label = QLabel("")
        self.highlight_label.setStyleSheet("color: #fbbf24; font-size: 11px; border: none;")
        self.highlight_label.setVisible(False)
        root.addWidget(self.highlight_label)
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(lambda: self.highlight_label.setVisible(False))

        # (3) Botón "Me perdí" + respuesta scrolleable
        self.lost_btn = QPushButton("Me perdí (AltGr+M)")
        self.lost_btn.clicked.connect(self._on_lost_clicked)
        root.addWidget(self.lost_btn)

        self.lost_spinner_label = QLabel("Resumiendo…")
        self.lost_spinner_label.setStyleSheet("color: rgba(255,255,255,160); font-size: 11px; border: none;")
        self.lost_spinner_label.setVisible(False)
        root.addWidget(self.lost_spinner_label)

        self.lost_scroll = QScrollArea()
        self.lost_scroll.setWidgetResizable(True)
        self.lost_scroll.setFixedHeight(90)
        self.lost_scroll.setVisible(False)
        self.lost_answer_label = QLabel("")
        self.lost_answer_label.setWordWrap(True)
        self.lost_answer_label.setStyleSheet("color: rgba(255,255,255,220); font-size: 12px; padding: 4px;")
        self.lost_scroll.setWidget(self.lost_answer_label)
        root.addWidget(self.lost_scroll)

        # (4) Mini-input de pregunta libre
        self.ask_input = QLineEdit()
        self.ask_input.setPlaceholderText("Pregunta sobre la reunión…")
        self.ask_input.returnPressed.connect(self._on_ask_submitted)
        root.addWidget(self.ask_input)

        # Foco deliberado: activar la ventana SOLO al clicar el input (no roba foco
        # el resto del tiempo, igual que la pill).
        self.ask_input.installEventFilter(self)

    # ------------------------------------------------------------------
    # API pública (llamada por main.py)
    # ------------------------------------------------------------------

    def add_card(self, card: dict):
        """Añade una tarjeta a la pila (máx 3 visibles; la más vieja se retira)."""
        c = _Card(card, parent=self)
        c.feedback.connect(self._on_card_feedback)
        self.cards_container.addWidget(c)
        self._cards.append(c)
        while len(self._cards) > _MAX_CARDS:
            old = self._cards.pop(0)
            old.setParent(None)
            old.deleteLater()

    def show_highlight_confirmation(self, time_label: str):
        """Muestra "⭐ mm:ss" y lo desvanece a los 2s (llamado desde main.py al marcar AltGr+H)."""
        self.highlight_label.setText(f"⭐ {time_label}")
        self.highlight_label.setVisible(True)
        self._highlight_timer.start(2000)

    def show_lost_spinner(self):
        self.lost_spinner_label.setVisible(True)
        self.lost_scroll.setVisible(False)

    def show_lost_answer(self, text: str):
        self.lost_spinner_label.setVisible(False)
        self.lost_answer_label.setText(text)
        self.lost_scroll.setVisible(True)

    def anchor_near(self, x: int, y: int):
        """Ancla el HUD cerca de un punto (posición de la pill al abrir)."""
        self.move(x, max(y - self.sizeHint().height() - 12, 0))

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _on_card_feedback(self, key: str, tipo: str, texto: str, value: int):
        if value != 0:  # 0 = caducidad silenciosa, no se reporta como feedback
            self.feedback_requested.emit(key, tipo, texto, value)
        if self._card_by_key(key) is None:
            return

    def _card_by_key(self, key: str):
        for c in self._cards:
            if c.key == key:
                return c
        return None

    def _on_lost_clicked(self):
        self.lost_requested.emit()

    def _on_ask_submitted(self):
        text = self.ask_input.text().strip()
        if not text:
            return
        self.ask_input.clear()
        self._restore_focus()
        self.ask_requested.emit(text)

    def _activate_for_input(self):
        """Foco deliberado: guarda el HWND frontal y activa esta ventana para escribir."""
        try:
            self._saved_hwnd = _user32.GetForegroundWindow()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hud: no se pudo guardar HWND frontal: %s", exc)
            self._saved_hwnd = None
        self.activateWindow()
        self.ask_input.setFocus()

    def _restore_focus(self):
        """Devuelve el foco a la ventana guardada (patrón core/clipboard.py)."""
        hwnd = self._saved_hwnd
        self._saved_hwnd = None
        if hwnd and _user32.IsWindow(hwnd):
            try:
                _user32.SetForegroundWindow(hwnd)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hud: no se pudo restaurar el foco: %s", exc)

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self.ask_input:
            if event.type() == QEvent.Type.MouseButtonPress:
                self._activate_for_input()
            elif event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
                self.ask_input.clear()
                self._restore_focus()
                return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Arrastre (mismo patrón que la pill)
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            screen = QApplication.screenAt(new_pos) or QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                x = max(geo.left(), min(new_pos.x(), geo.right() - self.width()))
                y = max(geo.top(), min(new_pos.y(), geo.bottom() - self.height()))
                new_pos.setX(x)
                new_pos.setY(y)
            self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
