from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from solin.styles.theme import PALETTE, current_theme_scheme


class ThemedHorizontalSlider(QWidget):
    """Painted horizontal slider used where native QSlider styling is unreliable."""

    valueChanged = Signal(int)
    sliderMoved = Signal(int)
    sliderPressed = Signal()
    sliderReleased = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        track_height: int = 4,
        handle_diameter: int = 12,
        dark_track: QColor | None = None,
        dark_handle: QColor | None = None,
    ) -> None:
        super().__init__(parent)
        self._minimum = 0
        self._maximum = 100
        self._value = 0
        self._dragging = False
        self._track_height = max(1, int(track_height))
        self._handle_diameter = max(2, int(handle_diameter))
        self._dark_track = dark_track
        self._dark_handle = dark_handle

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(max(18, self._handle_diameter + 8))

    def sizeHint(self) -> QSize:
        return QSize(96, max(18, self._handle_diameter + 8))

    def minimumSizeHint(self) -> QSize:
        return QSize(48, max(18, self._handle_diameter + 8))

    def setRange(self, minimum: int, maximum: int) -> None:
        minimum = int(minimum)
        maximum = int(maximum)
        if maximum < minimum:
            maximum = minimum
        self._minimum = minimum
        self._maximum = maximum
        self.setValue(self._value)

    def setMinimum(self, value: int) -> None:
        self.setRange(value, self._maximum)

    def setMaximum(self, value: int) -> None:
        self.setRange(self._minimum, value)

    def value(self) -> int:
        return self._value

    def setValue(self, value: int) -> None:
        self._set_value(value, emit_moved=False)

    def isSliderDown(self) -> bool:
        return self._dragging

    def apply_theme(self) -> None:
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.48)

        handle = self._handle_diameter
        radius = handle / 2.0
        track_height = self._track_height
        track_radius = max(1.0, track_height / 2.0)
        left = radius
        right = max(left, self.width() - radius)
        track_width = max(1.0, right - left)
        center_y = self.height() / 2.0
        track_rect = QRectF(left, center_y - track_height / 2.0, track_width, track_height)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._track_color())
        painter.drawRoundedRect(track_rect, track_radius, track_radius)

        progress_width = track_width * self._ratio()
        if progress_width > 0:
            progress_rect = QRectF(left, track_rect.y(), progress_width, track_height)
            painter.setBrush(self._progress_color())
            painter.drawRoundedRect(progress_rect, track_radius, track_radius)

        handle_center_x = left + progress_width
        handle_rect = QRectF(
            handle_center_x - radius,
            center_y - radius,
            handle,
            handle,
        )
        painter.setBrush(self._handle_color())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(handle_rect)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self.sliderPressed.emit()
        self._set_from_x(event.position().x(), emit_moved=True)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            super().mouseMoveEvent(event)
            return
        self._set_from_x(event.position().x(), emit_moved=True)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._set_from_x(event.position().x(), emit_moved=True)
        self._dragging = False
        self.sliderReleased.emit()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        key = event.key()
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Down):
            self._set_value(self._value - step, emit_moved=True)
            event.accept()
            return
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Up):
            self._set_value(self._value + step, emit_moved=True)
            event.accept()
            return
        if key == Qt.Key.Key_Home:
            self._set_value(self._minimum, emit_moved=True)
            event.accept()
            return
        if key == Qt.Key.Key_End:
            self._set_value(self._maximum, emit_moved=True)
            event.accept()
            return
        super().keyPressEvent(event)

    def _set_from_x(self, x: float, *, emit_moved: bool) -> None:
        handle = self._handle_diameter / 2.0
        width = max(1.0, self.width() - (handle * 2.0))
        ratio = (x - handle) / width
        span = self._maximum - self._minimum
        value = self._minimum + round(max(0.0, min(1.0, ratio)) * span)
        self._set_value(value, emit_moved=emit_moved)

    def _set_value(self, value: int, *, emit_moved: bool) -> None:
        value = max(self._minimum, min(self._maximum, int(value)))
        if value == self._value:
            return
        self._value = value
        self.update()
        if self.signalsBlocked():
            return
        self.valueChanged.emit(value)
        if emit_moved:
            self.sliderMoved.emit(value)

    def _ratio(self) -> float:
        span = self._maximum - self._minimum
        if span <= 0:
            return 0.0
        return (self._value - self._minimum) / span

    def _track_color(self) -> QColor:
        if current_theme_scheme() == "dark":
            return QColor(self._dark_track or QColor("#3d444d"))
        return QColor(PALETTE.border_muted)

    def _progress_color(self) -> QColor:
        if current_theme_scheme() == "dark":
            return QColor("#58a6ff")
        return QColor(PALETTE.accent)

    def _handle_color(self) -> QColor:
        if current_theme_scheme() == "dark":
            return QColor(self._dark_handle or QColor("#c9d1d9"))
        return QColor(PALETTE.accent)
