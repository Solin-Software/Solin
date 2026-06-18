"""Sermon-theme projection slide widget."""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QWidget

# ── SVG background for the sermon-theme slide ────────────────────────────────
# Pixel-perfect recreation of the reference design.
# Stored as a plain str (not bytes) to allow any character; encoded on use.

SERMON_THEME_BACKGROUND_SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 576" width="100%" height="100%">
  <defs>
    <linearGradient id="mainBgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#f8fbf9"/>
      <stop offset="100%" stop-color="#f2f8f5"/>
    </linearGradient>
    <linearGradient id="topRightBgGradient" x1="100%" y1="0%" x2="50%" y2="50%">
      <stop offset="0%" stop-color="#d8e6dd" stop-opacity="0.9"/>
      <stop offset="100%" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="waveBgGradient" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#cae9d9"/>
      <stop offset="100%" stop-color="#b5dec7"/>
    </linearGradient>
    <linearGradient id="waveFgGradient" x1="0%" y1="0%" x2="100%" y2="0%">
      <stop offset="0%" stop-color="#b6e1cd"/>
      <stop offset="100%" stop-color="#a3d4bd"/>
    </linearGradient>
  </defs>
  <rect width="1024" height="576" fill="url(#mainBgGrad)"/>
  <rect width="512" height="288" x="512" y="0" fill="url(#topRightBgGradient)"/>
  <path d="M 0,0 L 450,0 C 380,120 200,100 0,160 Z"
        fill="url(#waveBgGradient)" opacity="0.7"/>
  <path d="M 0,0 L 320,0 C 250,80 150,60 0,140 Z"
        fill="url(#waveFgGradient)" opacity="0.9"/>
  <path d="M 0,130 C 40,125 80,105 110,75 C 130,55 150,25 170,10 C 190,-5 205,-10 215,-5"
        fill="none" stroke="#85c3a4" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M 0,400 C 100,400 180,470 250,576 L 0,576 Z"
        fill="#def2e6" opacity="0.8"/>
  <rect x="0" y="335" width="48" height="28" fill="#a2d8d3"/>
  <path d="M 0,265 C 35,265 48,290 48,335 L 0,335 Z" fill="#ceeadd"/>
  <path d="M 46,265 C -5,255 -5,195 0,185 C 25,195 40,230 46,265 Z" fill="#99d2ad"/>
  <path d="M 47,268 C 65,230 85,210 105,215 C 85,245 65,270 47,268 Z" fill="#99d2ad"/>
  <path d="M 52,205 C 55,165 80,155 95,163 C 75,190 65,210 52,205 Z" fill="#9ad3cd"/>
  <path d="M 48,335 C 70,300 95,280 105,285 C 85,315 65,340 48,335 Z" fill="#9ad3cd"/>
  <path d="M 0,410 C 35,410 65,395 85,370 C 95,355 105,365 95,380 C 88,390 80,380 85,370"
        fill="none" stroke="#85c3a4" stroke-width="2.5" stroke-linecap="round"/>
  <circle cx="85" cy="370" r="6.5" fill="#c7ebdb"/>
