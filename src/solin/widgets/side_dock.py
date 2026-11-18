"""A strip docked at the right of the main window whose width the operator drags."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget


class _Grip(QWidget):
    """The left edge of the dock: drag it to resize."""

    def __init__(self, dock: ResizableSideDock) -> None:
        super().__init__(dock)
        self._dock = dock
        self._start_x = 0.0
        self._start_width = 0
        self.setObjectName("SideDockGrip")
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip(self.tr("Drag to resize"))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_x = event.globalPosition().x()
            self._start_width = self._dock.width()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            # The dock grows to the left: dragging left widens it.
            self._dock.set_dock_width(round(self._start_width - (event.globalPosition().x() - self._start_x)))
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dock.width_committed.emit(self._dock.width())
            event.accept()


class ResizableSideDock(QWidget):
    """Holds one panel (a vertical layout) with a drag handle on its left edge.

    ``width_committed`` fires when a drag ends, with the new width, so it can be saved.
    """

    width_committed = Signal(int)
    GRIP_WIDTH = 6

    def __init__(self, width: int, *, minimum: int = 280, maximum_fraction: float = 0.6,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._minimum = minimum
        self._maximum_fraction = maximum_fraction
        layout = QVBoxLayout(self)
        # The grip owns the left margin: the panel is often a native window container,
        # which would paint over (and take the mouse from) a grip laid on top of it.
        layout.setContentsMargins(self.GRIP_WIDTH, 0, 0, 0)
        layout.setSpacing(0)
        self._grip = _Grip(self)
        self.set_dock_width(width)
        # The window it was made for: a layout may reparent the dock, but this one's resizes still count.
        self._window = parent
        if parent is not None:
            parent.installEventFilter(self)

    def maximum_width(self) -> int:
        window = self.parentWidget()
        available = window.width() if window is not None else 0
        return max(self._minimum, int(available * self._maximum_fraction)) if available else 10_000

    def set_dock_width(self, width: int) -> None:
        self.setFixedWidth(max(self._minimum, min(int(width), self.maximum_width())))

    def resizeEvent(self, event) -> None:  # noqa: ANN001 - Qt override
        super().resizeEvent(event)
        self._grip.setGeometry(0, 0, self.GRIP_WIDTH, self.height())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # A smaller window must not leave the dock wider than it may be.
        if watched is getattr(self, "_window", None) and event.type() == QEvent.Type.Resize:
            self.set_dock_width(self.width())
        return False
