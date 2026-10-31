"""MeetingRowsModel keeps rows (and their QML delegates) across snapshots."""

from __future__ import annotations

from PySide6.QtCore import Qt

from solin.ui.qml.meeting_console_bridge import MeetingRowsModel


def rows(*devices: str, **changed) -> list[dict]:
    return [{"device": d, "name": d.upper(), "hand": changed.get(d, False)} for d in devices]


def names(model: MeetingRowsModel) -> list[str]:
    role = next(r for r, n in model.roleNames().items() if bytes(n.data()) == b"device")
    return [model.data(model.index(i), role) for i in range(model.rowCount())]


def record(model: MeetingRowsModel) -> list[tuple]:
    events: list[tuple] = []
    model.rowsInserted.connect(lambda _p, first, last: events.append(("insert", first, last)))
    model.rowsRemoved.connect(lambda _p, first, last: events.append(("remove", first, last)))
    model.rowsMoved.connect(lambda *_a: events.append(("move",)))
    model.modelReset.connect(lambda: events.append(("reset",)))
    model.dataChanged.connect(lambda top, _b, roles: events.append(("changed", top.row(), tuple(roles))))
    return events


def test_identical_snapshot_changes_nothing():
    model = MeetingRowsModel()
    model.replace(rows("a", "b"))
    events = record(model)
    model.replace(rows("a", "b"))
    assert events == []


def test_field_change_notifies_only_that_row_and_role():
    model = MeetingRowsModel()
    model.replace(rows("a", "b"))
    events = record(model)
    model.replace(rows("a", "b", b=True))
    hand = next(r for r, n in model.roleNames().items() if bytes(n.data()) == b"hand")
    assert events == [("changed", 1, (hand,))]


def test_inserts_removes_and_moves_without_reset():
    model = MeetingRowsModel()
    model.replace(rows("a", "b", "c"))
    events = record(model)
    model.replace(rows("c", "a", "d"))
    assert names(model) == ["c", "a", "d"]
    assert ("reset",) not in events
    assert ("remove", 1, 1) in events and ("insert", 2, 2) in events and ("move",) in events
    assert model.property("count") == 3


def test_rows_without_a_device_are_ignored():
    model = MeetingRowsModel()
    model.replace([{"name": "x"}, *rows("a")])
    assert names(model) == ["a"]
    assert model.data(model.index(5), Qt.ItemDataRole.UserRole) is None
