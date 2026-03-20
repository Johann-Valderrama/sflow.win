import math
import queue
import numpy as np
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import QTimer, Qt, QRectF
from PyQt6.QtGui import QPainter, QColor, QLinearGradient
from config import NUM_BARS, VIZ_FPS, BAR_GAIN


class AudioVisualizer(QWidget):
    """Premium audio visualizer. Ultra-thin luminous bars with glow and spring physics."""

    def __init__(self, parent=None):
        """Inicializa las barras, velocidades y el timer de actualización."""
        super().__init__(parent)
        self.num_bars = NUM_BARS
        self.bar_values = [0.0] * self.num_bars
        self._velocities = [0.0] * self.num_bars
        self.audio_queue: queue.Queue | None = None

        self._timer = QTimer()
        self._timer.setInterval(1000 // VIZ_FPS)
        self._timer.timeout.connect(self._update_bars)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_audio_queue(self, q: queue.Queue):
        """Asigna la cola de audio compartida con el recorder."""
        self.audio_queue = q

    def start(self):
        """Reinicia las barras y arranca el timer de actualización."""
        self.bar_values = [0.0] * self.num_bars
        self._velocities = [0.0] * self.num_bars
        self._timer.start()

    def stop(self):
        """Detiene el timer y resetea las barras a cero."""
        self._timer.stop()
        self.bar_values = [0.0] * self.num_bars
        self._velocities = [0.0] * self.num_bars
        self.update()

    def _update_bars(self):
        """Lee chunks de audio, calcula FFT y aplica física de resorte a las barras."""
        if not self.audio_queue:
            return

        chunks = []
        while True:
            try:
                chunks.append(self.audio_queue.get_nowait())
            except queue.Empty:
                break

        targets = [0.0] * self.num_bars

        if chunks:
            latest = chunks[-1]
            chunk = latest[:, 0] if latest.ndim > 1 else latest
            chunk = chunk.astype(np.float32) / 32768.0

            # FFT-based frequency analysis for organic movement
            fft = np.abs(np.fft.rfft(chunk))
            freq_bins = np.array_split(fft[:len(fft) // 2], self.num_bars)
            for i, fb in enumerate(freq_bins):
                if len(fb) > 0:
                    targets[i] = min(float(np.mean(fb)) * BAR_GAIN * 2.0, 1.0)

        # Spring physics: smooth rise, graceful fall
        dt = 1.0 / VIZ_FPS
        stiffness = 35.0
        damping = 8.0

        for i in range(self.num_bars):
            diff = targets[i] - self.bar_values[i]
            self._velocities[i] += diff * stiffness * dt
            self._velocities[i] *= max(0, 1.0 - damping * dt)
            self.bar_values[i] += self._velocities[i] * dt

            if self.bar_values[i] < 0.005:
                self.bar_values[i] = 0.0
                self._velocities[i] = 0.0
            elif self.bar_values[i] > 1.0:
                self.bar_values[i] = 1.0

        self.update()

    def paintEvent(self, event):
        """Dibuja las barras luminosas con gradiente vertical y efecto de glow."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        if w <= 0 or h <= 0:
            painter.end()
            return

        bar_w = 1.8
        gap = 2.0
        total_w = self.num_bars * bar_w + (self.num_bars - 1) * gap
        x_off = (w - total_w) / 2.0
        min_h = 2.0
        center_idx = self.num_bars / 2.0

        painter.setPen(Qt.PenStyle.NoPen)

        for i, val in enumerate(self.bar_values):
            dist = abs(i - center_idx + 0.5) / center_idx
            # Gaussian bell-curve taper from center
            taper = math.exp(-dist * dist * 1.2)
            bar_h = max(min_h, val * h * 1.6 * taper)
            x = x_off + i * (bar_w + gap)
            cy = h / 2.0
            y = cy - bar_h / 2.0

            rect = QRectF(x, y, bar_w, bar_h)

            # Glow layer — soft halo behind each bar
            if val > 0.02:
                glow_alpha = int(val * 40 * taper)
                glow_spread = bar_w + 3.0
                glow_rect = QRectF(
                    x - (glow_spread - bar_w) / 2, y - 1,
                    glow_spread, bar_h + 2,
                )
                painter.setBrush(QColor(255, 255, 255, glow_alpha))
                painter.drawRoundedRect(glow_rect, glow_spread / 2, glow_spread / 2)

            # Main bar — vertical gradient fading at tips
            gradient = QLinearGradient(x, y, x, y + bar_h)
            peak_alpha = int((100 + val * 155) * taper)
            edge_alpha = int(peak_alpha * 0.3)
            gradient.setColorAt(0.0, QColor(255, 255, 255, edge_alpha))
            gradient.setColorAt(0.35, QColor(255, 255, 255, peak_alpha))
            gradient.setColorAt(0.65, QColor(255, 255, 255, peak_alpha))
            gradient.setColorAt(1.0, QColor(255, 255, 255, edge_alpha))

            painter.setBrush(gradient)
            painter.drawRoundedRect(rect, bar_w / 2, bar_w / 2)

        painter.end()
