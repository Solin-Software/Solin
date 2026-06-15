"""
timer_window.py — Solin
=======================
Fullscreen clock/timer output window shown on a monitor reserved for the timer.

It is the timer-side counterpart of :class:`solin.projection.window.ProjectionWindow`
(media) and mirrors the same lifecycle: frameless always-on-top fullscreen on a
specific ``QScreen``, fade-in, refit on resolution change, exclusion from Windows
Aero Peek, and a fade-out close. The visible content is ``ClockFace.qml`` driven
by a shared :class:`ClockRenderBridge`.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve, QObject, QPropertyAnimation, QTimer, Property, Signal, Slot, Qt
)
from PySide6.QtGui import QColor, QSurfaceFormat
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..core.ui.fonts import timer_digit_font_family
from ..core.timer.models import TimerSnapshot
from ..core.timer.render import build_render_model
from solin.ui.qml.loader import load_qml_type
from .window import _exclude_from_aero_peek


# ── Render bridge ─────────────────────────────────────────────────────────────

class ClockRenderBridge(QObject):
    """Exposes the live render model to ClockFace.qml.

    One bridge feeds *every* timer window (they all show the same clock), so the
    engine is read once per tick and fanned out to all surfaces — mirroring the
    single-decoder idle-media pattern on the media side.
    """

    modelChanged = Signal()

    def __init__(self, engine, config_provider, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config_provider = config_provider
        self._model: dict = {}
        self._engine.tick.connect(self._refresh)
        self.refresh_now()

    def _refresh(self, _snapshot_dict=None) -> None:
        config = self._config_provider()
        snapshot = (
            TimerSnapshot.from_dict(_snapshot_dict)
            if isinstance(_snapshot_dict, dict)
            else self._engine.snapshot()
        )
        model = build_render_model(snapshot, config)
        if model == self._model:
            return
        self._model = model
        self.modelChanged.emit()

    @Slot()
    def refresh_now(self) -> None:
        self._refresh()

    def _get_model(self) -> dict:
        return self._model

    model = Property("QVariant", _get_model, notify=modelChanged)


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

        self._qml = QQuickWidget()
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        self._qml.setFormat(fmt)
        self._qml.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self._qml.setClearColor(QColor("#000000"))
        self._qml.rootContext().setContextProperty(
            "timerDigitFontFamily",
            timer_digit_font_family(),
        )
        self._qml.rootContext().setContextProperty("clock", bridge)
        load_qml_type(self._qml, "ClockFace")
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
        _exclude_from_aero_peek(int(self.winId()))

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
