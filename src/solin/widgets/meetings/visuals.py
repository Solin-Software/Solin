from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap

from ...styles.icons import (
    ICON_SEC_TREASURES as _SVG_DIAMOND,
    ICON_SEC_MINISTRY as _SVG_WHEAT,
    ICON_SEC_LIVING as _SVG_SHEEP,
)

MEETING_BG = "#0d1117"
MEETING_CARD = "#161b22"
MEETING_BORDER = "#21262d"
MEETING_MUTED = "#8b949e"
MEETING_TEXT = "#e6edf3"
MEETING_SUBTLE_TEXT = "#c9d1d9"
MEETING_ACCENT = "#388bfd"
MEETING_SUCCESS = "#3fb950"
MEETING_WARNING = "#d29922"
MEETING_DANGER = "#f85149"
MEETING_PURPLE = "#a371f7"
MEETING_GOLD = "#d29922"

MEETING_SECTION_ICONS: dict[str, tuple[str, int]] = {
    "tgw": (_SVG_DIAMOND, 14),
    "ayfm": (_SVG_WHEAT, 14),
    "lac": (_SVG_SHEEP, 16),
}


def rounded_meeting_pixmap(pix: QPixmap, w: int, h: int, r: int = 10) -> QPixmap:
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
