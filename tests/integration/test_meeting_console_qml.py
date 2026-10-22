from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal, qInstallMessageHandler
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtTest import QTest

from solin.core.conference.meeting_core import MeetingParticipant, ParticipantStatus
from solin.core.conference.meeting_service import MeetingState
from solin.ui.qml.host import configure_qml_host
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
        self.is_connected = True
        self.is_busy = False
        self.locked = True
        self.join_url = "http://192.168.1.20:7890/join#k=secret-key"
        self.stage_url = "http://192.168.1.20:7890/stage/abc"
        self.calls = []

    def attendance(self):
        return 5

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, *args))


class _Switch(QObject):
    active_changed = Signal(str)

    def __init__(self, active):
        super().__init__()
        self.active = active


@pytest.fixture
def qml_warnings():
    messages: list[str] = []

    def handler(_mode, _context, message):
        if "MeetingConsoleView" in message:
            messages.append(message)

    previous = qInstallMessageHandler(handler)
    yield messages
    qInstallMessageHandler(previous)


def _load(bridge, width):
    widget = QQuickWidget()
    widget.resize(width, 700)
    configure_qml_host(widget, type_name="MeetingConsoleView", clear_color="#000000",
                       context_properties={"meetingConsole": bridge}, mouse_tracking=True)
    assert widget.errors() == []
    assert widget.rootObject() is not None
    widget.show()
    QTest.qWait(30)
    return widget


def _p(identity, status, hand=None, live=False):
    return MeetingParticipant(identity, identity[4:].title(), status, 2, 0, hand_order=hand,
                              microphone_live=live)


@pytest.mark.parametrize("width", [480, 1100])
def test_console_renders_every_state_without_qml_errors(width, qml_warnings):
    service = _Service()
    bridge = MeetingConsoleBridge(service, _Switch("zoom"), settings_store=None)
    widget = _load(bridge, width)
    assert widget.rootObject().property("narrow") is (width < 760)

    # Solin selected, idle; then live with a lobby, hands and a speaker.
    bridge._switch.active = "solin"
    bridge._switch.active_changed.emit("solin")
    QTest.qWait(10)
    service.state = MeetingState.OPEN
    service.state_changed.emit("open")
    participants = [
        _p("att:ann", ParticipantStatus.LOBBY),
        _p("att:ben", ParticipantStatus.SPEAKING, hand=1, live=True),
        _p("att:cy", ParticipantStatus.ADMITTED, hand=2),
    ]
    service.participants_changed.emit(participants)
    service.hands_changed.emit(participants[1:])
    QTest.qWait(30)
    assert bridge.property("lobbyCount") == 1

    service.error.emit("server keeps failing")
    QTest.qWait(10)
    assert qml_warnings == []
    widget.deleteLater()
