"""Combine operator and Zoom hand indicators for the timer displays."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from ..core.foundation.constants import IS_DEV
from ..core.integrations.automation.settings import ZoomSettingsStore
from ..core.integrations.automation.zoom.state import (
    AudioState,
    RaisedHand,
    RaisedHandsSnapshot,
    RaisedHandsStatus,
)
from ..core.timer.hand_alert import TimerHandAlert


_DEMO_NAMES = (
    "Ana Ribeiro", "João Martins", "Beatriz Costa", "Mateus Lima", "Lúcia Ferreira",
    "Rafael Melo", "Isabela Santos", "Gabriel Rocha", "Marina Alves", "Daniel Souza",
    "Camila Dias", "Lucas Freitas", "Helena Duarte", "Tiago Nunes", "Sofia Mendes",
)


class TimerHandAlertController(QObject):
    alert_changed = Signal(object)
    controls_changed = Signal()
    demo_changed = Signal(object)

    def __init__(self, settings: ZoomSettingsStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._available = False
        self._integrated = settings.raised_hands_timer_integration_enabled()
        self._manual = False
        self._snapshot = RaisedHandsSnapshot()
        self._demo_snapshot: RaisedHandsSnapshot | None = None
        self._alert = TimerHandAlert()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def integrated(self) -> bool:
        return self._integrated

    @property
    def manual(self) -> bool:
        return self._manual

    @property
    def alert(self) -> TimerHandAlert:
        return self._alert

    @property
    def demo_available(self) -> bool:
        return IS_DEV

    @property
    def demo_snapshot(self) -> RaisedHandsSnapshot | None:
        return self._demo_snapshot

    def set_outputs(self, *, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        if not available:
            self._manual = False
        self._publish()
        self.controls_changed.emit()

    @Slot(bool)
    def set_integrated(self, enabled: bool) -> None:
        if self._available and enabled != self._integrated:
            self._settings.set_raised_hands_timer_integration_enabled(enabled)
            self._integrated = enabled
            self._publish()
            self.controls_changed.emit()

    @Slot(bool)
    def set_manual(self, enabled: bool) -> None:
        if self._available and enabled != self._manual:
            self._manual = enabled
            self._publish()
            self.controls_changed.emit()

    @Slot(bool)
    def set_demo(self, enabled: bool) -> None:
        if not self.demo_available or enabled == (self._demo_snapshot is not None):
            return
        self._demo_snapshot = (
            RaisedHandsSnapshot(
                RaisedHandsStatus.READY,
                tuple(RaisedHand(name, AudioState.UNKNOWN, None) for name in _DEMO_NAMES),
                chronological=True,
            )
            if enabled
            else None
        )
        self._publish()
        self.demo_changed.emit(self._demo_snapshot)

    @Slot(object)
    def set_zoom_snapshot(self, snapshot: RaisedHandsSnapshot) -> None:
        self._snapshot = snapshot
        self._publish()

    def _publish(self) -> None:
        names = ()
        snapshot = self._demo_snapshot or self._snapshot
        if (
            self._available
            and (self._demo_snapshot is not None or self._integrated)
            and snapshot.status is RaisedHandsStatus.READY
        ):
            names = tuple(hand.name for hand in snapshot.hands)
        alert = TimerHandAlert(names=names) if names else TimerHandAlert(manual=self._manual)
        if alert != self._alert:
            self._alert = alert
            self.alert_changed.emit(alert)
