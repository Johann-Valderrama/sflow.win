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

from PyQt6.QtCore import Qt, QTimer, QPoint, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QFrame, QApplication,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 API type annotations (critical for 64-bit Windows: sin argtypes,
# ctypes asume c_int/32-bit para el HWND y lo trunca). Mismo patrón que
# ui/pill_widget.py y core/clipboard.py.
# ---------------------------------------------------------------------------
_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
_user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
_user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
_user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
_user32.IsWindow.restype = ctypes.wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    ctypes.wintypes.HWND,   # hWnd
    ctypes.wintypes.HWND,   # hWndInsertAfter
    ctypes.c_int,            # X
    ctypes.c_int,            # Y
    ctypes.c_int,            # cx
    ctypes.c_int,            # cy
    ctypes.wintypes.UINT,    # uFlags
]
_user32.SetWindowPos.restype = ctypes.wintypes.BOOL

_HWND_TOPMOST = ctypes.wintypes.HWND(-1)

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
_HUD_WIDTH = 320


def _feedback_btn_css(rgb: str) -> str:
    """CSS de un botón de feedback (✓/✗) con el color rgb "r,g,b" dado."""
    return (
        f"QPushButton {{ background-color: rgba({rgb},0.12); color: rgba({rgb},0.95); "
        f"border: 1px solid rgba({rgb},0.35); border-radius: 6px; font-size: 11.5px; font-weight: 600; }}"
        f"QPushButton:hover {{ background-color: rgba({rgb},0.22); }}"
        f"QPushButton:pressed {{ background-color: rgba({rgb},0.32); }}"
    )


