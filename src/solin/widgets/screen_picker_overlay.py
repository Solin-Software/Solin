"""Multi-monitor picker for a target inside Zoom's share dialog."""

from __future__ import annotations

import logging
from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QWidget

from ..core.integrations.automation.screen_share import (
    automation_pointer_position,
    find_zoom_share_dialog_bounds_at_point,
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


def _united_screen_geometry(geometries: list[QRect]) -> QRect:
    if not geometries:
        return QRect()
    desktop = QRect(geometries[0])
    for geometry in geometries[1:]:
        desktop = desktop.united(geometry)
    return desktop


class ScreenPickerOverlay(QWidget):
    """Desktop-wide overlay that emits a dialog-relative Zoom share target."""

    target_picked = Signal(float, float)
    cancelled = Signal()

    def __init__(
        self,
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
        self._picked = False
        self._cancelled_emitted = False
        self._error_message = ""

        # Fade-out timer
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._start_fade_out)

        # Fade-out animation (window opacity)
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(_FADE_OUT_MS)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InQuad)
        self._fade_anim.finished.connect(self.close)

        # One logical overlay spans the complete Qt virtual desktop.
        self._setup_geometry()

    # ── Setup ─────────────────────────────────────────────────────────────

    def _setup_geometry(self):
        """Cover every connected screen, including negative desktop origins."""
        screens = QGuiApplication.screens()
        if not screens:
            return
        self.setGeometry(
            _united_screen_geometry([screen.geometry() for screen in screens])
        )

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
            self.tr("Click the Solin Media Preview tile in Zoom's share dialog"),
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
            self._error_message or self.tr("Press ESC to cancel"),
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
            global_pos = self.mapToGlobal(event.position().toPoint())
            target_x, target_y = automation_pointer_position(
                global_pos.x(),
                global_pos.y(),
            )
            bounds = find_zoom_share_dialog_bounds_at_point(
                target_x,
                target_y,
            )
            if bounds is None:
                self._error_message = self.tr(
                    "That point is not inside a visible Zoom share dialog"
                )
                self.update()
                event.accept()
                return

            left, top, width, height = bounds
            if width <= 1 or height <= 1:
                event.accept()
                return

            x_ratio = (target_x - left) / (width - 1)
            y_ratio = (target_y - top) / (height - 1)
            self._marker_pos = global_pos
            self._picked = True
            self._error_message = ""
            self.target_picked.emit(x_ratio, y_ratio)
            self.update()
            self._close_timer.start(_CLOSE_DELAY_MS)

        event.accept()

    # ── Keyboard ──────────────────────────────────────────────────────────

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        if not self._picked and not self._cancelled_emitted:
            self._cancelled_emitted = True
            self.cancelled.emit()
        super().closeEvent(event)

    # ── Fade-out ──────────────────────────────────────────────────────────

    def _start_fade_out(self):
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    # ── Public API ────────────────────────────────────────────────────────

    def show_overlay(self):
        """Show the overlay across the complete virtual desktop."""
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self.activateWindow()