</svg>
"""


class SermonThemeProjectionWidget(QWidget):
    """Fullscreen sermon-theme slide shown on the secondary monitor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._theme_text = ""
        self._subtitle = "DISCURSO PUBLICO"
        # Encode to UTF-8 bytes here — the SVG itself is pure ASCII so this is safe
        self._renderer = QSvgRenderer(QByteArray(SERMON_THEME_BACKGROUND_SVG.encode("utf-8")))

        # ── Zoom/pan transform ─────────────────────────────────────────────
        self._zoom:   float = 1.0
        self._norm_x: float = 0.0   # offset as fraction of widget width
        self._norm_y: float = 0.0
        # Current (animated) values
        self._cur_zoom:   float = 1.0
        self._cur_norm_x: float = 0.0
        self._cur_norm_y: float = 0.0
        self._transform_timer = QTimer(self)
        self._transform_timer.setInterval(16)
        self._transform_timer.timeout.connect(self._on_transform_tick)

    def set_theme(self, text: str, subtitle: str = ""):
        self._theme_text = text
        if subtitle:
            self._subtitle = subtitle
        self.update()

    def set_image_transform(self, zoom: float, norm_x: float, norm_y: float,
                            *, animate: bool = True) -> None:
        """Set a new zoom/pan target.

        Animated by default; pass ``animate=False`` to snap instantly (used when
        replaying the transform onto a freshly created surface)."""
        self._zoom   = zoom
        self._norm_x = norm_x
        self._norm_y = norm_y
        if animate:
            if not self._transform_timer.isActive():
                self._transform_timer.start()
        else:
            self._cur_zoom   = zoom
            self._cur_norm_x = norm_x
            self._cur_norm_y = norm_y
            self._transform_timer.stop()
            self.update()

    def reset_transform(self) -> None:
        self._zoom   = self._cur_zoom   = 1.0
        self._norm_x = self._cur_norm_x = 0.0
        self._norm_y = self._cur_norm_y = 0.0
        self._transform_timer.stop()
        self.update()

    def reset_transform_instant(self) -> None:
        """Snap transform to identity immediately — no lerp animation."""
        self._zoom   = self._cur_zoom   = 1.0
        self._norm_x = self._cur_norm_x = 0.0
        self._norm_y = self._cur_norm_y = 0.0
        self._transform_timer.stop()
        self.update()

    def _on_transform_tick(self):
        speed = 0.13
        dz = self._zoom   - self._cur_zoom
        dx = self._norm_x - self._cur_norm_x
        dy = self._norm_y - self._cur_norm_y
        self._cur_zoom   += dz * speed
        self._cur_norm_x += dx * speed
        self._cur_norm_y += dy * speed
        if abs(dz) < 0.0005 and abs(dx) < 0.00005 and abs(dy) < 0.00005:
            self._cur_zoom   = self._zoom
            self._cur_norm_x = self._norm_x
            self._cur_norm_y = self._norm_y
            self._transform_timer.stop()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w = self.width()
        h = self.height()

        # ── Apply zoom/pan transform centred on the widget ─────────────────
        if abs(self._cur_zoom - 1.0) > 0.001 or abs(self._cur_norm_x) > 0.0001 or abs(self._cur_norm_y) > 0.0001:
            from PySide6.QtGui import QTransform
            cx = w / 2.0 + self._cur_norm_x * w
            cy = h / 2.0 + self._cur_norm_y * h
            t = QTransform()
            t.translate(cx, cy)
            t.scale(self._cur_zoom, self._cur_zoom)
            t.translate(-w / 2.0, -h / 2.0)
            p.setTransform(t)

        self._renderer.render(p, QRectF(0, 0, w, h))

        if not self._theme_text:
            p.end()
            return

        text_x = int(w * 0.155)
        text_w = int(w * 0.72)

        # ── Available vertical space ──────────────────────────────────────
        avail_top    = h * 0.08
        avail_bottom = h * 0.92
        avail_h      = avail_bottom - avail_top
        gap          = h * 0.05   # fixed gap between title bottom and subtitle

        # ── Find largest font size where (title + gap + subtitle) fits ────
        max_size = max(32, min(int(w * 0.075), int(h * 0.18)))
        min_size = max(14, int(h * 0.025))

        title_font = sub_font = None
        title_h = sub_h = 0

        for size in range(max_size, min_size - 1, -1):
            tf = QFont()
            tf.setFamily("Arial")
            tf.setBold(True)
            tf.setPixelSize(size)

            sub_px = max(min_size, int(size * 0.38))
            sf = QFont()
            sf.setFamily("Arial")
            sf.setPixelSize(sub_px)
            sf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.5)

            t_rect = QFontMetrics(tf).boundingRect(
                QRect(0, 0, text_w, 10000),
                Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                self._theme_text,
            )
            t_h = t_rect.height()
            s_h = QFontMetrics(sf).height()

            if t_h + gap + s_h <= avail_h:
                title_font, sub_font = tf, sf
                title_h, sub_h = t_h, s_h
                break

        # Fallback: use minimum size even if it overflows slightly
        if title_font is None:
            title_font = QFont()
            title_font.setFamily("Arial")
            title_font.setBold(True)
            title_font.setPixelSize(min_size)
            sub_px = max(min_size, int(min_size * 0.38))
            sub_font = QFont()
            sub_font.setFamily("Arial")
            sub_font.setPixelSize(sub_px)
            sub_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2.5)
            t_rect = QFontMetrics(title_font).boundingRect(
                QRect(0, 0, text_w, 10000),
                Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                self._theme_text,
            )
            title_h = t_rect.height()
            sub_h   = QFontMetrics(sub_font).height()

        # ── Vertically centre the whole block inside the available band ───
        block_h = title_h + gap + sub_h
        start_y = avail_top + (avail_h - block_h) / 2

        # Draw title (top-aligned inside its measured rect)
        p.setFont(title_font)
        p.setPen(QColor("#3A4E42"))
        p.drawText(
            QRectF(text_x, start_y, text_w, title_h + 2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            | Qt.TextFlag.TextWordWrap,
            self._theme_text,
        )

        # Draw subtitle immediately below title + gap (no fixed % position)
        sub_y = start_y + title_h + gap
        p.setFont(sub_font)
        p.setPen(QColor("#5a7a62"))
        p.drawText(
            QRectF(text_x, sub_y, text_w, sub_h + 4),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self._subtitle,
        )

        p.end()

__all__ = ["SermonThemeProjectionWidget", "SERMON_THEME_BACKGROUND_SVG"]
