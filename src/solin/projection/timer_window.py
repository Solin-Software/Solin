"""
timer_window.py — Solin
=======================
Fullscreen clock/timer output window shown on a monitor reserved for the timer.

It is the timer-side counterpart of :class:`solin.projection.window.ProjectionWindow`
(media) and mirrors the same lifecycle: frameless always-on-top fullscreen on a
specific ``QScreen``, fade-in, refit on resolution change, exclusion from Windows
Aero Peek, and a fade-out close. The visible content is ``ClockFace.qml`` driven
by a shared :class:`solin.ui.qml.timer_output.ClockRenderBridge`.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QPropertyAnimation, QTimer, Slot, Qt
)
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..ui.fonts import timer_digit_font_family
from solin.ui.qml.host import configure_qml_host
from solin.ui.qml.timer_output import ClockRenderBridge
from .window import exclude_from_aero_peek


# ── Output window ─────────────────────────────────────────────────────────────

class TimerOutputWindow(QWidget):
    """Fullscreen QML clock on one secondary monitor."""

    # Windows currently fading out. The controller drops its reference the moment
    # a window leaves the reserved set, and tearing the window down re-enumerates
    # the monitors — which severs the QScreen.geometryChanged connection that
    # would otherwise keep the window alive. Without an explicit hold the widget
    # is garbage-collected mid-animation and vanishes instantly instead of
    # fading. This class-level set keeps it alive until the fade finishes.
    _fading: set = set()

    def __init__(self, screen, bridge: ClockRenderBridge, monitor_index: int = 1):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.monitor_index = monitor_index
        self._bridge = bridge

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet("background-color: black;")

        self.setScreen(screen)
        self.move(screen.geometry().topLeft())
        self.resize(screen.geometry().size())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._qml = QQuickWidget(self)
        configure_qml_host(
            self._qml,
            type_name="ClockFace",
            clear_color="#000000",
            context_properties={
                "timerDigitFontFamily": timer_digit_font_family(),
                "clock": bridge,
            },
        )
        layout.addWidget(self._qml)
        self.setLayout(layout)

        # Fade-in on open (same cadence/curve as ProjectionWindow).
        self.setWindowOpacity(0.0)
        self.showFullScreen()
        self._open_anim = QPropertyAnimation(self, b"windowOpacity")
        self._open_anim.setDuration(480)
        self._open_anim.setStartValue(0.0)
        self._open_anim.setEndValue(1.0)
        self._open_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        QTimer.singleShot(60, self._open_anim.start)

        screen.geometryChanged.connect(self._on_screen_geometry_changed)
        exclude_from_aero_peek(int(self.winId()))

    @Slot()
    def _on_screen_geometry_changed(self):
        QTimer.singleShot(300, self.refit_to_screen)

    def refit_to_screen(self, screen=None):
        if screen is not None:
            self.setScreen(screen)
        target = self.screen()
        if target is None:
            return
        geo = target.geometry()
        if (self.isFullScreen()
                and self.pos() == geo.topLeft()
                and self.size() == geo.size()):
            return
        saved_opacity = self.windowOpacity()
        self.setWindowOpacity(0.0)
        self.showNormal()
        self.move(geo.topLeft())
        self.resize(geo.size())
        self.showFullScreen()
        self.setWindowOpacity(saved_opacity)
        self._qml.update()

    def fade_out_and_close(self):
        # Stop the open animation first — both animate windowOpacity, and a still
        # running fade-in would fight the fade-out and never let it finish.
        self._open_anim.stop()
        # Hold a strong reference for the duration of the fade (see _fading).
        TimerOutputWindow._fading.add(self)
        self._close_anim = QPropertyAnimation(self, b"windowOpacity")
        self._close_anim.setDuration(380)
        self._close_anim.setStartValue(self.windowOpacity())
        self._close_anim.setEndValue(0.0)
        self._close_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._close_anim.finished.connect(self._finish_fade_out)
        self._close_anim.start()

    def _finish_fade_out(self):
        self.close()
        TimerOutputWindow._fading.discard(self)
