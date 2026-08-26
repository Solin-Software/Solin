import math

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from solin.styles.theme import (
    PALETTE,
    current_theme_scheme,
    slider_handle_border,
    slider_handle_fill,
)
from solin.ui.themed_tooltip import install_themed_tooltip


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
        install_themed_tooltip(self)

    def setRange(self, min_val, max_val):  # noqa: N802
        if (min_val, max_val) == (self._min, self._max):
            return
        self._min = min_val
        self._max = max_val
        self._value = max(self._min, min(self._max, self._value))
        self.update()

    def setValue(self, value):  # noqa: N802
        if self._dragging:
            return
        normalized = max(self._min, min(self._max, value))
        if normalized == self._value:
            return
        previous_width = self._progress_width(self._value)
        self._value = normalized
        if self._progress_width(normalized) != previous_width:
            self.update()

    def setBufferedRatio(self, ratio):  # noqa: N802
        normalized = max(0.0, min(1.0, ratio))
        if normalized == self._buffered:
            return
        previous_width = self._buffered_width(self._buffered)
        self._buffered = normalized
        if self._buffered_width(normalized) != previous_width:
            self.update()

    def setReconnectActive(self, active):  # noqa: N802
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

    def isSliderDown(self):  # noqa: N802
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

    def paintEvent(self, _event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.52)
        width, height = self.width(), self.height()
        track_height = 4
        y = (height - track_height) // 2
        painter.setPen(Qt.PenStyle.NoPen)
        dark = current_theme_scheme() == "dark"
        painter.setBrush(QColor(60, 60, 65) if dark else QColor(PALETTE.border_muted))
        painter.drawRoundedRect(0, y, width, track_height, 2, 2)
        if self._buffered > 0 and self._max > 0:
            painter.setBrush(QColor(110, 110, 120) if dark else QColor(PALETTE.border_strong))
            painter.drawRoundedRect(0, y, int(width * self._buffered), track_height, 2, 2)
        progress_width = self._progress_width(self._value)
        if progress_width is not None:
            painter.setBrush(QColor(100, 160, 255) if dark else QColor(PALETTE.accent))
            painter.drawRoundedRect(0, y, progress_width, track_height, 2, 2)

        if self._reconnect_active:
            self._paint_reconnect_overlay(painter, width, y, track_height)

        if progress_width is not None and self._should_draw_handle():
            if dark:
                painter.setBrush(QColor(220, 230, 255))
                painter.setPen(QPen(QColor(100, 160, 255), 1))
            else:
                painter.setBrush(QColor(slider_handle_fill()))
                painter.setPen(QPen(QColor(slider_handle_border()), 1))
            painter.drawEllipse(progress_width - 6, height // 2 - 6, 12, 12)

    def _should_draw_handle(self) -> bool:
        return self.isEnabled()

    def _progress_width(self, value: int) -> int | None:
        span = self._max - self._min
        if span <= 0 or value < self._min:
            return None
        return int(self.width() * ((value - self._min) / span))

    def _buffered_width(self, ratio: float) -> int:
        return int(self.width() * ratio)

    def changeEvent(self, event):  # noqa: N802
        if event.type() == QEvent.Type.EnabledChange:
            if not self.isEnabled():
                self._dragging = False
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if self.isEnabled()
                else Qt.CursorShape.ArrowCursor
            )
            self.update()
        super().changeEvent(event)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if self._reconnect_active:
            self._reconnect_timer.start()

    def hideEvent(self, event):  # noqa: N802
        super().hideEvent(event)
        self._reconnect_timer.stop()

    def _advance_reconnect_animation(self):
        self._reconnect_phase = (self._reconnect_phase + 0.024) % 1.0
        self.update()

    def _paint_reconnect_overlay(self, painter, width, y, track_height):
        pulse = 0.35 + 0.25 * (math.sin(self._reconnect_phase * math.tau) + 1.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(148, 163, 184, int(28 + 36 * pulse)))
        painter.drawRoundedRect(0, y - 1, width, track_height + 2, 3, 3)

        span = max(28, int(width * 0.22))
        x = int((width + span) * self._reconnect_phase) - span
        sheen = QLinearGradient(x, 0, x + span, 0)
        sheen.setColorAt(0.0, QColor(226, 232, 240, 0))
        sheen.setColorAt(0.5, QColor(226, 232, 240, 110))
        sheen.setColorAt(1.0, QColor(226, 232, 240, 0))
        painter.setBrush(sheen)
        painter.drawRoundedRect(0, y - 1, width, track_height + 2, 3, 3)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._seek_to(event.position().x())

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._dragging:
            self._seek_to(event.position().x())

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._seek_to(event.position().x())

    def _seek_to(self, pixels):
        if self._max <= 0:
            return
        ratio = max(0.0, min(1.0, pixels / self.width()))
        value = int(self._min + ratio * (self._max - self._min))
        previous_width = self._progress_width(self._value)
        self._value = value
        if self._progress_width(value) != previous_width:
            self.update()
        self.sliderMoved.emit(self._value)
