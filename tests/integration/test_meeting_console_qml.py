"""The Live meeting console loads and renders live meeting state without QML errors."""

from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, QObject, QtMsgType, Signal, qInstallMessageHandler
from PySide6.QtQuick import QQuickItem
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from solin.styles.theme import PALETTE
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
        return "3"


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
        upcoming = [
            {"id": "midweek:2026-11-18", "kind": "midweek", "startsAt": "2026-11-18T19:30:00-03:00",
             "code": "k7m4p2", "password": "482913", "url": "https://meet.solin.run/k7m4p2", "cancelled": False,
             "moved": False, "open": True, "running": True},
            {"id": "extra:1", "kind": "extra", "startsAt": "2026-11-20T19:30:00-03:00", "code": "b2c3d4",
             "password": "111111", "url": "https://meet.solin.run/b2c3d4", "cancelled": True, "moved": True,
             "open": False, "running": False},
        ]
        device = {"device": "m1", "name": "Irmão Lima", "role": "moderator", "lastSeen": 1.8e9}
        idle = {"state": "idle", "linkMode": "per_meeting", "upcoming": upcoming, "devices": [device],
                "lowOnCodes": True, "warnings": []}
        service.info_changed.emit(idle)
        _pump(0.4)
        assert quick.errors() == []
        service.info_changed.emit(dict(idle, linkMode="same", invitationUrl="https://meet.solin.run/k7m4p2",
                                       password="482913", stageUrl="https://meet.solin.run/k7m4p2/s",
                                       moderatorUrl="https://meet.solin.run/k7m4p2/m", linkScope="hub"))
        _pump(0.4)
        live = {"state": "live", "invitationUrl": "https://meet.solin.run/k7m4p2", "password": "482913",
                "stageUrl": "https://meet.solin.run/k7m4p2/s", "moderatorUrl": "https://meet.solin.run/k7m4p2/m",
                "linkMode": "per_meeting", "upcoming": upcoming, "output": "live", "tunnel": "online",
                "galene": "running", "commenterAudio": "running", "warnings": ["Example warning"], "error": ""}
        row = {"device": "d1", "name": "Ana", "state": "admitted", "count": 3, "connected": True, "live": False,
               "partHolder": False, "hand": True}
        service.info_changed.emit(live)
        service.snapshot_changed.emit({
            "micsLocked": True, "lobby": [dict(row, device="d2", name="Bia", state="lobby", count=0)],
            "attendees": [row], "hands": [row], "connected": 1, "people": 3, "devices": 1, "live": "",
            "staffRequests": [{"device": "m2", "name": "Irmão <b>Reis</b>", "role": "moderator"},
                              {"device": "s1", "name": "Tribuna", "role": "stage"}],
            "staff": [{"device": "m1", "name": "Irmão Lima", "role": "moderator", "networkChanged": True}],
            "approvedDevices": [dict(device, online=True)],
            "officialCount": {"people": 3, "devices": 1, "at": "2026-11-18T21:00-03:00"},
        })
        _pump(0.6)
        assert quick.errors() == []
        window.bridge.admitAll()
        window.bridge.callOn("d1")
        window.bridge.approveStaff("m2")
        window.bridge.removeDevice("m1")
        window.bridge.newLink("extra:1")
        window.bridge.copyCount()
        assert ("admit_all",) in service.calls and ("call_on", "d1") in service.calls
        assert ("approve_staff", "m2") in service.calls and ("remove_device", "m1") in service.calls
        assert ("new_link", "extra:1") in service.calls
    finally:
        qInstallMessageHandler(previous)
        window.close()
        window.deleteLater()
        _pump(0.1)
    assert problems == [], problems


def _visible_texts(item: QQuickItem) -> list[str]:
    texts: list[str] = []
    if not item.isVisible():
        return texts
    if item.metaObject().className().startswith("QQuickText") and item.property("text"):
        texts.append(str(item.property("text")))
    for child in item.childItems():
        texts.extend(_visible_texts(child))
    return texts


