from __future__ import annotations

from PySide6.QtWidgets import QWidget


__all__ = ("AspectRatioViewFrame",)


class AspectRatioViewFrame(QWidget):
    """Container that optionally forces the native webview to stay in 16:9."""

    _RATIO_W = 16
    _RATIO_H = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self._child: QWidget | None = None
        self._locked = False
        self.setStyleSheet("background:#05070a;")

    def set_child(self, child: QWidget) -> None:
        self._child = child
        child.setParent(self)
        self._apply_child_geometry()

    def set_aspect_ratio_locked(self, locked: bool) -> None:
        if self._locked == locked:
            return
        self._locked = locked
        self._apply_child_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_child_geometry()

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_child_geometry()

    def _apply_child_geometry(self) -> None:
        if not self._child:
            return

        if not self._locked:
            self._child.setGeometry(0, 0, self.width(), self.height())
            return

        avail_w = max(0, self.width())
        avail_h = max(0, self.height())
        if avail_w <= 0 or avail_h <= 0:
            self._child.setGeometry(0, 0, 0, 0)
            return

        target_h = int(avail_w * self._RATIO_H / self._RATIO_W)
        target_w = avail_w
        if target_h > avail_h:
            target_h = avail_h
            target_w = int(avail_h * self._RATIO_W / self._RATIO_H)

        x = (avail_w - target_w) // 2
        y = (avail_h - target_h) // 2
        self._child.setGeometry(x, y, target_w, target_h)
