"""The Live meeting console loads and renders live meeting state without QML errors."""

from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, QObject, QtMsgType, Signal, qInstallMessageHandler
from PySide6.QtQuick import QQuickItem
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from solin.widgets.meeting_console_window import MeetingConsoleWindow


class FakeService(QObject):
    info_changed = Signal(object)
    snapshot_changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.info = {"state": "idle", "warnings": []}
        self.snapshot: dict = {}
        self.calls: list[tuple] = []

    @staticmethod
    def available() -> bool:
        return True

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args: self.calls.append((name, *args))

    def attendance_text(self) -> str:
        return "1\n19:30\tAna"


def _pump(seconds: float = 0.3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def test_console_renders_live_state(qt_object_owner=None):
    problems: list[str] = []

    def handler(kind, _context, message):
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg) and "qml" in message.lower():
            problems.append(message)

    previous = qInstallMessageHandler(handler)
    service = FakeService()
    window = MeetingConsoleWindow(service)
    try:
        window.show()
        _pump(1.0)
        quick = window.view
        assert quick.errors() == [], quick.errors()
        assert isinstance(quick.rootObject(), QQuickItem)
        live = {"state": "live", "invitationUrl": "https://hall.hub/j/x", "stageUrl": "https://hall.hub/stage/x#k",
                "output": "live", "tunnel": "online", "galene": "running", "commenterAudio": "running",
                "warnings": ["Example warning"], "error": ""}
        row = {"device": "d1", "name": "Ana", "state": "admitted", "connected": True, "live": False,
               "partHolder": False, "hand": True}
        service.info_changed.emit(live)
        service.snapshot_changed.emit({"micsLocked": True, "lobby": [dict(row, device="d2", name="Bia", state="lobby")],
                                       "attendees": [row], "hands": [row], "connected": 1, "attendance": 1,
                                       "live": ""})
        _pump(0.6)
        assert quick.errors() == []
        window.bridge.admitAll()
        window.bridge.callOn("d1")
        window.bridge.copyAttendance()
        assert ("admit_all",) in service.calls and ("call_on", "d1") in service.calls
    finally:
        qInstallMessageHandler(previous)
        window.close()
        window.deleteLater()
        _pump(0.1)
    assert problems == [], problems


def _attendee_delegates(window: MeetingConsoleWindow) -> dict[str, QQuickItem]:
    """The attendee rows' ⋯-menu buttons by attendee name (one per live delegate)."""
    found: dict[str, QQuickItem] = {}

    def visit(item: QQuickItem) -> None:
        children = item.childItems()
        if item.property("text") == "⋯":
            label = next((str(child.property("text")) for child in item.parentItem().childItems()
                          if str(child.property("text") or "").startswith(("Ana", "Bia", "Caio"))), "")
            if label:
                found[label.split()[0]] = item
        for child in children:
            visit(child)

    visit(window.view.rootObject())
    return found


def test_snapshots_update_rows_in_place():
    """A new snapshot must not rebuild rows: a pressed button or an open menu survives."""
    service = FakeService()
    window = MeetingConsoleWindow(service)
    try:
        window.show()
        _pump(1.0)
        service.info_changed.emit({"state": "live", "warnings": []})

        def row(device, name, **extra):
            return {"device": device, "name": name, "state": "admitted", "connected": True, "live": False,
                    "partHolder": False, "hand": False, **extra}

        service.snapshot_changed.emit({"attendees": [row("d1", "Ana"), row("d2", "Bia")], "lobby": [], "hands": []})
        _pump(0.5)
        before = _attendee_delegates(window)
        assert set(before) == {"Ana", "Bia"}, before
        # Ana raises her hand, Caio joins: Ana's and Bia's rows are the same objects.
        service.snapshot_changed.emit({"attendees": [row("d1", "Ana", hand=True), row("d2", "Bia"), row("d3", "Caio")],
                                       "lobby": [], "hands": [row("d1", "Ana", hand=True)]})
        _pump(0.5)
        after = _attendee_delegates(window)
        assert set(after) == {"Ana", "Bia", "Caio"}, after
        assert after["Ana"] is before["Ana"] and after["Bia"] is before["Bia"]
        assert window.bridge.attendees.property("count") == 3
        assert window.bridge.hands.property("count") == 1
    finally:
        window.close()
        window.deleteLater()
        _pump(0.1)


def quick_container(window: MeetingConsoleWindow) -> QWidget:
    return window._body


def _live_row(device, name):
    return {"device": device, "name": name, "state": "admitted", "connected": True, "live": False,
            "partHolder": False, "hand": False}


def test_docks_into_a_thin_vertical_panel_and_back():
    host = QWidget()
    host.resize(1000, 700)
    outer = QHBoxLayout(host)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(QWidget(host), 1)
    strip = QWidget(host)
    strip.setFixedWidth(340)
    QVBoxLayout(strip).setContentsMargins(0, 0, 0, 0)
    strip.hide()
    outer.addWidget(strip)
    service = FakeService()
    window = MeetingConsoleWindow(service, host, dock_host=lambda: strip)
    states: list[bool] = []
    window.docked_changed.connect(states.append)
    try:
        host.show()
        window.show()
        _pump(1.0)
        quick = window.view
        assert window.bridge.dockable and not window.bridge.docked
        assert quick.rootObject().property("compact") is False

        service.info_changed.emit({"state": "live", "warnings": []})
        service.snapshot_changed.emit({"attendees": [_live_row("d1", "Ana")], "lobby": [], "hands": []})
        # The QML button's request is queued: docking reparents the QML's own window.
        window.bridge.requestDock(True)
        _pump(0.6)
        assert window.docked and window.bridge.docked and states == [True]
        assert strip.isVisible() and strip.isAncestorOf(quick_container(window))
        assert not window.isVisible()  # no empty top-level left behind
        assert quick.width() == 340
        root = quick.rootObject()
        assert root.property("compact") is True
        assert quick.errors() == []
        rows = _attendee_delegates(window)
        assert "Ana" in rows  # the attendee list still renders, and fits the strip
        button = rows["Ana"]
        right = button.mapToItem(root, button.width(), 0).x()
        assert right <= quick.width()

        window.bridge.requestDock(False)
        _pump(0.6)
        assert not window.docked and states == [True, False]
        assert window.isVisible() and not strip.isVisible() and window.isAncestorOf(quick_container(window))
        assert quick.rootObject().property("compact") is False
    finally:
        window.close()
        window.deleteLater()
        host.close()
        host.deleteLater()
        _pump(0.1)


def test_not_dockable_without_a_host():
    window = MeetingConsoleWindow(FakeService())
    try:
        assert window.bridge.dockable is False
        window.set_docked(True)
        assert not window.docked
    finally:
        window.deleteLater()
        _pump(0.1)
