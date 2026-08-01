"""HUD proactivo (unidad 5.4) — panel lateral Qt nativo, sin QtWebEngine.

Rediseño 5.4: de popup pequeño anclado a la pill, a PANEL LATERAL de dos zonas
(mockup aprobado por Johann, fidelidad literal de colores/tamaños):

  HEADER  — punto ámbar + "Reunión · copiloto" + timer + botón "restablecer".
  AHORA   — zona transitoria: pila de tarjetas de push (máx 3), placeholder si
            no hay ninguna.
  REGISTRO — memoria de solo-lectura de la reunión completa (MEETING.get_registro()):
            scroll con una fila por evento (highlight/note/deteccion/cruzada).
  BOTTOM  — botón "Me perdí" + input de pregunta libre (igual que antes).

Nada se borra automáticamente: "Ahora" es la única zona transitoria (las
tarjetas caducan o reciben feedback y desaparecen de ahí), pero el Registro
sigue existiendo porque se reconstruye siempre desde MEETING (fuente de
verdad), nunca se edita localmente.

Se alimenta EN-PROCESO desde main.py (nunca HTTP/sockets): main.py empuja
datos en el tick de 1s del ``_meeting_sync_timer`` existente (corrección O9
del debate: lecturas del HUD hacia MEETING deben ser mínimas y cortas; mejor
que main.py empuje).

Flags de ventana FIJOS (corrección O10): jamás togglear WindowDoesNotAcceptFocus
ni ningún otro flag en caliente — mismo patrón verificado en ui/pill_widget.py
(los clicks llegan sin robar foco con esta combinación). Para escribir en el
mini-input de pregunta, el foco se activa DELIBERADAMENTE (patrón de
core/clipboard.py: guardar HWND frontal, SetForegroundWindow, restaurar al salir).

Redimensionable/reposicionable (requisito 5.4): QSizeGrip en la esquina
inferior derecha, arrastre restringido a la banda del header (para no chocar
con clicks en tarjetas/scroll del registro), y geometría persistida en memoria
del proceso (no hay "re-anclaje" tras el primer show: si el usuario movió o
redimensionó el panel, se respeta hasta que pulse "restablecer").
"""
import ctypes
import ctypes.wintypes
import logging

from PyQt6.QtCore import Qt, QTimer, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QFontMetrics, QTextOption
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QScrollArea, QFrame, QApplication, QSizeGrip,
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

# Estilo por tipo de tarjeta ("Ahora"): (color de título, etiqueta, emoji del chip).
CARD_STYLES = {
    "pendiente": ("#d97706", "Pendiente", "📋"),        # ámbar
    "coaching": ("#22d3ee", "Coaching", "🧭"),           # cian
    "deteccion": ("#a78bfa", "Detección", "❓"),          # violeta
    "cruzada": ("#a78bfa", "Memoria cruzada", "🕘"),      # violeta
}
_DEFAULT_CARD_STYLE = ("#a78bfa", "Nota", "📝")

# Estilo por tipo de fila del Registro: (color del chip, fondo alpha, emoji default).
REGISTRO_STYLES = {
    "highlight": (QColor(251, 191, 36), "rgba(251,191,36,0.15)", "⭐"),
    "note": (QColor(96, 165, 250), "rgba(96,165,250,0.15)", "📝"),
    "deteccion": (QColor(167, 139, 250), "rgba(139,92,246,0.16)", "❓"),
    "cruzada": (QColor(167, 139, 250), "rgba(139,92,246,0.16)", "🕘"),
}
_DEFAULT_REGISTRO_STYLE = (QColor(167, 139, 250), "rgba(139,92,246,0.16)", "📝")

# Emojis que el propio texto de detección ya trae al frente (evita duplicar
# el emoji si coincide con el que pondríamos en el chip).
_LEADING_EMOJIS = ("❓", "🤝", "⚠️")

CARD_TTL_MS = 180_000  # 180s de caducidad visual (contrato con proactive.py)
_MAX_CARDS = 3

_DEFAULT_WIDTH = 340
_MIN_WIDTH = 280
_MIN_HEIGHT = 320
_RIGHT_MARGIN = 18  # separación del borde derecho de pantalla al anclar
_TOP_MARGIN = 40


def _feedback_btn_css(rgb: str) -> str:
    """CSS de un botón de feedback (Útil/Ruido) con el color rgb "r,g,b" dado."""
    return (
        f"QPushButton {{ background-color: rgba({rgb},0.12); color: rgba({rgb},0.95); "
        f"border: 1px solid rgba({rgb},0.35); border-radius: 6px; font-size: 12px; "
        f"font-weight: 600; padding: 0px; }}"
        f"QPushButton:hover {{ background-color: rgba({rgb},0.22); }}"
        f"QPushButton:pressed {{ background-color: rgba({rgb},0.32); }}"
    )


_LEVEL_INTERVAL_MS = 80  # cadencia del VU del indicador de captura (~12.5 Hz)


class _SectionLabel(QLabel):
    """Etiqueta de sección tipo "AHORA" / "REGISTRO DE LA REUNIÓN"."""

    def __init__(self, text: str, parent=None):
        super().__init__(text.upper(), parent)
        self.setStyleSheet(
            "color: rgba(255,255,255,0.35); font-size: 10.5px; font-weight: 600; "
            "letter-spacing: 0.7px; border: none; background: transparent;"
        )


