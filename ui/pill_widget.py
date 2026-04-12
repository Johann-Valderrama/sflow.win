import ctypes
import math
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QPen, QPixmap
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


class PillWidget(QWidget):
    """Minimal floating pill. Logo + bars when recording, tiny icons for status."""

    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"
    STATE_DONE = "done"
    STATE_ERROR = "error"

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
        self._bg_color_active = QColor(15, 15, 15, int(255 * PILL_OPACITY))
        self._bg_color_idle = QColor(140, 140, 140, 176)  # gray, 20% more translucent
        self._bg_color = self._bg_color_idle  # start in idle

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

        # Enforce always-on-top every 3 s (Qt's hint alone is unreliable on Windows)
        self._topmost_timer = QTimer()
        self._topmost_timer.setInterval(3000)
        self._topmost_timer.timeout.connect(self._force_topmost)
        self._topmost_timer.start()

        self._position_on_screen()

    def _position_on_screen(self):
        """Centra la pill horizontalmente en la parte inferior de la pantalla."""
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            # Bottom anchor: pill's bottom edge stays here at all times
            self._bottom_anchor_y = geo.bottom() - PILL_MARGIN_BOTTOM
            x = geo.center().x() - PILL_WIDTH_IDLE // 2
            y = self._bottom_anchor_y - int(self._current_height)
            self.move(x, y)

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

        # Border
        painter.setPen(QPen(QColor(255, 255, 255, 12), 0.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(0, 0, w, h, radius, radius)

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

        painter.end()

    def _force_topmost(self):
        """Fuerza la ventana al frente usando Win32 SetWindowPos (más confiable que Qt en Windows)."""
        HWND_TOPMOST   = -1
        SWP_NOMOVE     = 0x0002
        SWP_NOSIZE     = 0x0001
        SWP_NOACTIVATE = 0x0010
        ctypes.windll.user32.SetWindowPos(
            int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    def showEvent(self, event):
        """Al mostrarse, aplica HWND_TOPMOST después de que el HWND esté listo."""
        super().showEvent(event)
        QTimer.singleShot(0, self._force_topmost)

    def mousePressEvent(self, event):
        """Registra la posición inicial para arrastrar la pill."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        """Mueve la pill siguiendo el cursor durante el arrastre, limitada a la pantalla."""
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            screen = QApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                x = max(geo.left(), min(new_pos.x(), geo.right() - self.width()))
                y = max(geo.top(), min(new_pos.y(), geo.bottom() - self.height()))
                new_pos.setX(x)
                new_pos.setY(y)
            self.move(new_pos)
            self._bottom_anchor_y = new_pos.y() + self.height()  # track new anchor
            event.accept()

    def mouseReleaseEvent(self, event):
        """Finaliza el arrastre de la pill."""
        self._drag_pos = None
