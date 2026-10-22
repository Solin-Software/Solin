from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from solin.core.conference.meeting_core import MeetingParticipant, ParticipantStatus
from solin.core.conference.meeting_service import MeetingState
from solin.ui.qml.meeting_console_bridge import MeetingConsoleBridge


class _Service(QObject):
    state_changed = Signal(str)
    connection_changed = Signal(bool)
    busy_changed = Signal(bool)
    operation_finished = Signal(object)
    attendance_changed = Signal(int)
    participants_changed = Signal(object)
    hands_changed = Signal(object)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.state = MeetingState.IDLE
        self.is_available = True
        self.is_enabled = False
        self.is_connected = False
        self.is_busy = False
        self.locked = False
        self.join_url = "http://192.168.1.20:7890/join#k=secret-key"
        self.stage_url = "http://192.168.1.20:7890/stage/abc"
        self.calls = []

    def attendance(self):
        return 7

    def configure(self, settings):
        self.calls.append(("configure", settings))

    def start(self):
        self.calls.append("start")
        self.is_enabled = True

    def stop(self):
        self.calls.append("stop")

    def __getattr__(self, name):  # operator commands
        return lambda *args: self.calls.append((name, *args))


class _Switch(QObject):
    active_changed = Signal(str)

    def __init__(self, active):
        super().__init__()
        self.active = active


class _Store:
    def meeting_settings(self):
        return "settings"


def _p(identity, status=ParticipantStatus.ADMITTED, hand=None):
    return MeetingParticipant(identity, identity.upper(), status, 2, 0, hand_order=hand)


def test_start_needs_the_hosted_provider():
    service = _Service()
    bridge = MeetingConsoleBridge(service, _Switch("zoom"), _Store())
    assert not bridge.property("hosted")
    bridge.startMeeting()
    assert service.calls == []
    bridge = MeetingConsoleBridge(service, _Switch("solin"), _Store())
    bridge.startMeeting()
    bridge.startMeeting()  # already running
    assert service.calls == [("configure", "settings"), "start"]


def test_rows_carry_status_and_hand_positions():
    service = _Service()
    bridge = MeetingConsoleBridge(service, _Switch("solin"), _Store())
    changes = []
    bridge.participantsChanged.connect(lambda: changes.append(1))
    service.participants_changed.emit([_p("att:a", ParticipantStatus.LOBBY), _p("att:b", hand=4),
                                       _p("att:c", hand=2)])
    service.hands_changed.emit([_p("att:c", hand=2), _p("att:b", hand=4)])
    rows = {row["identity"]: row for row in bridge.property("participants")}
    assert rows["att:b"]["handPosition"] == 2 and rows["att:c"]["handPosition"] == 1
    assert rows["att:a"]["status"] == "lobby" and rows["att:a"]["handPosition"] == 0
    assert bridge.property("lobbyCount") == 1
    assert [h["identity"] for h in bridge.property("hands")] == ["att:c", "att:b"]
    assert bridge.property("attendance") == 7 and changes


def test_commands_reach_the_service_and_failures_show():
    service = _Service()
    bridge = MeetingConsoleBridge(service, _Switch("solin"), _Store())
    bridge.admit("att:a")
    bridge.callOn("att:a")
    bridge.rename("att:a", "Ann")
    bridge.lock()
    assert ("admit", "att:a") in service.calls and ("rename", "att:a", "Ann") in service.calls
    assert ("request_pre_meeting",) in service.calls
    service.error.emit("server keeps failing")
    assert bridge.property("failed")
    bridge.stopMeeting()
    assert not bridge.property("failed") and bridge.property("participants") == []


def test_links_are_copied_and_the_key_is_never_displayed():
    from PySide6.QtGui import QGuiApplication

    service = _Service()
    bridge = MeetingConsoleBridge(service, _Switch("solin"), _Store())
    assert bridge.property("attendeeAddress") == "http://192.168.1.20:7890/join"
    bridge.copyJoinLink()
    assert QGuiApplication.clipboard().text() == "http://192.168.1.20:7890/join#k=secret-key"
    bridge.copyStageLink()
    assert QGuiApplication.clipboard().text() == "http://192.168.1.20:7890/stage/abc"
