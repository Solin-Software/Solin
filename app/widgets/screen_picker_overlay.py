"""
ScreenPickerOverlay — fullscreen overlay for configuring Quick Share Click position.

Covers the primary monitor with a semi-transparent dark layer (like Win+Shift+S).
Shows a crosshair marker at the configured position (if any).
On click: saves the new position, shows the marker, and fades out after 1 second.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QWidget
from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint, Signal,
)
from PySide6.QtGui import (
    QGuiApplication, QPainter, QColor, QPen, QBrush, QScreen,
)

log = logging.getLogger(__name__)

# ── Visual constants ──────────────────────────────────────────────────────────
_OVERLAY_OPACITY  = 0.45           # background dimming
_RING_RADIUS      = 28             # outer crosshair ring radius (px)
_RING_WIDTH       = 2.5            # ring stroke width
_DOT_RADIUS       = 3              # center dot radius
_RING_COLOR       = QColor(255, 255, 255, 200)
_RING_GLOW_COLOR  = QColor(56, 139, 253, 120)   # #388bfd glow
_DOT_COLOR        = QColor(56, 139, 253, 255)    # #388bfd accent
_FADE_OUT_MS      = 600            # fade-out animation duration
_CLOSE_DELAY_MS   = 1000           # delay before starting fade-out after click


class ScreenPickerOverlay(QWidget):
    """
    Fullscreen overlay on the primary monitor.

    Signals:
        position_picked(int, int): emitted with (x, y) screen coords when user clicks.
    """

    position_picked = Signal(int, int)

    def __init__(
        self,
        current_x: int = -1,
        current_y: int = -1,
        parent: QWidget | None = None,
    ):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)

        # Marker state
        self._marker_pos: QPoint | None = None
        self._picked = False   # True after user clicks (prevents re-click)

        # Load existing position
        if current_x >= 0 and current_y >= 0:
            self._marker_pos = QPoint(current_x, current_y)

        # Fade-out timer
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._start_fade_out)

        # Fade-out animation (window opacity)
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(_FADE_OUT_MS)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InQuad)
        self._fade_anim.finished.connect(self.close)

        # Position on primary screen
        self._setup_geometry()

    # ── Setup ─────────────────────────────────────────────────────────────

    def _setup_geometry(self):
        """Cover the primary screen entirely."""
        screen: QScreen | None = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.geometry()
        self.setGeometry(geo)
        self._screen_geo = geo

    # ── Paint ─────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1) Semi-transparent dark background
        p.fillRect(self.rect(), QColor(0, 0, 0, int(255 * _OVERLAY_OPACITY)))

        # 2) Hint text at center-top
        p.setPen(QPen(QColor(255, 255, 255, 180)))
        font = p.font()
        font.setPixelSize(14)
        font.setWeight(font.Weight.Medium)
        p.setFont(font)
        hint_rect = self.rect().adjusted(0, 40, 0, -self.height() + 80)
        p.drawText(
            hint_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            self.tr("Click on the share target position"),
        )

        # 3) ESC hint
        font.setPixelSize(11)
        font.setWeight(font.Weight.Normal)
        p.setFont(font)
        p.setPen(QPen(QColor(255, 255, 255, 100)))
        esc_rect = self.rect().adjusted(0, 62, 0, -self.height() + 90)
        p.drawText(
            esc_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            self.tr("Press ESC to cancel"),
        )

        # 4) Draw marker if position is set
        if self._marker_pos is not None:
            self._draw_marker(p, self._marker_pos)

        p.end()

    def _draw_marker(self, p: QPainter, pos: QPoint):
        """Draws the crosshair ring + center dot at the given screen position."""
        # Convert screen coords to widget-local coords
        local = self.mapFromGlobal(pos)
        x, y = local.x(), local.y()

        # Outer glow ring
        glow_pen = QPen(_RING_GLOW_COLOR, _RING_WIDTH + 4)
        glow_pen.setStyle(Qt.PenStyle.SolidLine)
        p.setPen(glow_pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(x - _RING_RADIUS, y - _RING_RADIUS,
                       _RING_RADIUS * 2, _RING_RADIUS * 2)

        # Main ring
        ring_pen = QPen(_RING_COLOR, _RING_WIDTH)
        ring_pen.setStyle(Qt.PenStyle.SolidLine)
        p.setPen(ring_pen)
        p.drawEllipse(x - _RING_RADIUS, y - _RING_RADIUS,
                       _RING_RADIUS * 2, _RING_RADIUS * 2)

        # Crosshair lines (short ticks outside the ring)
        tick_len = 8
        outer = _RING_RADIUS + 4
        tick_pen = QPen(_RING_COLOR, 1.5)
        p.setPen(tick_pen)
        p.drawLine(x, y - outer, x, y - outer - tick_len)   # top
        p.drawLine(x, y + outer, x, y + outer + tick_len)   # bottom
        p.drawLine(x - outer, y, x - outer - tick_len, y)   # left
        p.drawLine(x + outer, y, x + outer + tick_len, y)   # right

        # Center dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_DOT_COLOR))
        p.drawEllipse(x - _DOT_RADIUS, y - _DOT_RADIUS,
                       _DOT_RADIUS * 2, _DOT_RADIUS * 2)

    # ── Mouse events ──────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if self._picked:
            return  # Already picked, ignore further clicks

        if event.button() == Qt.MouseButton.LeftButton:
            # Map widget-local click to global screen coords
            global_pos = self.mapToGlobal(event.position().toPoint())
            self._marker_pos = global_pos
            self._picked = True

            # Emit signal with screen coords
            self.position_picked.emit(global_pos.x(), global_pos.y())

            # Repaint to show marker at new position
            self.update()

            # Schedule fade-out
            self._close_timer.start(_CLOSE_DELAY_MS)

        event.accept()

    # ── Keyboard ──────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    # ── Fade-out ──────────────────────────────────────────────────────────

    def _start_fade_out(self):
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    # ── Public API ────────────────────────────────────────────────────────

    def show_overlay(self):
        """Show the overlay covering the primary screen."""
        self.setWindowOpacity(1.0)
        if sys.platform == "darwin":
            # showFullScreen() creates a dedicated macOS Space and makes the
            # windows underneath disappear. A borderless, top-level window keeps
            # the current desktop visible through the translucent overlay.
            self.show()
        else:
            self.showFullScreen()
        self.raise_()
        self.activateWindow()
