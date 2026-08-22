from __future__ import annotations

from PySide6.QtCore import QRect, QSize
from PySide6.QtWidgets import QWidget


class FlowContainer(QWidget):
    """Small wrapping container for stable, self-sized child widgets."""

    def __init__(
        self,
        *,
        horizontal_spacing: int = 6,
        vertical_spacing: int = 6,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._horizontal_spacing = horizontal_spacing
        self._vertical_spacing = vertical_spacing
        self._items: list[QWidget] = []

    def add_widget(self, widget: QWidget) -> None:
        widget.setParent(self)
        self._items.append(widget)
        # setParent() hides an existing widget. Clear that explicit hidden state
        # even when an ancestor is currently hidden so the item is visible the
        # next time the container is shown.
        widget.show()

    @property
    def widgets(self) -> tuple[QWidget, ...]:
        return tuple(self._items)

    def clear_items(self, *, delete: bool = True) -> tuple[QWidget, ...]:
        items = tuple(self._items)
        self._items.clear()
        for widget in items:
            widget.setParent(None)
            if delete:
                widget.deleteLater()
        return items

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, max(0, width), 0), dry_run=True)

    def relayout(self) -> None:
        self._layout_current_width()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(100, 32)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_current_width()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._layout_current_width()

    def _layout_current_width(self) -> None:
        if self.width() > 0:
            self._do_layout(QRect(0, 0, self.width(), self.height()), dry_run=False)

    def _do_layout(self, rect: QRect, *, dry_run: bool) -> int:
        x = rect.x()
        y = rect.y()
        row_height = 0
        for widget in self._items:
            if not dry_run and not widget.isVisible():
                continue
            item_size = widget.sizeHint().expandedTo(widget.minimumSizeHint())
            width = (
                widget.minimumWidth()
                if widget.minimumWidth() == widget.maximumWidth()
                else item_size.width()
            )
            height = (
                widget.minimumHeight()
                if widget.minimumHeight() == widget.maximumHeight()
                else item_size.height()
            )
            width = min(width, rect.width())
            if x > rect.x() and x + width > rect.right() + 1:
                x = rect.x()
                y += row_height + self._vertical_spacing
                row_height = 0
            if not dry_run:
                widget.setGeometry(x, y, width, height)
            x += width + self._horizontal_spacing
            row_height = max(row_height, height)
        return y + row_height
