from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
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

    def isSliderDown(self):
        return self._dragging

    def value(self):
        return self._value

    def reset(self):
        self._value = 0
        self._buffered = 0.0
        self._min = 0
        self._max = 0
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
        if self._max > 0 and self._value >= self._min:
            ratio = (self._value - self._min) / (self._max - self._min)
            prog_w = int(w * ratio)
            p.setBrush(QColor(100, 160, 255))
            p.drawRoundedRect(0, y, prog_w, track_h, 2, 2)
            p.setBrush(QColor(220, 230, 255))
            p.setPen(QPen(QColor(100, 160, 255), 1))
            p.drawEllipse(prog_w - 6, h // 2 - 6, 12, 12)

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
    def __init__(self, lang_manager, media_ctrl=None, parent=None):
        super().__init__("songs", lang_manager, media_ctrl, parent)
