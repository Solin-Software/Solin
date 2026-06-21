from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton

from solin.styles.icons import make_icon


PROJECTION_MENU_STYLE = """
QMenu {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 6px 4px;
    color: #c9d1d9;
    font-size: 12px;
}
QMenu::item { padding: 6px 20px 6px 12px; border-radius: 4px; }
QMenu::item:selected { background: #21262d; color: #e6edf3; }
QMenu::item:checked  { color: #388bfd; font-weight: 600; }
QMenu::separator     { height: 1px; background: #30363d; margin: 4px 8px; }
QMenu::indicator     { width: 0; }
"""

SPEED_CHOICES = (
    ("0.5x", 0.5),
    ("0.75x", 0.75),
    ("1x", 1.0),
    ("1.25x", 1.25),
    ("1.5x", 1.5),
    ("2x", 2.0),
)


def icon_button(
    svg: str,
    size: int = 30,
    icon_px: int = 15,
    color: str = "#c9d1d9",
    tooltip: str = "",
) -> QPushButton:
    btn = QPushButton()
    btn.setFixedSize(size, size)
    btn.setIcon(make_icon(svg, icon_px, color))
    btn.setIconSize(QSize(icon_px, icon_px))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        btn.setToolTip(tooltip)
    radius = size // 2
    btn.setStyleSheet(
        f"QPushButton{{border:none;border-radius:{radius}px;"
        "background:transparent;padding:0;}"
        f"QPushButton:hover{{background:rgba(255,255,255,0.08);border-radius:{radius}px;}}"
        "QPushButton:pressed{background:rgba(255,255,255,0.13);}"
    )
    return btn
