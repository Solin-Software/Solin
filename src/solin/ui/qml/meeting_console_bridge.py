"""QML bridge for the Live meeting console (hosted meetings)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot
from PySide6.QtGui import QGuiApplication

from solin.core.hosted_meeting.service import HostedMeetingService


class MeetingConsoleBridge(QObject):
    infoChanged = Signal()
    snapshotChanged = Signal()
    copied = Signal(str)

    def __init__(self, service: HostedMeetingService, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._service = service
        self._info: dict[str, Any] = service.info
        self._snapshot: dict[str, Any] = service.snapshot
        service.info_changed.connect(self._on_info)
        service.snapshot_changed.connect(self._on_snapshot)

    def _on_info(self, info: dict[str, Any]) -> None:
        self._info = dict(info)
        if info.get("state") == "idle":
            self._snapshot = {}
            self.snapshotChanged.emit()
        self.infoChanged.emit()

    def _on_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = dict(snapshot)
        self.snapshotChanged.emit()

    @Property("QVariantMap", notify=infoChanged)
    def info(self) -> dict[str, Any]:
        return self._info

    @Property("QVariantMap", notify=snapshotChanged)
    def snapshot(self) -> dict[str, Any]:
        return self._snapshot

    @Property(bool, constant=True)
    def available(self) -> bool:
        return self._service.available()

    @Slot()
    def start(self) -> None:
        self._service.start_meeting()

    @Slot()
    def end(self) -> None:
        self._service.end_meeting()

    @Slot(str)
    def admit(self, device: str) -> None:
        self._service.admit(device)

    @Slot()
    def admitAll(self) -> None:  # noqa: N802 - QML API
        self._service.admit_all()

    @Slot(str)
    def callOn(self, device: str) -> None:  # noqa: N802 - QML API
        self._service.call_on(device)

    @Slot(str)
    def done(self, device: str) -> None:
        self._service.done(device)

    @Slot(str)
    def lowerHand(self, device: str) -> None:  # noqa: N802 - QML API
        self._service.lower_hand(device)

    @Slot(str)
    def remove(self, device: str) -> None:
        self._service.remove(device)

    @Slot(str, str)
    def rename(self, device: str, name: str) -> None:
        self._service.rename(device, name)

    @Slot(str, bool)
    def setPartHolder(self, device: str, value: bool) -> None:  # noqa: N802 - QML API
        self._service.set_part_holder(device, value)

    @Slot(bool)
    def lockMics(self, locked: bool) -> None:  # noqa: N802 - QML API
        self._service.lock_mics(locked)

    @Slot()
    def copyAttendance(self) -> None:  # noqa: N802 - QML API
        self.copy(self._service.attendance_text())

    @Slot(str)
    def copy(self, text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None and text:
            clipboard.setText(text)
            self.copied.emit(text)