class _ListeningStrip(QWidget):
    """Indicador de captura en vivo: estado "Escuchando / Sin señal" + dos barras
    VU por canal (Yo violeta / Ellos cian).

    Responde a la pregunta "¿me está escuchando?" de un vistazo. Si AMBOS canales
    llevan ~4s planos, pasa a "⚠ Sin señal" (avisa de una fuente de audio mal
    enrutada sin que el usuario tenga que darle a "Me perdí" para enterarse).
    Se alimenta desde un timer del HUD que lee los niveles lock-free (get_levels).
    """

    _SIGNAL_THRESHOLD = 0.05   # nivel RMS (0..1) por debajo del cual el canal se considera mudo
    _COLOR_YO = (167, 139, 250)
    _COLOR_ELLOS = (34, 211, 238)

    def __init__(self, silent_limit: int, parent=None):
        super().__init__(parent)
        self.setFixedHeight(50)
        self._disp_yo = 0.0      # nivel mostrado (con decaimiento suave, tipo VU)
        self._disp_ellos = 0.0
        self._silent_updates = 0
        self._silent_limit = max(1, silent_limit)

    def set_levels(self, yo: float, ellos: float):
        yo = max(0.0, min(1.0, yo or 0.0))
        ellos = max(0.0, min(1.0, ellos or 0.0))
        # Peak-hold con decaimiento: sube al instante, baja suave (sensación VU).
        self._disp_yo = yo if yo > self._disp_yo else self._disp_yo * 0.82
        self._disp_ellos = ellos if ellos > self._disp_ellos else self._disp_ellos * 0.82
        if max(yo, ellos) < self._SIGNAL_THRESHOLD:
            self._silent_updates = min(self._silent_updates + 1, self._silent_limit + 1)
        else:
            self._silent_updates = 0
        self.update()

    @property
    def _silent(self) -> bool:
        return self._silent_updates >= self._silent_limit

    def _draw_bar(self, painter, x, y, w, h, level, rgb):
        track = QPainterPath()
        track.addRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
        painter.fillPath(track, QColor(255, 255, 255, 20))
        fill_w = max(0.0, min(1.0, level)) * w
        if fill_w > 1:
            fp = QPainterPath()
            fp.addRoundedRect(QRectF(x, y, fill_w, h), h / 2, h / 2)
            painter.fillPath(fp, QColor(rgb[0], rgb[1], rgb[2], 230))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        font = painter.font()

        # Fila 1: estado (dot + texto), verde si hay señal, ámbar si mudo.
        if self._silent:
            col = QColor(245, 158, 11)   # ámbar
            txt = "⚠ Sin señal — revisa la fuente de audio"
        else:
            col = QColor(74, 222, 128)   # verde
            txt = "● Escuchando"
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(col)
        painter.drawEllipse(14, 9, 7, 7)
        font.setPointSizeF(8.0)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(col)
        painter.drawText(27, 3, w - 40, 14, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)

        # Fila 2: dos barras VU con etiqueta.
        by, bh = 30, 8
        mid = w // 2
        painter.setFont(font)
        # Yo
        painter.setPen(QColor(self._COLOR_YO[0], self._COLOR_YO[1], self._COLOR_YO[2], 220))
        painter.drawText(14, by - 3, 24, 14, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Yo")
        self._draw_bar(painter, 40, by, mid - 50, bh, self._disp_yo, self._COLOR_YO)
        # Ellos
        painter.setPen(QColor(self._COLOR_ELLOS[0], self._COLOR_ELLOS[1], self._COLOR_ELLOS[2], 220))
        painter.drawText(mid, by - 3, 34, 14, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Ellos")
        self._draw_bar(painter, mid + 38, by, w - mid - 52, bh, self._disp_ellos, self._COLOR_ELLOS)
        painter.end()


class _Card(QFrame):
    """Una tarjeta individual dentro de la pila "Ahora"."""

    feedback = pyqtSignal(str, str, str, int)  # key, tipo, texto, value

    def __init__(self, card: dict, parent=None):
        super().__init__(parent)
        self.key = card.get("key", "")
        self.tipo = card.get("tipo", "")
        self.texto = card.get("texto", "")
        color, label, emoji = CARD_STYLES.get(self.tipo, _DEFAULT_CARD_STYLE)

        r, g, b, _ = QColor(color).getRgb()
        self.setObjectName("HudCard")
        self.setStyleSheet(
            f"QFrame#HudCard {{ background-color: rgba(255,255,255,22); "
            f"border: 1px solid rgba({r},{g},{b},110); border-radius: 10px; }}"
        )
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(8)

        chip = QLabel(emoji)
        chip.setFixedSize(34, 34)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setStyleSheet(
            f"background-color: rgba({r},{g},{b},38); border-radius: 10px; "
            f"font-size: 15px; border: none;"
        )
        outer.addWidget(chip, 0)

        body_col = QVBoxLayout()
        body_col.setSpacing(5)

        title = QLabel(label.upper())
        title.setStyleSheet(
            f"color: {color}; font-weight: 700; font-size: 10.5px; "
            f"letter-spacing: 0.5px; border: none; background: transparent;"
        )
        body_col.addWidget(title)

        body = QLabel(self.texto)
        body.setWordWrap(True)
        body.setStyleSheet(
            "color: rgba(255,255,255,235); font-size: 13px; border: none; background: transparent;"
        )
        body_col.addWidget(body)

        detail = card.get("detail")
        if detail:
            detail_lbl = QLabel(str(detail))
            detail_lbl.setWordWrap(True)
            detail_lbl.setStyleSheet(
                "color: rgba(255,255,255,130); font-size: 11px; border: none; background: transparent;"
            )
            body_col.addWidget(detail_lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        ok_btn = QPushButton("Útil")
        ok_btn.setFixedHeight(28)
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setStyleSheet(_feedback_btn_css("74,222,128"))
        ok_btn.clicked.connect(lambda: self.feedback.emit(self.key, self.tipo, self.texto, 1))
        bad_btn = QPushButton("Ruido")
        bad_btn.setFixedHeight(28)
        bad_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        bad_btn.setStyleSheet(_feedback_btn_css("248,113,113"))
        bad_btn.clicked.connect(lambda: self.feedback.emit(self.key, self.tipo, self.texto, -1))
        btn_row.addWidget(ok_btn, 1)
        btn_row.addWidget(bad_btn, 1)
        body_col.addLayout(btn_row)

        outer.addLayout(body_col, 1)

        self._ttl_timer = QTimer(self)
        self._ttl_timer.setSingleShot(True)
        self._ttl_timer.timeout.connect(self._expire)
        self._ttl_timer.start(CARD_TTL_MS)

    def _expire(self):
        self.feedback.emit(self.key, self.tipo, self.texto, 0)  # 0 = caducó, no es ni +1 ni -1
        self.setParent(None)
        self.deleteLater()


class _RegistroRow(QFrame):
    """Una fila de solo-lectura dentro del scroll de Registro."""

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        tipo = entry.get("tipo", "")
        texto = str(entry.get("texto", "") or "")
        time_label = str(entry.get("time", "") or "")
        color, bg, default_emoji = REGISTRO_STYLES.get(tipo, _DEFAULT_REGISTRO_STYLE)

        emoji = default_emoji
        for lead in _LEADING_EMOJIS:
            if texto.startswith(lead):
                emoji = lead
                texto = texto[len(lead):].lstrip()
                break

        self.setObjectName("RegistroRow")
        self.setStyleSheet(
            "QFrame#RegistroRow { background: transparent; border-radius: 9px; }"
            "QFrame#RegistroRow:hover { background-color: rgba(255,255,255,0.04); }"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 8, 6, 8)
        row.setSpacing(8)

        chip = QLabel(emoji)
        chip.setFixedSize(26, 26)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setStyleSheet(
            f"background-color: {bg}; border-radius: 8px; font-size: 12px; border: none;"
        )
        row.addWidget(chip, 0)

        text_lbl = QLabel()
        text_lbl.setStyleSheet(
            "color: rgba(255,255,255,0.85); font-size: 12.5px; border: none; background: transparent;"
        )
        fm = QFontMetrics(text_lbl.font())
        elided = fm.elidedText(texto, Qt.TextElideMode.ElideRight, 220)
        text_lbl.setText(elided)
        text_lbl.setToolTip(texto)
        row.addWidget(text_lbl, 1)

        time_lbl = QLabel(time_label)
        time_lbl.setStyleSheet(
            "color: rgba(255,255,255,0.35); font-size: 11px; border: none; background: transparent;"
        )
        row.addWidget(time_lbl, 0)


_ASK_MIN_HEIGHT = 34   # una línea (mismo alto que el QLineEdit anterior)
_ASK_MAX_HEIGHT = 90   # ~4 líneas; pasado esto, scroll interno


class _AutoGrowTextEdit(QTextEdit):
    """Input de pregunta libre: QTextEdit compacto que crece con el contenido.

    Enter envía (mismo contrato que el QLineEdit anterior: returnPressed →
    submit); Shift+Enter inserta salto de línea; Esc limpia y devuelve el
    foco. Se redimensiona entre ``_ASK_MIN_HEIGHT`` y ``_ASK_MAX_HEIGHT``
    según el tamaño real del documento (scrollbar interno pasado el máximo).
    """

    submitted = pyqtSignal()
    escaped = pyqtSignal()
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Pregunta sobre la reunión…")
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setStyleSheet(
            "QTextEdit { background-color: rgba(255,255,255,18); color: rgba(255,255,255,230); "
            "border: 1px solid rgba(255,255,255,40); border-radius: 12px; padding: 6px 10px; "
            "font-size: 12.5px; }"
            "QTextEdit:focus { border: 1px solid rgba(139,92,246,160); }"
        )
        self.document().documentLayout().documentSizeChanged.connect(self._adjust_height)
        # Altura de referencia de una sola línea vacía: todo crecimiento posterior
        # se mide como delta contra esta base, para que el estado inicial/vacío
        # caiga exactamente en _ASK_MIN_HEIGHT (evita doble conteo de márgenes/
        # frame del QTextEdit, que varían con la stylesheet aplicada).
        self._base_doc_h = self.document().size().height()
        self._adjust_height()

    def _adjust_height(self, *_args):
        doc_h = self.document().size().height()
        base = getattr(self, "_base_doc_h", doc_h)
        grown = max(0, int(doc_h - base))
        target = _ASK_MIN_HEIGHT + grown
        clamped = max(_ASK_MIN_HEIGHT, min(target, _ASK_MAX_HEIGHT))
        self.setFixedHeight(clamped)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if target > _ASK_MAX_HEIGHT
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

    def text(self) -> str:
        """Compat con el contrato previo del QLineEdit (usado internamente)."""
        return self.toPlainText().strip()

    def clear(self):
        super().clear()
        self._adjust_height()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)  # Shift+Enter: salto de línea normal
            else:
                self.submitted.emit()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.escaped.emit()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


class HudWidget(QWidget):
    """Panel lateral flotante nativo del HUD proactivo (unidad 5.4)."""

    feedback_requested = pyqtSignal(str, str, str, int)  # key, tipo, texto, value
    lost_requested = pyqtSignal()                         # botón "Me perdí"
    ask_requested = pyqtSignal(str)                        # texto de la pregunta libre

    # Modo Transform (unidad 3c). ``transform_accepted`` LLEVA el texto: el panel es
    # el único que tiene el resultado del modelo, así que no hay forma de que main.py
    # pegue algo que el usuario no haya visto y aceptado. Es el control de G1-A
    # expresado en la forma del código, no en la disciplina de quien lo llame.
    transform_accepted = pyqtSignal(str)
    transform_discarded = pyqtSignal()
    transform_copy_original_requested = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(_MIN_WIDTH, _MIN_HEIGHT)
        # El fondo/borde redondeado se pinta a mano en paintEvent (mismo patrón que
        # ui/pill_widget.py): un QWidget plano con WA_TranslucentBackground NO
        # garantiza que su stylesheet background-color/border-radius se pinte en
        # Windows — es la causa de que el HUD se viera "casi invisible".

        self._drag_pos = None
        self._saved_hwnd = None  # HWND frontal guardado al activar el input (foco deliberado)
        self._geometry_saved = False  # True tras el primer move/resize manual del usuario
        self._last_registro_sig = None  # (len, primer time, último time) — evita re-render inútil
        self._elapsed_label = "00:00"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ------------------------------------------------------------
        # (1) HEADER
        # ------------------------------------------------------------
        self.header = QFrame()
        self.header.setObjectName("HudHeader")
        self.header.setStyleSheet(
            "QFrame#HudHeader { border-bottom: 1px solid rgba(255,255,255,0.07); background: transparent; }"
        )
        header_row = QHBoxLayout(self.header)
        header_row.setContentsMargins(16, 15, 16, 12)
        header_row.setSpacing(8)

        self._dot = QLabel()
        self._dot.setFixedSize(8, 8)
        self._dot.setStyleSheet("background-color: #d97706; border-radius: 4px; border: none;")
        header_row.addWidget(self._dot, 0)

        title_lbl = QLabel("Reunión · copiloto")
        title_lbl.setStyleSheet(
            "color: rgba(255,255,255,0.6); font-size: 12.5px; font-weight: 500; "
            "border: none; background: transparent;"
        )
        header_row.addWidget(title_lbl, 1)
        self._title_lbl = title_lbl  # el modo Transform (3c) le cambia el texto

        self.timer_label = QLabel("00:00")
        self.timer_label.setStyleSheet(
            "color: rgba(255,255,255,0.5); font-size: 12.5px; border: none; background: transparent;"
        )
        header_row.addWidget(self.timer_label, 0)

        self.reset_btn = QPushButton("⤢")
        self.reset_btn.setToolTip("Restablecer posición y tamaño")
        self.reset_btn.setFixedSize(22, 22)
        self.reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(255,255,255,0.45); "
            "border: none; font-size: 13px; padding: 0px; }"
            "QPushButton:hover { color: rgba(255,255,255,0.85); }"
        )
        self.reset_btn.clicked.connect(self.reset_geometry)
        header_row.addWidget(self.reset_btn, 0)

        root.addWidget(self.header)

        # ------------------------------------------------------------
        # (1.5) INDICADOR DE CAPTURA EN VIVO (¿me está escuchando?)
        # ------------------------------------------------------------
        listen_wrap = QFrame()
        listen_wrap.setObjectName("HudListen")
        listen_wrap.setStyleSheet(
            "QFrame#HudListen { border-bottom: 1px solid rgba(255,255,255,0.07); background: transparent; }"
        )
        listen_layout = QVBoxLayout(listen_wrap)
        listen_layout.setContentsMargins(2, 4, 2, 6)
        listen_layout.setSpacing(0)
        # ~4s de silencio en AMBOS canales antes de avisar "Sin señal" (el timer
        # de niveles corre a _LEVEL_INTERVAL_MS).
        self.listening = _ListeningStrip(silent_limit=int(4000 / _LEVEL_INTERVAL_MS))
        listen_layout.addWidget(self.listening)
        root.addWidget(listen_wrap)
        self._listen_wrap = listen_wrap

        # Provider de niveles (inyectado por main.py: MEETING.get_levels) + timer
        # propio del HUD: lee los floats lock-free ~cada 80ms para un VU fluido sin
        # depender del tick de 1s de main.py. Solo corre con el HUD visible.
        self._level_provider = None
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(_LEVEL_INTERVAL_MS)
        self._level_timer.timeout.connect(self._poll_levels)

        # ------------------------------------------------------------
        # (2) ZONA "AHORA"
        # ------------------------------------------------------------
        now_section = QFrame()
        now_layout = QVBoxLayout(now_section)
        now_layout.setContentsMargins(12, 12, 12, 6)
        now_layout.setSpacing(0)

        now_label = _SectionLabel("Ahora")
        now_label.setContentsMargins(0, 0, 0, 8)
        now_layout.addWidget(now_label)

        self.cards_container = QVBoxLayout()
        self.cards_container.setSpacing(6)
        now_layout.addLayout(self.cards_container)
        self._cards: list[_Card] = []

        self.cards_placeholder = QLabel("Sin sugerencias ahora")
        self.cards_placeholder.setStyleSheet(
            "color: rgba(255,255,255,0.3); font-size: 12px; border: none; background: transparent;"
        )
        now_layout.addWidget(self.cards_placeholder)

        # Línea de confirmación de highlight (se desvanece a los 2s)
        self.highlight_label = QLabel("")
        self.highlight_label.setStyleSheet(
            "color: #fbbf24; font-size: 12px; font-weight: 600; border: none; background: transparent;"
        )
        self.highlight_label.setVisible(False)
        now_layout.addWidget(self.highlight_label)
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(lambda: self.highlight_label.setVisible(False))

        root.addWidget(now_section, 0)
        self._now_section = now_section

        # ------------------------------------------------------------
        # (3) ZONA "REGISTRO" (flex: ocupa el resto)
        # ------------------------------------------------------------
        registro_section = QFrame()
        registro_layout = QVBoxLayout(registro_section)
        registro_layout.setContentsMargins(12, 8, 12, 0)
        registro_layout.setSpacing(0)

        registro_label = _SectionLabel("Registro de la reunión")
        registro_label.setContentsMargins(0, 0, 0, 8)
        registro_layout.addWidget(registro_label)

        self.registro_scroll = QScrollArea()
        self.registro_scroll.setWidgetResizable(True)
        self.registro_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.registro_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,70); border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self.registro_column = QWidget()
        self.registro_column_layout = QVBoxLayout(self.registro_column)
        self.registro_column_layout.setContentsMargins(0, 0, 0, 8)
        self.registro_column_layout.setSpacing(2)
        self.registro_column_layout.addStretch(1)
        self.registro_scroll.setWidget(self.registro_column)

        self.registro_placeholder = QLabel("Aún no hay eventos en esta reunión")
        self.registro_placeholder.setStyleSheet(
            "color: rgba(255,255,255,0.3); font-size: 12px; border: none; background: transparent;"
        )
        self.registro_column_layout.insertWidget(0, self.registro_placeholder)

        registro_layout.addWidget(self.registro_scroll, 1)
        root.addWidget(registro_section, 1)
        self._registro_section = registro_section

        # ------------------------------------------------------------
        # (4) BOTTOM
        # ------------------------------------------------------------
        bottom_section = QFrame()
        bottom_section.setObjectName("HudBottom")
        bottom_section.setStyleSheet(
            "QFrame#HudBottom { border-top: 1px solid rgba(255,255,255,0.07); background: transparent; }"
        )
        bottom_layout = QVBoxLayout(bottom_section)
        bottom_layout.setContentsMargins(12, 10, 12, 12)
        bottom_layout.setSpacing(8)

        self.lost_btn = QPushButton("🧭  Me perdí · resumir · AltGr+M")
        self.lost_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lost_btn.setFixedHeight(32)
        self.lost_btn.setStyleSheet(
            "QPushButton { background-color: rgba(139,92,246,0.16); color: #ddd6fe; "
            "border: 1px solid rgba(139,92,246,0.45); border-radius: 8px; "
            "font-size: 12px; font-weight: 600; text-align: center; padding: 0px; }"
            "QPushButton:hover { background-color: rgba(139,92,246,0.28); }"
            "QPushButton:pressed { background-color: rgba(139,92,246,0.4); }"
        )
        self.lost_btn.clicked.connect(self._on_lost_clicked)
        bottom_layout.addWidget(self.lost_btn)

        self.lost_spinner_label = QLabel("Resumiendo…")
        self.lost_spinner_label.setStyleSheet(
            "color: rgba(255,255,255,160); font-size: 11.5px; border: none; background: transparent;"
        )
        self.lost_spinner_label.setVisible(False)
        bottom_layout.addWidget(self.lost_spinner_label)

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
        bottom_layout.addWidget(self.lost_scroll)

        self.ask_input = _AutoGrowTextEdit()
        self.ask_input.submitted.connect(self._on_ask_submitted)
        self.ask_input.escaped.connect(self._on_ask_escaped)
        self.ask_input.clicked.connect(self._activate_for_input)
        bottom_layout.addWidget(self.ask_input)

        root.addWidget(bottom_section, 0)
        self._bottom_section = bottom_section

        # ------------------------------------------------------------
        # (5) MODO TRANSFORM (unidad 3c) — la previsualización de G1-A.
        #
        # Es un MODO de este panel, no un widget nuevo: el HUD ya es una ventana
        # flotante que no roba el foco, con Enter/Esc y con el cableado hecho en
        # main.py, así que construir otro habría duplicado todo eso, incluida la
        # corrección pagada de jamás togglear WindowDoesNotAcceptFocus en caliente.
        # Oculto mientras no haya una transformación en curso.
        # ------------------------------------------------------------
        transform_section = QFrame()
        transform_section.setObjectName("HudTransform")
        transform_layout = QVBoxLayout(transform_section)
        transform_layout.setContentsMargins(12, 12, 12, 12)
        transform_layout.setSpacing(8)

        self.transform_prompt_label = QLabel("")
        self.transform_prompt_label.setStyleSheet(
            "color: rgba(255,255,255,0.45); font-size: 11px; font-weight: 600; "
            "border: none; background: transparent;"
        )
        transform_layout.addWidget(self.transform_prompt_label)

        self.transform_scroll = QScrollArea()
        self.transform_scroll.setWidgetResizable(True)
        self.transform_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.transform_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,70); border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self.transform_text_label = QLabel("")
        self.transform_text_label.setWordWrap(True)
        self.transform_text_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.transform_text_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.transform_text_label.setStyleSheet(
            "color: rgba(255,255,255,235); font-size: 12.5px; padding: 4px; background: transparent;"
        )
        self.transform_scroll.setWidget(self.transform_text_label)
        transform_layout.addWidget(self.transform_scroll, 1)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(6)
        self.transform_apply_btn = QPushButton("Aplicar · Enter")
        self.transform_apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.transform_apply_btn.setFixedHeight(30)
        self.transform_apply_btn.setStyleSheet(
            "QPushButton { background-color: rgba(16,185,129,0.18); color: #a7f3d0; "
            "border: 1px solid rgba(16,185,129,0.45); border-radius: 8px; "
            "font-size: 12px; font-weight: 600; padding: 0px; }"
            "QPushButton:hover { background-color: rgba(16,185,129,0.3); }"
        )
        self.transform_apply_btn.clicked.connect(self._on_transform_apply)
        actions_row.addWidget(self.transform_apply_btn, 1)

        self.transform_discard_btn = QPushButton("Descartar · Esc")
        self.transform_discard_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.transform_discard_btn.setFixedHeight(30)
        self.transform_discard_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(255,255,255,0.55); "
            "border: 1px solid rgba(255,255,255,0.18); border-radius: 8px; "
            "font-size: 12px; font-weight: 600; padding: 0px; }"
            "QPushButton:hover { color: rgba(255,255,255,0.9); }"
        )
        self.transform_discard_btn.clicked.connect(self._on_transform_discard)
        actions_row.addWidget(self.transform_discard_btn, 1)
        transform_layout.addLayout(actions_row)

        # "Copiar original": la tercera capa de reversión de la unidad 3z. Existe
        # porque el Deshacer del historial (6.2) NO cubre Transform — el texto que
        # habría que restaurar no está en la base de datos, está en el documento
        # del usuario. Vive en RAM y muere con el panel: nada de esto se persiste.
        self.transform_copy_btn = QPushButton("Copiar el texto original")
        self.transform_copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.transform_copy_btn.setFixedHeight(26)
        self.transform_copy_btn.setStyleSheet(
            "QPushButton { background: transparent; color: rgba(255,255,255,0.4); "
            "border: none; font-size: 11px; padding: 0px; text-align: center; }"
            "QPushButton:hover { color: rgba(255,255,255,0.75); }"
        )
        self.transform_copy_btn.clicked.connect(self._on_transform_copy_original)
        transform_layout.addWidget(self.transform_copy_btn)

        self.transform_status_label = QLabel("")
        self.transform_status_label.setWordWrap(True)
        self.transform_status_label.setStyleSheet(
            "color: #fbbf24; font-size: 11.5px; border: none; background: transparent;"
        )
        self.transform_status_label.setVisible(False)
        transform_layout.addWidget(self.transform_status_label)

        transform_section.setVisible(False)
        root.addWidget(transform_section, 1)
        self._transform_section = transform_section

        # Estado del modo, SOLO en memoria (unidad 3z: un Transform no se persiste).
        self._transform_mode = False
        self._transform_original = None   # texto seleccionado por el usuario
        self._transform_result = None     # salida del modelo pendiente de aceptar

        # ------------------------------------------------------------
        # QSizeGrip (redimensionable) — esquina inferior derecha, funciona en
        # ventanas frameless. Se superpone al bottom_section vía posicionamiento
        # absoluto tras cada resize (ver resizeEvent).
        # ------------------------------------------------------------
        self.size_grip = QSizeGrip(self)
        self.size_grip.setFixedSize(16, 16)
        self.size_grip.setStyleSheet("background: transparent;")

        self._update_placeholders()

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
        """Añade una tarjeta a la pila "Ahora" (máx 3 visibles; la más vieja se retira)."""
        c = _Card(card, parent=self)
        c.feedback.connect(self._on_card_feedback)
        self.cards_container.addWidget(c)
        self._cards.append(c)
        while len(self._cards) > _MAX_CARDS:
            old = self._cards.pop(0)
            old.setParent(None)
            old.deleteLater()
        self._update_placeholders()

    def set_registro(self, entries: list):
        """Repuebla la columna de Registro desde MEETING.get_registro() (idempotente).

        Compara por longitud + primer/último ``time`` contra el último render para
        evitar reconstruir la lista (y parpadear) cuando no cambió nada relevante.
        """
        entries = entries or []
        sig = (
            len(entries),
            entries[0].get("time") if entries else None,
            entries[-1].get("time") if entries else None,
        )
        if sig == self._last_registro_sig:
            return
        self._last_registro_sig = sig

        # Limpia filas previas (deja el placeholder y el stretch final intactos:
        # se re-insertan al final de este método).
        layout = self.registro_column_layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self.registro_placeholder:
                w.setParent(None)
                w.deleteLater()

        if not entries:
            layout.addWidget(self.registro_placeholder)
            self.registro_placeholder.setVisible(True)
            layout.addStretch(1)
            return

        self.registro_placeholder.setVisible(False)
        for entry in entries:
            row = _RegistroRow(entry, parent=self.registro_column)
            layout.addWidget(row)
        layout.addStretch(1)

    def set_timer_label(self, elapsed_fmt: str):
        """Actualiza el timer mm:ss/hh:mm:ss del header (llamado ~1/s desde main.py)."""
        self._elapsed_label = elapsed_fmt
        self.timer_label.setText(elapsed_fmt)

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

    # ------------------------------------------------------------------
    # Modo Transform (unidad 3c) — la previsualización de G1-A
    # ------------------------------------------------------------------

    def _set_meeting_sections_visible(self, visible: bool):
        for section in (self._listen_wrap, self._now_section,
                        self._registro_section, self._bottom_section):
            section.setVisible(visible)

    def enter_transform_mode(self, original: str, prompt_label: str):
        """Entra al modo previsualización y muestra el spinner (aún no hay resultado).

        ``original`` se guarda EN MEMORIA para el botón "copiar el texto original"
        y se borra al salir del modo. No se escribe en ninguna parte.
        """
        self._transform_mode = True
        self._transform_original = original
        self._transform_result = None
        self._title_lbl.setText(f"Transform · {prompt_label}")
        self.transform_prompt_label.setText("Transformando…")
        self.transform_text_label.setText("")
        self.transform_status_label.setVisible(False)
        self._set_transform_actions_enabled(False)
        self._set_meeting_sections_visible(False)
        self._transform_section.setVisible(True)

    def show_transform_result(self, text: str):
        """Pinta el resultado del modelo y habilita Aplicar/Descartar.

        Este es el ÚNICO camino por el que la salida del modelo entra a la interfaz:
        el texto se queda aquí hasta que el usuario acepte, y solo entonces viaja en
        ``transform_accepted``.
        """
        if not self._transform_mode:
            return
        self._transform_result = text
        self.transform_prompt_label.setText("Revisa antes de aplicar")
        self.transform_text_label.setText(text)
        self._set_transform_actions_enabled(True)
        self._activate_for_transform()

    def show_transform_error(self, message: str):
        """Muestra el fallo dentro del propio panel (nunca una notificación muda)."""
        if not self._transform_mode:
            return
        self._transform_result = None
        self.transform_prompt_label.setText("No se pudo transformar")
        self.transform_status_label.setText(message)
        self.transform_status_label.setVisible(True)
        self._set_transform_actions_enabled(False)
        self.transform_discard_btn.setEnabled(True)
        self._activate_for_transform()

    def exit_transform_mode(self):
        """Sale del modo y BORRA el texto original y el resultado de la memoria."""
        self._transform_mode = False
        self._transform_original = None
        self._transform_result = None
        self.transform_text_label.setText("")
        self.transform_status_label.setVisible(False)
        self._transform_section.setVisible(False)
        self._title_lbl.setText("Reunión · copiloto")
        self._set_meeting_sections_visible(True)

    def is_transform_mode(self) -> bool:
        return self._transform_mode

    def _set_transform_actions_enabled(self, enabled: bool):
        self.transform_apply_btn.setEnabled(enabled)
        self.transform_discard_btn.setEnabled(True)   # descartar siempre disponible
        self.transform_copy_btn.setEnabled(enabled or self._transform_original is not None)

    def _activate_for_transform(self):
        """Foco deliberado para que Enter y Esc lleguen al panel.

        Mismo patrón que ``_activate_for_input`` y por la misma razón: los flags de
        ventana NO se tocan en caliente (ver la cabecera del módulo), se activa la
        ventana. El HWND anterior queda guardado para devolver el foco al salir.
        """
        try:
            self._saved_hwnd = _user32.GetForegroundWindow()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hud: no se pudo guardar HWND frontal (transform): %s", exc)
            self._saved_hwnd = None
        self.activateWindow()
        self.setFocus()

    def _on_transform_apply(self):
        text = self._transform_result
        if not text:
            return
        self.exit_transform_mode()
        self._restore_focus()
        self.transform_accepted.emit(text)

    def _on_transform_discard(self):
        self.exit_transform_mode()
        self._restore_focus()
        self.transform_discarded.emit()

    def _on_transform_copy_original(self):
        original = self._transform_original
        if not original:
            return
        self.transform_copy_original_requested.emit(original)
        self.transform_status_label.setText("Texto original copiado al portapapeles.")
        self.transform_status_label.setVisible(True)

    def keyPressEvent(self, event):
        """Enter aplica y Esc descarta, SOLO en modo Transform.

        Fuera del modo se delega al comportamiento normal para no secuestrar teclas
        del panel de reunión.
        """
        if self._transform_mode:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._on_transform_apply()
                event.accept()
                return
            if key == Qt.Key.Key_Escape:
                self._on_transform_discard()
                event.accept()
                return
        super().keyPressEvent(event)

    def reset_geometry(self):
        """Botón "restablecer" del header: mueve el panel al lado derecho de la
        pantalla y restaura el tamaño por defecto (340 x 72% del alto disponible)."""
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        width = _DEFAULT_WIDTH
        height = int(geo.height() * 0.72)
        x = geo.right() - width - _RIGHT_MARGIN
        y = geo.top() + _TOP_MARGIN
        self.setGeometry(x, y, width, height)
        self._geometry_saved = True

    def ensure_initial_geometry(self):
        """Aplica la geometría por defecto SOLO si el usuario nunca movió/redimensionó
        el panel a mano. Se llama antes de cada show() (reemplaza el anchor_near de
        la 5.3: el panel ya no es un popup pegado a la pill)."""
        if not self._geometry_saved:
            self.reset_geometry()

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

    def set_level_provider(self, fn):
        """Inyecta la fuente de niveles de audio: fn() -> (nivel_yo, nivel_ellos),
        ambos 0..1. main.py pasa MEETING.get_levels (lock-free). Desacopla el HUD
        de core/ (no importa MEETING)."""
        self._level_provider = fn

    def _poll_levels(self):
        yo, ellos = 0.0, 0.0
        if self._level_provider is not None:
            try:
                yo, ellos = self._level_provider()
            except Exception:  # noqa: BLE001 — un fallo de lectura no debe tumbar el HUD
                yo, ellos = 0.0, 0.0
        self.listening.set_levels(yo, ellos)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.force_topmost)
        self._topmost_timer.start()
        self._level_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._topmost_timer.stop()
        self._level_timer.stop()
        # Cerrar el panel BORRA la transformación pendiente (unidad 3z: vive en RAM
        # y muere con el panel). Además evita que un resultado quede esperando un
        # Enter que llegaría cuando el usuario ya está en otra cosa.
        if self._transform_mode:
            self.exit_transform_mode()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Reposiciona el grip a la esquina inferior derecha en cada resize.
        self.size_grip.move(self.width() - self.size_grip.width(),
                             self.height() - self.size_grip.height())
        if self.isVisible():
            self._geometry_saved = True

    def paintEvent(self, event):
        """Pinta el fondo redondeado semi-translúcido a mano (ver nota en __init__:
        un QWidget plano con solo WA_TranslucentBackground no garantiza que su
        stylesheet background-color/border-radius se pinte en Windows)."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 22, 22)
        painter.fillPath(path, QColor(17, 17, 21, 245))
        painter.setPen(QPen(QColor(139, 92, 246, 100), 1.2))
        painter.drawPath(path)
        painter.end()

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    def _update_placeholders(self):
        self.cards_placeholder.setVisible(len(self._cards) == 0)

    def _on_card_feedback(self, key: str, tipo: str, texto: str, value: int):
        if value != 0:  # 0 = caducidad silenciosa, no se reporta como feedback
            self.feedback_requested.emit(key, tipo, texto, value)
        if key in [c.key for c in self._cards]:
            return
        # La tarjeta ya se retiró de self._cards en add_card() o por _expire();
        # cuando el widget termina de destruirse, actualizamos el placeholder.
        QTimer.singleShot(0, self._update_placeholders)

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

    def _on_ask_escaped(self):
        """Esc dentro del input de pregunta: limpia y devuelve el foco (mismo
        contrato que el QLineEdit anterior, ahora conectado por señal en vez de
        eventFilter ya que ``_AutoGrowTextEdit`` emite ``escaped`` directamente)."""
        self.ask_input.clear()
        self._restore_focus()

    # ------------------------------------------------------------------
    # Arrastre — SOLO desde la banda del header (para no chocar con clicks
    # en tarjetas/scroll del registro; a diferencia de la 5.3, ahora el panel
    # tiene contenido interactivo en casi toda su superficie).
    # ------------------------------------------------------------------

    def _in_header_band(self, pos) -> bool:
        return 0 <= pos.y() <= self.header.height()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._in_header_band(event.position().toPoint()):
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

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
            self._geometry_saved = True
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_pos is not None:
            self._drag_pos = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)
