import ctypes
import ctypes.wintypes
import logging
import math
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QPen, QPixmap, QCursor
from ui.audio_visualizer import AudioVisualizer
from config import (
    PILL_WIDTH_IDLE,
    PILL_WIDTH_RECORDING,
    PILL_WIDTH_STATUS,
    PILL_HEIGHT,
    PILL_HEIGHT_IDLE,
    PILL_OPACITY,
    PILL_MARGIN_BOTTOM,
    LOGO_SIZE,
    LOGO_PATH,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 API type annotations (critical for 64-bit Windows)
# Without these, ctypes defaults to c_int (32-bit) for all parameters,
# truncating 64-bit HWND pointers.  See also core/clipboard.py.
# ---------------------------------------------------------------------------
_user32 = ctypes.windll.user32
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
_user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
_user32.IsWindow.restype = ctypes.wintypes.BOOL

_HWND_TOPMOST = ctypes.wintypes.HWND(-1)


class PillWidget(QWidget):
    """Minimal floating pill. Logo + bars when recording, tiny icons for status."""

    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"
    STATE_DONE = "done"
    STATE_ERROR = "error"

    # Clic corto (sin arrastre) con reunión activa: pide abrir el dashboard en /reunion.
    open_dashboard_requested = pyqtSignal()

    def __init__(self):
        """Configura la ventana flotante, timers de animación y el visualizador de audio."""
        super().__init__()
        self._state = self.STATE_IDLE
        self._target_width = PILL_WIDTH_IDLE
        self._current_width = float(PILL_WIDTH_IDLE)
        self._target_height = PILL_HEIGHT_IDLE
        self._current_height = float(PILL_HEIGHT_IDLE)
        self._bottom_anchor_y: int = 0  # set in _position_on_screen
        self._drag_pos = None
        self._press_pos = None  # posición global del mousePress, para distinguir clic de arrastre
        self._DRAG_THRESHOLD = 6  # px: por debajo de esto, un release cuenta como "clic"

        # Estado de reunión (distinto del dictado): acento propio + timer + indicador
        # compacto del canal "Ellos" (NO se reescribe AudioVisualizer, ver CLAUDE.md).
        self._meeting_mode = False
        self._meeting_paused = False
        self._meeting_elapsed_fmt = "00:00"
        self._meeting_level_ellos = 0.0
        self._meeting_source_system = False  # AUDIO_SOURCE=system: matiz/glifo levemente distinto
        self._bg_color_active = QColor(15, 15, 15, int(255 * PILL_OPACITY))
        self._bg_color_idle = QColor(140, 140, 140, 176)  # gray, 20% more translucent
        self._bg_color = self._bg_color_idle  # start in idle
        # Acento de reunión: ámbar (distinto del violeta de dictado) para que el
        # usuario diferencie "grabando reunión" de "dictando" de un vistazo.
        self._meeting_accent = QColor(217, 119, 6)       # ámbar (AUDIO_SOURCE=mic)
        self._meeting_accent_system = QColor(202, 138, 4)  # ámbar con matiz distinto (AUDIO_SOURCE=system)
        self._meeting_cyan = QColor(34, 211, 238)          # indicador "Ellos" (mismo cian del dashboard)

        self._logo = QPixmap(LOGO_PATH)
        if not self._logo.isNull():
            self._logo = self._logo.scaled(
                LOGO_SIZE, LOGO_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        self._show_checkmark = False
        self._show_spinner = False
        self._show_error = False
        self._spinner_angle = 0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedHeight(PILL_HEIGHT_IDLE)
        self.setFixedWidth(PILL_WIDTH_IDLE)

        self.visualizer = AudioVisualizer(parent=self)
        self.visualizer.setVisible(False)

        self._anim_timer = QTimer()
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._animate_size)

        self._spinner_timer = QTimer()
        self._spinner_timer.setInterval(50)
        self._spinner_timer.timeout.connect(self._animate_spinner)

        self._done_timer = QTimer()
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(lambda: self.set_state(self.STATE_IDLE))

        # Enforce always-on-top every 1 s (Qt's hint alone is unreliable on Windows)
        self._topmost_timer = QTimer()
        self._topmost_timer.setInterval(1000)
        self._topmost_timer.timeout.connect(self._force_topmost)
        self._topmost_timer.start()

        # Re-assert topmost whenever any window gains focus
        app = QApplication.instance()
        if app:
            app.focusChanged.connect(lambda _old, _new: QTimer.singleShot(50, self._force_topmost))

        self._position_on_screen()

        # Reposicionar la pill si cambia la configuración de monitores
        # (reutiliza la variable `app` ya asignada arriba)
        if app:
            app.primaryScreenChanged.connect(lambda _s: QTimer.singleShot(100, self._ensure_on_screen))
            app.screenAdded.connect(lambda _s: QTimer.singleShot(100, self._ensure_on_screen))
            app.screenRemoved.connect(lambda _s: QTimer.singleShot(100, self._ensure_on_screen))

    def _position_on_screen(self):
        """Centra la pill horizontalmente en la parte inferior del monitor donde está el cursor.

        Si ``screenAt`` no puede determinar el monitor (por ejemplo, cuando el cursor
        está fuera de todos los monitores), usa la pantalla primaria como fallback.
        """
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            # Ancla inferior: el borde inferior de la pill se mantiene aquí siempre
            self._bottom_anchor_y = geo.bottom() - PILL_MARGIN_BOTTOM
            x = geo.center().x() - PILL_WIDTH_IDLE // 2
            y = self._bottom_anchor_y - int(self._current_height)
            self.move(x, y)

    def _ensure_on_screen(self):
        """Reposiciona la pill si quedó fuera de todos los monitores disponibles.

        Se invoca automáticamente cuando se añade, elimina o cambia la pantalla
        primaria. Solo actúa si el centro de la pill no cae dentro de ninguna
        geometría disponible, para no interrumpir posiciones de arrastre válidas.
        """
        try:
            center = self.frameGeometry().center()
            visible = any(
                s.availableGeometry().contains(center)
                for s in QApplication.screens()
            )
            if not visible:
                logger.debug(
                    "_ensure_on_screen: pill fuera de pantalla (%s, %s) — reposicionando",
                    center.x(), center.y(),
                )
                self._position_on_screen()
        except Exception as exc:
            logger.warning("_ensure_on_screen falló: %s", exc)

    def set_meeting_state(self, active: bool, paused: bool = False, elapsed_fmt: str = "00:00",
                           level_ellos: float = 0.0, source_system: bool = False):
        """Actualiza el estado de reunión mostrado en la pill (llamado desde main.py ~1/s).

        No cambia self._state (idle/recording/...): la reunión pinta un acento propio
        ENCIMA del estado normal de dictado mientras `active` es True. Solo repinta
        (sin animación de tamaño): timer/indicador son overlays baratos.
        """
        self._meeting_mode = active
        self._meeting_paused = paused
        self._meeting_elapsed_fmt = elapsed_fmt
        self._meeting_level_ellos = max(0.0, min(1.0, level_ellos))
        self._meeting_source_system = source_system
        self.update()

    def set_state(self, state: str):
        """Cambia el estado visual de la pill (idle, recording, processing, done, error)."""
        self._state = state
        self._show_checkmark = False
        self._show_spinner = False
        self._show_error = False
        self._spinner_timer.stop()

        if state == self.STATE_IDLE:
            self._bg_color = self._bg_color_idle
            self._target_width = PILL_WIDTH_IDLE
            self._target_height = PILL_HEIGHT_IDLE
            self.visualizer.setVisible(False)
            self.visualizer.stop()
        elif state == self.STATE_RECORDING:
            self._bg_color = self._bg_color_active
            self._target_width = PILL_WIDTH_RECORDING
            self._target_height = PILL_HEIGHT
            self.visualizer.setVisible(True)
            self.visualizer.start()
        elif state == self.STATE_PROCESSING:
            self._target_width = PILL_WIDTH_STATUS
            self._target_height = PILL_HEIGHT
            self._show_spinner = True
            self._spinner_timer.start()
            self.visualizer.setVisible(False)
            self.visualizer.stop()
        elif state == self.STATE_DONE:
            self._target_width = PILL_WIDTH_STATUS
            self._target_height = PILL_HEIGHT
            self._show_checkmark = True
            self.visualizer.setVisible(False)
            self.visualizer.stop()
            self._done_timer.start(800)
        elif state == self.STATE_ERROR:
            self._target_width = PILL_WIDTH_STATUS
            self._target_height = PILL_HEIGHT
            self._show_error = True
            self.visualizer.setVisible(False)
            self.visualizer.stop()
            self._done_timer.start(1200)

        if not self._anim_timer.isActive():
            self._anim_timer.start()
        self.update()

    def _animate_spinner(self):
        """Avanza el ángulo del spinner de procesamiento y repinta."""
        self._spinner_angle = (self._spinner_angle + 30) % 360
        self.update()

    def _animate_size(self):
        """Interpola suavemente el ancho y alto de la pill hacia los tamaños objetivo."""
        # Width lerp
        dw = self._target_width - self._current_width
        if abs(dw) < 1:
            self._current_width = float(self._target_width)
        else:
            self._current_width += dw * 0.22

        # Height lerp
        dh = self._target_height - self._current_height
        if abs(dh) < 1:
            self._current_height = float(self._target_height)
        else:
            self._current_height += dh * 0.22

        # Stop only when both dimensions have settled
        if abs(self._target_width - self._current_width) < 1 and abs(self._target_height - self._current_height) < 1:
            self._anim_timer.stop()

        # Anchor left edge (width expands right) and bottom edge (height grows up)
        left_x = self.x()
        new_w = int(self._current_width)
        new_h = int(self._current_height)
        self.setFixedWidth(new_w)
        self.setFixedHeight(new_h)
        self.move(left_x, self._bottom_anchor_y - new_h)
        self._layout_children()
        self.update()

    def _layout_children(self):
        """Reposiciona el visualizador de audio dentro de la pill."""
        w = int(self._current_width)
        h = int(self._current_height)
        logo_pad = 6
        logo_area = logo_pad + LOGO_SIZE + 4
        content_w = w - logo_area - 4
        if content_w > 0 and self.visualizer.isVisible():
            self.visualizer.setGeometry(logo_area, 2, content_w, h - 4)

    def paintEvent(self, event):
        """Dibuja el fondo redondeado, logo e iconos de estado (check, spinner, error)."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        # Dynamic corner radius so thin-line state stays pill-shaped
        radius = h / 2.0

        # Background
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(w), float(h), radius, radius)
        painter.fillPath(path, self._bg_color)

        # Border (more visible in idle so the thin line is findable)
        border_alpha = 40 if h < 12 else 12
        painter.setPen(QPen(QColor(255, 255, 255, border_alpha), 0.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(0, 0, w, h, radius, radius)

        # Anillo de reunión: acento ámbar (distinto del violeta de dictado) para que el
        # usuario diferencie "reunión" de "dictado" de un vistazo. Se dibuja ENCIMA del
        # borde normal, sin cambiar el resto del estado visual.
        if self._meeting_mode and h >= 10:
            ring_color = self._meeting_accent_system if self._meeting_source_system else self._meeting_accent
            ring = QPen(ring_color, 2.0)
            painter.setPen(ring)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(1, 1, w - 2, h - 2, radius - 1, radius - 1)

        # Skip content when thin (idle line — no logo, no icons)
        if h < 10:
            painter.end()
            return

        # Logo
        if not self._logo.isNull():
            lx = 6
            ly = (h - LOGO_SIZE) // 2
            painter.drawPixmap(lx, ly, self._logo)

        # Status icons - positioned right of logo, centered in remaining space
        icon_cx = 6 + LOGO_SIZE + 4 + (w - 6 - LOGO_SIZE - 4 - 4) // 2
        icon_cy = h // 2

        if self._show_checkmark:
            pen = QPen(QColor(80, 210, 120), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(icon_cx - 4, icon_cy, icon_cx - 1, icon_cy + 3)
            painter.drawLine(icon_cx - 1, icon_cy + 3, icon_cx + 5, icon_cy - 3)

        elif self._show_spinner:
            painter.setPen(Qt.PenStyle.NoPen)
            for i in range(6):
                angle = math.radians(self._spinner_angle + i * 60)
                dx = 5 * math.cos(angle)
                dy = 5 * math.sin(angle)
                alpha = 220 - i * 35
                painter.setBrush(QColor(255, 255, 255, max(alpha, 30)))
                s = 2
                painter.drawEllipse(int(icon_cx + dx) - 1, int(icon_cy + dy) - 1, s, s)

        elif self._show_error:
            pen = QPen(QColor(255, 70, 70), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(icon_cx - 3, icon_cy - 3, icon_cx + 3, icon_cy + 3)
            painter.drawLine(icon_cx - 3, icon_cy + 3, icon_cx + 3, icon_cy - 3)

        # Overlay de reunión: timer mm:ss (o ⏸ parpadeante en pausa) + indicador
        # compacto cian de actividad del canal "Ellos" (confirma que se captura sin
        # tocar AudioVisualizer, que sigue mostrando solo el mic).
        if self._meeting_mode and self._state == self.STATE_RECORDING:
            text_x = 6 + LOGO_SIZE + 6
            text_w = max(w - text_x - 12, 0)
            if text_w > 10:
                painter.setPen(QColor(255, 255, 255, 235))
                font = painter.font()
                font.setPointSizeF(max(font.pointSizeF(), 8.0))
                painter.setFont(font)
                if self._meeting_paused and (self._spinner_angle // 60) % 2 == 0:
                    label = "⏸"  # glifo de pausa, parpadea reutilizando el ángulo del spinner
                else:
                    label = self._meeting_elapsed_fmt
                painter.drawText(text_x, 0, text_w - 10, h, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
                # Indicador "Ellos": punto cian cuya opacidad sigue el nivel del canal.
                dot_r = 3
                dot_cx = w - 10
                dot_cy = h // 2
                alpha = int(60 + 180 * self._meeting_level_ellos)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(self._meeting_cyan.red(), self._meeting_cyan.green(),
                                         self._meeting_cyan.blue(), min(alpha, 255)))
                painter.drawEllipse(dot_cx - dot_r, dot_cy - dot_r, dot_r * 2, dot_r * 2)

        painter.end()

    def _force_topmost(self):
        """Fuerza la ventana al frente usando Win32 SetWindowPos (64-bit safe)."""
        try:
            wid = self.winId()
            if not wid:
                return
            hwnd = ctypes.wintypes.HWND(int(wid))
            if not _user32.IsWindow(hwnd):
                return
            SWP_NOMOVE     = 0x0002
            SWP_NOSIZE     = 0x0001
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            _user32.SetWindowPos(
                hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception as exc:
            logger.warning("_force_topmost failed: %s", exc)

    def showEvent(self, event):
        """Al mostrarse, aplica HWND_TOPMOST después de que el HWND esté listo."""
        super().showEvent(event)
        QTimer.singleShot(0, self._force_topmost)

    def mousePressEvent(self, event):
        """Registra la posición inicial para arrastrar la pill (y para distinguir clic de arrastre)."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        """Mueve la pill siguiendo el cursor durante el arrastre, limitada al monitor activo.

        El clamp se calcula contra el monitor donde se encuentra la nueva posición
        propuesta. Esto permite arrastrar la pill a cualquier monitor: al cruzar el
        borde, ``screenAt(new_pos)`` devuelve el nuevo monitor y el clamp se adapta
        automáticamente. Si ``screenAt`` no puede identificar el monitor, se usa el
        monitor donde está el frame actual y, como último recurso, la pantalla primaria.
        """
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            screen = (
                QApplication.screenAt(new_pos)
                or QApplication.screenAt(self.frameGeometry().center())
                or QApplication.primaryScreen()
            )
            if screen:
                geo = screen.availableGeometry()
                x = max(geo.left(), min(new_pos.x(), geo.right() - self.width()))
                y = max(geo.top(), min(new_pos.y(), geo.bottom() - self.height()))
                new_pos.setX(x)
                new_pos.setY(y)
            self.move(new_pos)
            self._bottom_anchor_y = new_pos.y() + self.height()  # actualiza ancla tras mover
            event.accept()

    def mouseReleaseEvent(self, event):
        """Finaliza el arrastre de la pill.

        Si el desplazamiento total desde el press fue menor al umbral (clic corto,
        no arrastre) y hay una reunión activa, emite open_dashboard_requested para
        que main.py abra el dashboard en /reunion. Sin reunión activa, un clic corto
        no hace nada (comportamiento previo sin cambios).
        """
        if event.button() == Qt.MouseButton.LeftButton and self._press_pos is not None:
            release_pos = event.globalPosition().toPoint()
            moved = (release_pos - self._press_pos).manhattanLength()
            if moved < self._DRAG_THRESHOLD and self._meeting_mode:
                self.open_dashboard_requested.emit()
        self._drag_pos = None
        self._press_pos = None
