"""Sidebar navigation button."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton

from solin.styles.icons import make_icon


class SidebarButton(QPushButton):
    """Navigation button with themed SVG icon and active state."""

    def __init__(self, svg_icon: str, label: str, parent=None):
        super().__init__(parent)
        self.setObjectName("SidebarBtn")
        self._svg_icon = svg_icon
        self._label = label
        self._active = False
        self.setCheckable(False)
        self.setMinimumHeight(52)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_icon_and_text()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._update_icon_and_text()
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def _update_icon_and_text(self) -> None:
        color = "#e6edf3" if self._active else "#8b949e"
        self.setIcon(make_icon(self._svg_icon, 16, color))
        self.setIconSize(QSize(16, 16))
        self.setText(f"  {self._label}")
