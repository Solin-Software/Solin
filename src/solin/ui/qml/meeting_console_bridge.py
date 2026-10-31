"""QML bridge for the Live meeting console (hosted meetings)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication

from solin.core.hosted_meeting.service import HostedMeetingService

_ROW_ROLES = ("device", "name", "state", "connected", "live", "partHolder", "hand")


class MeetingRowsModel(QAbstractListModel):
    """One snapshot list (lobby, hands, attendees), updated in place by device.

    Replacing a QML list model rebuilds every delegate: a button being pressed, or an
    open ⋯ menu, was destroyed whenever the hall sent a new snapshot. Here unchanged
    rows keep their delegates; only changed fields notify.
    """

    countChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []
        self._roles = {Qt.ItemDataRole.UserRole + i: name for i, name in enumerate(_ROW_ROLES)}

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        name = self._roles.get(role)
        return self._rows[index.row()].get(name) if name is not None else None

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802 - Qt API
        return {role: QByteArray(name.encode()) for role, name in self._roles.items()}

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._rows)

    def replace(self, rows: list[dict[str, Any]]) -> None:
        rows = [dict(row) for row in rows if isinstance(row, dict) and row.get("device")]
        before = len(self._rows)
        wanted = [row["device"] for row in rows]
        # Drop rows that left, from the end so indexes stay valid.
        for i in range(len(self._rows) - 1, -1, -1):
            if self._rows[i]["device"] not in wanted:
                self.beginRemoveRows(QModelIndex(), i, i)
                del self._rows[i]
                self.endRemoveRows()
        for target, row in enumerate(rows):
            current = next((i for i in range(target, len(self._rows)) if self._rows[i]["device"] == row["device"]), None)
            if current is None:
                self.beginInsertRows(QModelIndex(), target, target)
                self._rows.insert(target, row)
                self.endInsertRows()
                continue
            if current != target:
                self.beginMoveRows(QModelIndex(), current, current, QModelIndex(), target)
                self._rows.insert(target, self._rows.pop(current))
                self.endMoveRows()
            if self._rows[target] != row:
                changed = [role for role, name in self._roles.items() if self._rows[target].get(name) != row.get(name)]
                self._rows[target] = row
                index = self.index(target)
                self.dataChanged.emit(index, index, changed)
        if len(self._rows) != before:
            self.countChanged.emit()


class MeetingConsoleBridge(QObject):
    infoChanged = Signal()
    snapshotChanged = Signal()
    copied = Signal(str)
    dockedChanged = Signal()
    dockRequested = Signal(bool)

    def __init__(
        self, service: HostedMeetingService, parent: QObject | None = None, *, dockable: bool = False
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._dockable = dockable
        self._docked = False
        self._info: dict[str, Any] = {}
        self._snapshot: dict[str, Any] = {}
        self._lobby = MeetingRowsModel(self)
        self._hands = MeetingRowsModel(self)
        self._attendees = MeetingRowsModel(self)
        self._on_info(service.info)
        self._on_snapshot(service.snapshot)
        service.info_changed.connect(self._on_info)
        service.snapshot_changed.connect(self._on_snapshot)

    def _on_info(self, info: dict[str, Any]) -> None:
        info = dict(info)
        if info.get("state") == "idle":
            self._on_snapshot({})
        if info != self._info:
            self._info = info
            self.infoChanged.emit()

    def _on_snapshot(self, snapshot: dict[str, Any]) -> None:
        snapshot = dict(snapshot)
        self._lobby.replace(snapshot.get("lobby") or [])
        self._hands.replace(snapshot.get("hands") or [])
        self._attendees.replace(snapshot.get("attendees") or [])
        # The lists live in the models; QML binds the rest (counts, mic lock).
        summary = {key: value for key, value in snapshot.items() if key not in ("lobby", "hands", "attendees")}
        if summary != self._snapshot:
            self._snapshot = summary
            self.snapshotChanged.emit()

    @Property("QVariantMap", notify=infoChanged)
    def info(self) -> dict[str, Any]:
        return self._info

    @Property("QVariantMap", notify=snapshotChanged)
    def snapshot(self) -> dict[str, Any]:
        return self._snapshot

    @Property(QObject, constant=True)
    def lobby(self) -> QObject:
        return self._lobby

    @Property(QObject, constant=True)
    def hands(self) -> QObject:
        return self._hands

    @Property(QObject, constant=True)
    def attendees(self) -> QObject:
        return self._attendees

    @Property(bool, constant=True)
    def available(self) -> bool:
        return self._service.available()

    @Property(bool, constant=True)
    def dockable(self) -> bool:
        return self._dockable

    @Property(bool, notify=dockedChanged)
    def docked(self) -> bool:
        return self._docked

    def set_docked(self, docked: bool) -> None:
        if docked != self._docked:
            self._docked = docked
            self.dockedChanged.emit()

    @Slot(bool)
    def requestDock(self, docked: bool) -> None:  # noqa: N802 - QML API
        self.dockRequested.emit(docked)

    @Slot()
    def newLink(self) -> None:  # noqa: N802 - QML API
        self._service.new_link()

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
