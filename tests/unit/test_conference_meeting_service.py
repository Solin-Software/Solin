from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal, SignalInstance

from solin.core.conference.meeting_core import ClearHand, RemoveParticipant, SetName, SetPermission
from solin.core.conference.meeting_service import (
    MeetingSettings,
    MeetingState,
    SolinMeetingService,
    apply_actions,
)
from solin.core.conference.provider import PROVIDER_SIGNALS, MeetingProvider, StepFailure
from solin.core.conference.room_api import (
    ParticipantInfo,
    ParticipantNotFoundError,
    Permission,
    RoomApiError,
)
from solin.core.conference.server_config import ServerSettings
from solin.core.conference.server_process import ServerState
from solin.core.integrations.automation.zoom.service import ZoomService
from tests._qt import wait_until


class _FakeServer(QObject):
    state_changed = Signal(str)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.state = ServerState.STOPPED
        self.settings = None
        self.started = []

    def start(self, settings):
        self.settings = settings
        self.started.append(settings)

    def stop(self, *, wait=False, timeout_ms=3000):
        self.state = ServerState.STOPPED

    def become(self, state):
        self.state = state
        self.state_changed.emit(state.value)


class _FakePublisher(QObject):
    state_changed = Signal(str)
    egress_changed = Signal(object)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.configs = []

    def start(self, config):
        self.configs.append(config)

    def stop(self):
        self.egress_changed.emit(None)


class _FakeApi:
    def __init__(self):
        self.participants: list[ParticipantInfo] = []
        self.calls = []
        self.fail_updates = False

    def create_room(self, room, **kwargs):
        self.calls.append(("create_room", room))

    def list_participants(self, room):
        return list(self.participants)

    def update_participant(self, room, identity, **kwargs):
        self.calls.append(("update", identity, kwargs))
        if self.fail_updates:
            raise RoomApiError("down", "http_503")

    def remove_participant(self, room, identity):
        self.calls.append(("remove", identity))


SETTINGS = MeetingSettings(server=ServerSettings(api_key="SOLk", api_secret="s" * 43))


@pytest.fixture
def parts():
    server, publisher, api = _FakeServer(), _FakePublisher(), _FakeApi()
    service = SolinMeetingService(server, publisher, room_api_factory=lambda _s: api,
                                  poll_interval_ms=50)
    yield service, server, publisher, api
    service.stop(wait=True)


def _open(service, server):
    service.configure(SETTINGS)
    service.start()
    server.become(ServerState.RUNNING)
    wait_until(lambda: service.state is MeetingState.OPEN, description="open", timeout_ms=3000)


@pytest.mark.parametrize("provider_class", [ZoomService, SolinMeetingService])
def test_both_providers_expose_the_meeting_provider_surface(provider_class):
    for name in PROVIDER_SIGNALS:
        assert isinstance(getattr(provider_class, name), Signal), name
    if provider_class is ZoomService:
        provider = ZoomService()
    else:
        provider = SolinMeetingService(_FakeServer(), _FakePublisher())
    for name in PROVIDER_SIGNALS:
        assert isinstance(getattr(provider, name), SignalInstance)
    assert isinstance(provider, MeetingProvider)
    provider.deleteLater()


def test_starting_requires_settings_and_sequences_require_an_open_meeting(parts):
    service, *_ = parts
    with pytest.raises(RuntimeError):
        service.start()
    assert not service.request_pre_meeting() and not service.request_post_meeting()


def test_start_opens_the_room_then_publishes_with_a_fresh_e2ee_key(parts):
    service, server, publisher, api = parts
    enabled, egress = [], []
    service.enabled_changed.connect(enabled.append)
    service.egress_changed.connect(egress.append)
    _open(service, server)
    assert server.started == [SETTINGS.server]
    assert api.calls[0] == ("create_room", "solin-meeting")
    (config,) = publisher.configs
    assert config.e2ee_key == service.e2ee_key and len(service.e2ee_key) >= 32
    assert config.url == f"ws://127.0.0.1:{SETTINGS.server.http_port}"
    publisher.state_changed.emit("publishing")
    assert service.is_connected and enabled == [True]
    service.stop()
    assert enabled == [True, False] and not service.is_connected and egress[-1] is None


def test_lobby_attendees_appear_and_sequences_report_failures(parts):
    service, server, publisher, api = parts
    _open(service, server)
    seen, results = [], []
    service.participants_changed.connect(seen.append)
    service.operation_finished.connect(results.append)
    api.participants = [ParticipantInfo("att:a", "Ann", permission=Permission(
        hidden=True, can_update_metadata=True))]
    wait_until(lambda: seen and seen[-1], description="participant", timeout_ms=3000)
    service.admit("att:a")
    wait_until(lambda: any(c[0] == "update" for c in api.calls), description="admit sent",
               timeout_ms=3000)
    api.fail_updates = True
    assert service.request_pre_meeting()
    assert service.is_busy and not service.request_post_meeting()
    wait_until(lambda: results, description="sequence result", timeout_ms=3000)
    assert results[0].sequence == "pre_meeting"
    assert results[0].failed_steps == (StepFailure("SetPermission", "http_503"),)
    assert not service.is_busy and service.locked


def test_attendee_tokens_need_an_open_meeting_and_a_sane_device_id(parts):
    service, server, *_ = parts
    service.configure(SETTINGS)
    assert service.attendee_token("phone") is None
    _open(service, server)
    assert service.attendee_token("phone", "Ann").count(".") == 2
    for bad in ("", "   ", "x" * 65, "line\nbreak"):
        assert service.attendee_token(bad) is None


def test_server_failure_fails_the_meeting(parts):
    service, server, *_ = parts
    errors = []
    service.error.connect(errors.append)
    _open(service, server)
    server.error.emit("Conference server keeps failing")
    assert service.state is MeetingState.FAILED and errors


def test_a_server_restart_recreates_the_room(parts):
    service, server, _publisher, api = parts
    _open(service, server)
    server.become(ServerState.RESTARTING)
    server.become(ServerState.RUNNING)
    wait_until(lambda: len([c for c in api.calls if c[0] == "create_room"]) == 2,
               description="room recreated", timeout_ms=3000)


def test_configure_is_refused_while_open(parts):
    service, server, *_ = parts
    _open(service, server)
    with pytest.raises(RuntimeError):
        service.configure(SETTINGS)


def test_apply_actions_maps_each_action_and_skips_departed_participants():
    class _Api:
        def __init__(self):
            self.calls = []

        def update_participant(self, room, identity, **kwargs):
            if identity == "att:gone":
                raise ParticipantNotFoundError("gone", "not_found")
            self.calls.append((identity, kwargs))

        def remove_participant(self, room, identity):
            self.calls.append((identity, "removed"))

    api = _Api()
    failures = apply_actions(api, "r", [
        SetPermission("att:a", Permission()), SetName("att:a", "Ann"), ClearHand("att:a"),
        SetPermission("att:gone", Permission()), RemoveParticipant("att:b"),
    ])
    assert failures == ()
    assert api.calls == [
        ("att:a", {"permission": Permission()}), ("att:a", {"name": "Ann"}),
        ("att:a", {"attributes": {"hand": ""}}), ("att:b", "removed"),
    ]
