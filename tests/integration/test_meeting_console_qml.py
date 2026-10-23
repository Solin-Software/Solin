"""The Live meeting console loads and renders live meeting state without QML errors."""

from __future__ import annotations

import time

from PySide6.QtCore import QCoreApplication, QObject, QtMsgType, Signal, qInstallMessageHandler
from PySide6.QtQuick import QQuickItem

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
        quick = window._qml
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