def test_meeting_information_folds_away_during_a_meeting():
    """Open between meetings; folded (link and password still on one line) during one; the header toggles it."""
    service = FakeService()
    window = MeetingConsoleWindow(service)
    try:
        window.show()
        links = {"invitationUrl": "https://meet.solin.run/k7m4p2", "password": "482913", "linkMode": "same",
                 "stageUrl": "https://meet.solin.run/k7m4p2/s", "moderatorUrl": "https://meet.solin.run/k7m4p2/m",
                 "linkScope": "hub", "upcoming": [], "warnings": []}
        service.info_changed.emit(dict(links, state="idle"))
        _pump(0.4)
        root = window.view.rootObject()
        assert root.property("infoExpanded") is True
        assert "Stage page" in _visible_texts(root)

        service.info_changed.emit(dict(links, state="live"))
        _pump(0.4)
        assert root.property("infoExpanded") is False
        texts = _visible_texts(root)
        assert "Stage page" not in texts
        assert "meet.solin.run/k7m4p2  ·  password 482913" in texts

        root.setProperty("infoExpanded", True)   # what a click on the header does
        _pump(0.2)
        assert "Stage page" in _visible_texts(root)
        assert window.view.errors() == []
    finally:
        window.close()
        window.deleteLater()
        _pump(0.1)


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
        # Reads as window chrome: the bottom bar's background and line.
        seam = root.findChild(QQuickItem, "dockSeam")
        assert seam.isVisible() and seam.width() == 1 and seam.height() == root.height()
        assert seam.property("color").name() == PALETTE.border
        assert root.property("color").name() == PALETTE.bg1
        assert quick.color().name() == PALETTE.bg1
        grabbed = quick.grabWindow()
        assert grabbed.pixelColor(0, 400).name() == PALETTE.border
        assert grabbed.pixelColor(1, 400).name() == PALETTE.bg1
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
        assert not seam.isVisible()
        assert root.property("color").name() == PALETTE.bg0
        assert quick.color().name() == PALETTE.bg0
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


def test_dialogs_fit_a_narrow_docked_panel():
    """Docked at its narrowest, a dialog stays inside the panel and its fields inside the dialog."""
    service = FakeService()
    window = MeetingConsoleWindow(service)
    try:
        window.resize(300, 700)
        window.show()
        service.info_changed.emit({"state": "idle", "linkMode": "per_meeting", "upcoming": [], "warnings": []})
        _pump(0.4)
        root = window.view.rootObject()
        dialog = root.findChild(QObject, "timeDialog")
        dialog.open()
        _pump(0.5)
        field = root.findChild(QQuickItem, "dateField")
        width = dialog.property("width")
        assert 0 < width <= root.width()
        assert field.isVisible() and field.width() > 40
        right = field.mapToItem(dialog.property("contentItem"), field.width(), 0).x()
        assert right <= dialog.property("availableWidth") + 0.5
    finally:
        window.close()
        window.deleteLater()
        _pump(0.1)


def test_moderators_and_stage_come_after_the_attendees():
    service = FakeService()
    window = MeetingConsoleWindow(service)
    try:
        window.resize(1080, 680)
        window.show()
        service.info_changed.emit({"state": "live", "linkMode": "same", "upcoming": [], "warnings": [],
                                   "invitationUrl": "https://meet.solin.run/k7m4p2", "password": "482913"})
        row = {"device": "d1", "name": "Ana", "state": "admitted", "count": 1, "connected": True, "live": False,
               "partHolder": False, "hand": False}
        service.snapshot_changed.emit({"attendees": [row, dict(row, device="d2", name="Bia")], "hands": [],
                                       "lobby": [], "staff": [{"device": "m1", "name": "Lima", "role": "moderator"}]})
        _pump(0.6)
        root = window.view.rootObject()
        staff = root.findChild(QQuickItem, "staffOnline")
        assert staff is not None and staff.isVisible()
        list_view = staff.parentItem().parentItem()          # footer -> contentItem -> ListView
        top = staff.mapToItem(list_view, 0, 0).y()
        assert top >= 2 * 20, top                              # below both attendee rows
    finally:
        window.close()
        window.deleteLater()
        _pump(0.1)
