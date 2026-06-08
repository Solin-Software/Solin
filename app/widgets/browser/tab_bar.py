from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTabBar, QToolButton


class _TabBar(QTabBar):
    """QTabBar that scrolls the tab row with the mouse wheel without switching tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        self.setDrawBase(False)
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setElideMode(Qt.TextElideMode.ElideRight)

    def _scroll_btns(self):
        buttons = [child for child in self.children() if isinstance(child, QToolButton)]
        if len(buttons) < 2:
            return None, None
        buttons.sort(key=lambda button: button.x())
        return buttons[0], buttons[-1]

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            delta = -event.angleDelta().x()
        left, right = self._scroll_btns()
        if delta > 0 and left and left.isVisible() and left.isEnabled():
            left.click()
        elif delta < 0 and right and right.isVisible() and right.isEnabled():
            right.click()
        event.accept()
