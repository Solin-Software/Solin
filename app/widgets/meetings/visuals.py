from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ...styles.icons import (
    ICON_SEC_TREASURES as _SVG_DIAMOND,
    ICON_SEC_MINISTRY as _SVG_WHEAT,
    ICON_SEC_LIVING as _SVG_SHEEP,
)

_BG = "#0d1117"
_CARD = "#161b22"
_BORDER = "#21262d"
_MUTED = "#8b949e"
_TEXT = "#e6edf3"
_SUB = "#c9d1d9"
_ACCENT = "#388bfd"
_GREEN = "#3fb950"
_AMBER = "#d29922"
_RED = "#f85149"
_PURPLE = "#a371f7"
_GOLD = "#d29922"

_SEC_ICONS: dict[str, tuple[str, int]] = {
    "tgw": (_SVG_DIAMOND, 14),
    "ayfm": (_SVG_WHEAT, 14),
    "lac": (_SVG_SHEEP, 16),
}


def _svg_pixmap(svg_data: str, size: int, color: str) -> QPixmap:
    colored = svg_data.replace("currentColor", color)
    renderer = QSvgRenderer(colored.encode())
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pix


def _rounded_pixmap(pix: QPixmap, w: int, h: int, r: int = 10) -> QPixmap:
    scaled = pix.scaled(
        w,
        h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    if scaled.width() > w or scaled.height() > h:
        x = (scaled.width() - w) // 2
        y = (scaled.height() - h) // 2
        scaled = scaled.copy(x, y, w, h)
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, w, h, r, r)
    p.setClipPath(path)
    p.drawPixmap(0, 0, scaled)
    p.end()
    return out
