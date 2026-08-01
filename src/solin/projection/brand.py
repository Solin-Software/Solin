"""Shared JW brand mark.

Single source of truth for the "JW" monogram badge, used both as the bottom-right
badge on the yeartext screen and — centred — as the virtual camera's default idle
("standby") screen, so the two never drift apart.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

#: The "JW" monogram (black glyph); rendered on the dark badge background below.
JW_LOGO_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 151 153">
  <path fill="black" d="M47.34 87c.22-13.47-.15-26.95.16-40.43 2.33 0 4.66 0 6.99-.01.25 13.48.1 26.97.08 40.46.09 6.42-3.16 13.91-9.88 15.63-6.87 1.58-14.59.65-20.24-3.8 1.03-1.94 2.09-3.86 3.14-5.78 3.76 2.25 7.87 4.83 12.46 4.07 4.68-.73 7.34-5.74 7.29-10.14m14.14-40.42c2.57.01 5.15.01 7.72.05 1.74 9.51 4.19 18.88 5.95 28.39.93 4.65 1.7 9.36 3.24 13.86 4.04-13.41 8.11-26.81 12.45-40.12 1.83-.06 3.66-.11 5.5-.15 3.19 7.38 4.6 15.36 7.14 22.97 1.89 5.54 3.1 11.28 4.95 16.84 1.12-1.86 1.63-3.97 2.02-6.09 2.35-11.94 5.51-23.7 7.76-35.66 2.51-.08 5.02-.08 7.53-.11-4.7 18.69-9.12 37.44-13.86 56.11-2.11.01-4.22 0-6.33 0-4.19-13.92-7.88-27.99-11.96-41.95-4.46 13.96-8.67 28.01-13.2 41.94-1.99.02-3.97.04-5.95.05-4.6-18.64-8.58-37.43-12.96-56.13"/>
</svg>"""

#: The badge background (dark grey square behind the glyph).
BADGE_BACKGROUND = QColor(51, 51, 51)

#: Idle-screen background (matches the projection Display's black letterbox).
IDLE_BACKGROUND = QColor(0, 0, 0)

#: Centred idle badge size as a fraction of the canvas' shorter side.
_IDLE_BADGE_FRACTION = 0.24


def render_jw_badge(size: int) -> QPixmap:
    """The dark JW badge (as shown bottom-right on the yeartext screen), ``size``²."""
    size = max(1, int(size))
    pixmap = QPixmap(size, size)
    pixmap.fill(BADGE_BACKGROUND)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pad = int(size * -0.08)  # slight bleed, matching the yeartext badge
    QSvgRenderer(QByteArray(JW_LOGO_SVG)).render(
        painter, QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    )
    painter.end()
    return pixmap


def render_idle_logo(
    width: int, height: int, *, background: QColor | None = None
) -> QImage:
    """A full-canvas idle screen: the JW badge centred on ``background``.

    Used as the virtual camera's default/standby screen — branded, not black —
    so a videoconference always sees the logo when nothing is being projected.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(background if background is not None else IDLE_BACKGROUND)
    size = int(min(width, height) * _IDLE_BADGE_FRACTION)
    if size > 0:
        badge = render_jw_badge(size)
        painter = QPainter(image)
        painter.drawPixmap((width - size) // 2, (height - size) // 2, badge)
        painter.end()
    return image
