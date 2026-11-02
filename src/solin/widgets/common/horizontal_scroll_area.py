from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QScrollArea, QWidget


class HorizontalScrollArea(QScrollArea):
    """A single horizontal strip navigable with a mouse wheel or touchpad."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        bar = self.horizontalScrollBar()
        if bar.minimum() == bar.maximum():
            event.ignore()
            return

        pixels = event.pixelDelta()
        if not pixels.isNull():
            # Qt already applies the system's natural-scrolling direction.
            # Prefer a horizontal gesture; map a vertical one to the same axis.
            delta = pixels.x() if pixels.x() else pixels.y()
            previous = bar.value()
            bar.setValue(previous - delta)
            event.setAccepted(bar.value() != previous)
            return

        angle = event.angleDelta()
        forwarded = event
        if angle.x() and abs(angle.y()) >= abs(angle.x()):
            # Qt ignores vertical-dominant diagonal input on horizontal bars.
            forwarded = QWheelEvent(
                event.position(), event.globalPosition(), pixels, QPoint(0, angle.y()),
                event.buttons(), event.modifiers(), event.phase(), event.inverted(),
                event.source(), event.pointingDevice(),
            )

        # Keep Qt's wheel-step preference, fractional-angle accumulation and
        # modifier behavior, and propagate whether the scrollbar consumed it.
        bar.event(forwarded)
        event.setAccepted(forwarded.isAccepted())
