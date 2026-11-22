"""QML presentation adapter for projected timer output."""

from __future__ import annotations

from collections.abc import Callable
import sys

from PySide6.QtCore import QObject, Property, Signal, Slot
from PySide6.QtWidgets import QApplication, QStyle

from solin.core.timer.hand_alert import TimerHandAlert
from solin.core.timer.models import ClockConfig, ClockMode, TimerSnapshot
from solin.core.timer.render import build_render_model


class ClockRenderBridge(QObject):
    """Expose one shared timer render model to projected QML surfaces."""

    modelChanged = Signal()
    handAlertChanged = Signal()

    def __init__(
        self,
        engine,
        config_provider: Callable[[], ClockConfig],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config_provider = config_provider
        self._model: dict = {}
        self._hand_alert = TimerHandAlert()
        self._reduce_motion = True
        self._engine.tick.connect(self._refresh)
        self.refresh_now()

    def _refresh(self, snapshot_data=None) -> None:
        snapshot = (
            TimerSnapshot.from_dict(snapshot_data)
            if isinstance(snapshot_data, dict)
            else self._engine.snapshot()
        )
        model = build_render_model(
            snapshot, self._config_provider(), compact_readout=bool(self._hand_alert.names)
        )
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

    @Slot(object)
    def set_hand_alert(self, alert: TimerHandAlert) -> None:
        if alert == self._hand_alert:
            return
        names_visibility_changed = bool(alert.names) != bool(self._hand_alert.names)
        if names_visibility_changed or alert.manual != self._hand_alert.manual:
            self._reduce_motion = _reduce_motion_preferred()
        self._hand_alert = alert
        if names_visibility_changed and self._config_provider().mode is ClockMode.ANALOG:
            self._refresh()
        self.handAlertChanged.emit()

    def _get_hand_alert(self) -> dict:
        if self._hand_alert.names:
            return {
                "kind": "automatic",
                "count": len(self._hand_alert.names),
                "names": list(self._hand_alert.names),
            }
        return {"kind": "manual"} if self._hand_alert.manual else {}

    def _get_reduce_motion(self) -> bool:
        return self._reduce_motion

    handAlert = Property("QVariantMap", _get_hand_alert, notify=handAlertChanged)
    reduceMotion = Property(bool, _get_reduce_motion, notify=handAlertChanged)


def _reduce_motion_preferred() -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes.wintypes import BOOL

        animations = BOOL()
        action = 0x1042  # SPI_GETCLIENTAREAANIMATION
        if ctypes.windll.user32.SystemParametersInfoW(action, 0, ctypes.byref(animations), 0):
            return not bool(animations.value)
    elif sys.platform == "darwin":
        from importlib import import_module

        try:
            workspace = import_module("AppKit").NSWorkspace.sharedWorkspace()
            return bool(workspace.accessibilityDisplayShouldReduceMotion())
        except (ImportError, AttributeError):
            pass

    if isinstance(QApplication.instance(), QApplication):
        return QApplication.style().styleHint(QStyle.StyleHint.SH_Widget_Animation_Duration) <= 0
    return True
