from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QPushButton

from solin.styles.icons import make_icon
from solin.styles.theme import PALETTE, qss_rgba


PROJECTION_MENU_STYLE = f"""
QMenu {{
    background: {PALETTE.surface};
    border: 1px solid {PALETTE.border};
    border-radius: 8px;
    padding: 6px 4px;
    color: {PALETTE.text_secondary};
    font-size: 12px;
}}
QMenu::item {{ padding: 6px 20px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {PALETTE.bg2}; color: {PALETTE.text_primary}; }}
QMenu::item:checked  {{ color: {PALETTE.accent}; font-weight: 600; }}
QMenu::separator     {{ height: 1px; background: {PALETTE.border}; margin: 4px 8px; }}
QMenu::indicator     {{ width: 0; }}
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
    color: str = PALETTE.text_secondary,
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
        f"QPushButton:hover{{background:{qss_rgba(PALETTE.white, 0.08)};border-radius:{radius}px;}}"
        f"QPushButton:pressed{{background:{qss_rgba(PALETTE.white, 0.13)};}}"
    )
    return btn