class _Card(QFrame):
    """Una tarjeta individual dentro de la pila del HUD."""

    feedback = pyqtSignal(str, str, str, int)  # key, tipo, texto, value

    def __init__(self, card: dict, parent=None):
        super().__init__(parent)
        self.key = card.get("key", "")
        self.tipo = card.get("tipo", "")
        self.texto = card.get("texto", "")
        color, label = CARD_STYLES.get(self.tipo, _DEFAULT_STYLE)

        r, g, b, _ = QColor(color).getRgb()
        self.setObjectName("HudCard")
        self.setStyleSheet(
            f"QFrame#HudCard {{ background-color: rgba(255,255,255,22); "
            f"border: 1px solid rgba({r},{g},{b},110); border-radius: 10px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)

        title = QLabel(label.upper())
        title.setStyleSheet(
            f"color: {color}; font-weight: 700; font-size: 10.5px; "
            f"letter-spacing: 0.5px; border: none; background: transparent;"
        )
        layout.addWidget(title)

        body = QLabel(self.texto)
        body.setWordWrap(True)
        body.setStyleSheet(
            "color: rgba(255,255,255,235); font-size: 13px; border: none; background: transparent;"
        )
        layout.addWidget(body)

        detail = card.get("detail")
        if detail:
            detail_lbl = QLabel(str(detail))
            detail_lbl.setWordWrap(True)
            detail_lbl.setStyleSheet(
                "color: rgba(255,255,255,130); font-size: 11px; border: none; background: transparent;"
            )
            layout.addWidget(detail_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        ok_btn = QPushButton("✓ Útil")
        ok_btn.setFixedHeight(24)
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setStyleSheet(_feedback_btn_css("74,222,128"))
        ok_btn.clicked.connect(lambda: self.feedback.emit(self.key, self.tipo, self.texto, 1))
        bad_btn = QPushButton("✗ Ruido")
        bad_btn.setFixedHeight(24)
        bad_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        bad_btn.setStyleSheet(_feedback_btn_css("248,113,113"))
        bad_btn.clicked.connect(lambda: self.feedback.emit(self.key, self.tipo, self.texto, -1))
        btn_row.addWidget(ok_btn, 1)
        btn_row.addWidget(bad_btn, 1)
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
        self.setFixedWidth(_HUD_WIDTH)
        # El fondo/borde redondeado se pinta a mano en paintEvent (mismo patrón que
        # ui/pill_widget.py): un QWidget plano con WA_TranslucentBackground NO
        # garantiza que su stylesheet background-color/border-radius se pinte en
        # Windows — es la causa de que el HUD se viera "casi invisible".

        self._drag_pos = None
        self._saved_hwnd = None  # HWND frontal guardado al activar el input (foco deliberado)
        self._anchor_point = None  # (x, y) de referencia para re-anclar tras el primer show()

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # (1) Pila de tarjetas (máx 3)
        self.cards_container = QVBoxLayout()
        self.cards_container.setSpacing(6)
        root.addLayout(self.cards_container)
        self._cards: list[_Card] = []

        # (2) Línea de confirmación de highlight (se desvanece a los 2s)
        self.highlight_label = QLabel("")
        self.highlight_label.setStyleSheet(
            "color: #fbbf24; font-size: 12px; font-weight: 600; border: none; background: transparent;"
        )
        self.highlight_label.setVisible(False)
        root.addWidget(self.highlight_label)
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(lambda: self.highlight_label.setVisible(False))

        # (3) Botón "Me perdí" + respuesta scrolleable
        self.lost_btn = QPushButton("🧭  Me perdí  ·  AltGr+M")
        self.lost_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lost_btn.setFixedHeight(30)
        self.lost_btn.setStyleSheet(
            "QPushButton { background-color: rgba(139,92,246,0.16); color: #ddd6fe; "
            "border: 1px solid rgba(139,92,246,0.45); border-radius: 8px; "
            "font-size: 12px; font-weight: 600; text-align: center; }"
            "QPushButton:hover { background-color: rgba(139,92,246,0.28); }"
            "QPushButton:pressed { background-color: rgba(139,92,246,0.4); }"
        )
        self.lost_btn.clicked.connect(self._on_lost_clicked)
        root.addWidget(self.lost_btn)

        self.lost_spinner_label = QLabel("Resumiendo…")
        self.lost_spinner_label.setStyleSheet(
            "color: rgba(255,255,255,160); font-size: 11.5px; border: none; background: transparent;"
        )
        self.lost_spinner_label.setVisible(False)
        root.addWidget(self.lost_spinner_label)

        self.lost_scroll = QScrollArea()
        self.lost_scroll.setWidgetResizable(True)
        self.lost_scroll.setFixedHeight(110)
        self.lost_scroll.setVisible(False)
        self.lost_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.lost_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,70); border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self.lost_answer_label = QLabel("")
        self.lost_answer_label.setWordWrap(True)
        self.lost_answer_label.setStyleSheet(
            "color: rgba(255,255,255,225); font-size: 12.5px; padding: 4px; background: transparent;"
        )
        self.lost_scroll.setWidget(self.lost_answer_label)
        root.addWidget(self.lost_scroll)

        # (4) Mini-input de pregunta libre
        self.ask_input = QLineEdit()
        self.ask_input.setPlaceholderText("Pregunta sobre la reunión…")
        self.ask_input.setFixedHeight(30)
        self.ask_input.setStyleSheet(
            "QLineEdit { background-color: rgba(255,255,255,18); color: rgba(255,255,255,230); "
            "border: 1px solid rgba(255,255,255,40); border-radius: 8px; padding: 4px 10px; font-size: 12.5px; }"
            "QLineEdit:focus { border: 1px solid rgba(139,92,246,160); }"
        )
        self.ask_input.returnPressed.connect(self._on_ask_submitted)
        root.addWidget(self.ask_input)

        # Foco deliberado: activar la ventana SOLO al clicar el input (no roba foco
        # el resto del tiempo, igual que la pill).
        self.ask_input.installEventFilter(self)

        # Reafirmación de "always on top" (mismo patrón Win32 que ui/pill_widget.py):
        # la pill reasserta su HWND_TOPMOST cada 1s, y sin esto el HUD termina
        # detrás de ella. Intervalo más corto (500ms) para que, si la pill gana
        # brevemente, el HUD recupere el frente casi de inmediato.
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(500)
        self._topmost_timer.timeout.connect(self.force_topmost)

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
        """Ancla el HUD cerca de un punto (posición de la pill al abrir), arriba de él.

        Se posiciona dos veces: una estimación inmediata con ``sizeHint()`` (antes
        de que Qt calcule el layout real) y una segunda pasada tras el primer
        ``show()`` con la altura REAL ya renderizada — sin esto, una estimación
        corta hace que el HUD termine con su borde inferior solapando la pill.
        """
        self._anchor_point = (x, y)
        self._apply_anchor(self.sizeHint().height())
        QTimer.singleShot(0, lambda: self._apply_anchor(self.height()))

    def _apply_anchor(self, height: int):
        if self._anchor_point is None:
            return
        x, y = self._anchor_point
        gap = 16
        target = QPoint(x, max(y - height - gap, 0))
        screen = QApplication.screenAt(target) or QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            tx = max(geo.left(), min(target.x(), geo.right() - self.width()))
            ty = max(geo.top(), min(target.y(), geo.bottom() - height))
            target = QPoint(tx, ty)
        self.move(target)

    def force_topmost(self):
        """Reasserta HWND_TOPMOST vía Win32 (mismo patrón que ui/pill_widget.py).

        La pill reafirma su propio HWND_TOPMOST cada 1s; sin esta misma técnica
        aquí, esa reafirmación gana la banda "topmost" de Windows y el HUD
        termina detrás de la pill (bug reportado: "queda detrás del pill").
        """
        try:
            wid = self.winId()
            if not wid:
                return
            hwnd = ctypes.wintypes.HWND(int(wid))
            if not _user32.IsWindow(hwnd):
                return
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            _user32.SetWindowPos(
                hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hud: force_topmost falló: %s", exc)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.force_topmost)
        self._topmost_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._topmost_timer.stop()

    def paintEvent(self, event):
        """Pinta el fondo redondeado semi-translúcido a mano (ver nota en __init__:
        un QWidget plano con solo WA_TranslucentBackground no garantiza que su
        stylesheet background-color/border-radius se pinte en Windows)."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        painter.fillPath(path, QColor(16, 16, 20, 238))
        painter.setPen(QPen(QColor(139, 92, 246, 100), 1.2))
        painter.drawPath(path)
        painter.end()

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
