"""
Shared CircularTimerWidget — used both in the projection bar overlay
and in the fullscreen ProjectionWindow on secondary monitors.
"""

from PySide6.QtWidgets import QWidget, QSizePolicy
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QPen, QColor, QFont


class CircularTimerWidget(QWidget):
    """
    Clean circular countdown.
    White background · colored arc ring · time centered.
    No extra labels — just the ring and the digits.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._remaining_secs: int = 0
        self._total_secs: int = 1
        self._blink_on: bool = False
        self.setMinimumSize(120, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ── Public API ────────────────────────────────────────────────────────

    def update_data(self, remaining: int, total: int):
        self._remaining_secs = max(0, remaining)
        self._total_secs = max(1, total)
        self.update()

    def set_blink(self, on: bool):
        self._blink_on = on
        self.update()

    # ── Painting ──────────────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        W, H = self.width(), self.height()

        # White background
        p.fillRect(0, 0, W, H, QColor("#ffffff"))

        # Ring is perfectly centered in the full widget area
        margin = max(32, min(W, H) // 8)
        side = min(W, H) - margin * 2
        side = max(side, 80)
        cx = W / 2
        cy = H / 2
        rx = cx - side / 2
        ry = cy - side / 2
        ring_rect = QRectF(rx, ry, side, side)

        pen_w = max(8, side // 14)
        inner = ring_rect.adjusted(pen_w / 2, pen_w / 2, -pen_w / 2, -pen_w / 2)

        # ── Background ring ───────────────────────────────────────────────
        pen_bg = QPen(QColor("#e2e8f0"))
        pen_bg.setWidth(pen_w)
        pen_bg.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen_bg)
        p.drawArc(inner, 0, 360 * 16)

        # ── Arc color: blue → orange → red in last 30 s ───────────────────
        progress = self._remaining_secs / self._total_secs
        progress = max(0.0, min(1.0, progress))

        if self._blink_on:
            arc_color = QColor("#ef4444")
        elif self._remaining_secs <= 30:
            t = (30 - self._remaining_secs) / 30.0
            if t < 0.5:
                t2 = t * 2
                r = int(59  + t2 * (251 - 59))
                g = int(130 + t2 * (146 - 130))
                b = int(246 - t2 * 246)
            else:
                t2 = (t - 0.5) * 2
                r = int(251 + t2 * (239 - 251))
                g = int(146 - t2 * 146)
                b = 0
            arc_color = QColor(r, g, b)
        else:
            arc_color = QColor("#3b82f6")

        if progress > 0:
            pen_arc = QPen(arc_color)
            pen_arc.setWidth(pen_w)
            pen_arc.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen_arc)
            span = int(-progress * 360 * 16)
            p.drawArc(inner, 90 * 16, span)

        # ── Center time digits ────────────────────────────────────────────
        rem = self._remaining_secs
        h_p = rem // 3600
        m_p = (rem % 3600) // 60
        s_p = rem % 60

        if h_p > 0:
            time_str = f"{h_p:02d}:{m_p:02d}:{s_p:02d}"
            font_size = max(14, int(side * 0.14))
        else:
            time_str = f"{m_p:02d}:{s_p:02d}"
            font_size = max(20, int(side * 0.22))

        f = QFont("Segoe UI", font_size, QFont.Weight.Bold)
        p.setFont(f)
        text_color = QColor("#ef4444") if self._blink_on else QColor("#1e293b")
        p.setPen(text_color)
        p.drawText(ring_rect, Qt.AlignmentFlag.AlignCenter, time_str)
