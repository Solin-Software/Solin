import math

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .media_library_widget import MediaLibraryWidget


class BufferedSlider(QWidget):
    sliderMoved = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._min = 0
        self._max = 0
        self._value = 0
        self._buffered = 0.0
        self._dragging = False
        self._reconnect_active = False
        self._reconnect_phase = 0.0
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setInterval(45)
        self._reconnect_timer.timeout.connect(self._advance_reconnect_animation)
        self.setFixedHeight(20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def setRange(self, min_val, max_val):
        self._min = min_val
        self._max = max_val
        self.update()

    def setValue(self, value):
        if not self._dragging:
            self._value = max(self._min, min(self._max, value))
            self.update()

    def setBufferedRatio(self, ratio):
        self._buffered = max(0.0, min(1.0, ratio))
        self.update()

    def setReconnectActive(self, active):
        active = bool(active)
        if self._reconnect_active == active:
            return
        self._reconnect_active = active
        if active and self.isVisible():
            self._reconnect_timer.start()
        else:
            self._reconnect_timer.stop()
            self._reconnect_phase = 0.0
        self.update()

    def isSliderDown(self):
        return self._dragging

    def value(self):
        return self._value

    def reset(self):
        self._value = 0
        self._buffered = 0.0
        self._min = 0
        self._max = 0
        self.setReconnectActive(False)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        track_h = 4
        y = (h - track_h) // 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(60, 60, 65))
        p.drawRoundedRect(0, y, w, track_h, 2, 2)
        if self._buffered > 0 and self._max > 0:
            p.setBrush(QColor(110, 110, 120))
            p.drawRoundedRect(0, y, int(w * self._buffered), track_h, 2, 2)
        prog_w = None
        if self._max > 0 and self._value >= self._min:
            ratio = (self._value - self._min) / (self._max - self._min)
            prog_w = int(w * ratio)
            p.setBrush(QColor(100, 160, 255))
            p.drawRoundedRect(0, y, prog_w, track_h, 2, 2)

        if self._reconnect_active:
            self._paint_reconnect_overlay(p, w, y, track_h)

        if prog_w is not None:
            p.setBrush(QColor(220, 230, 255))
            p.setPen(QPen(QColor(100, 160, 255), 1))
            p.drawEllipse(prog_w - 6, h // 2 - 6, 12, 12)

    def showEvent(self, event):
        super().showEvent(event)
        if self._reconnect_active:
            self._reconnect_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._reconnect_timer.stop()

    def _advance_reconnect_animation(self):
        self._reconnect_phase = (self._reconnect_phase + 0.024) % 1.0
        self.update()

    def _paint_reconnect_overlay(self, painter, width, y, track_h):
        pulse = 0.35 + 0.25 * (
            math.sin(self._reconnect_phase * math.tau) + 1.0
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(148, 163, 184, int(28 + 36 * pulse)))
        painter.drawRoundedRect(0, y - 1, width, track_h + 2, 3, 3)

        span = max(28, int(width * 0.22))
        x = int((width + span) * self._reconnect_phase) - span
        sheen = QLinearGradient(x, 0, x + span, 0)
        sheen.setColorAt(0.0, QColor(226, 232, 240, 0))
        sheen.setColorAt(0.5, QColor(226, 232, 240, 110))
        sheen.setColorAt(1.0, QColor(226, 232, 240, 0))
        painter.setBrush(sheen)
        painter.drawRoundedRect(0, y - 1, width, track_h + 2, 3, 3)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._seek_to(event.position().x())

    def mouseMoveEvent(self, event):
        if self._dragging:
            self._seek_to(event.position().x())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._seek_to(event.position().x())

    def _seek_to(self, px):
        if self._max <= 0:
            return
        ratio = max(0.0, min(1.0, px / self.width()))
        self._value = int(self._min + ratio * (self._max - self._min))
        self.update()
        self.sliderMoved.emit(self._value)


class SongsWidget(MediaLibraryWidget):
    def __init__(
        self,
        lang_manager,
        cache_manager,
        songs_store,
        jw_cache_dir,
        media_ctrl=None,
        parent=None,
    ):
        super().__init__(
            "songs",
            lang_manager,
            cache_manager,
            media_ctrl,
            songs_store=songs_store,
            jw_cache_dir=jw_cache_dir,
            parent=parent,
        )
