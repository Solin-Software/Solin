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
    participant_ingress_changed = Signal(object)
    warning = Signal(str)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.configs = []
        self.spotlights = []

    def set_spotlight(self, identity):
        if not self.spotlights or self.spotlights[-1] != identity:
            self.spotlights.append(identity)

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

    def update_room_metadata(self, room, metadata):
        self.calls.append(("metadata", metadata))

    def update_participant(self, room, identity, **kwargs):
        self.calls.append(("update", identity, kwargs))
        if self.fail_updates:
            raise RoomApiError("down", "http_503")

    def remove_participant(self, room, identity):
        self.calls.append(("remove", identity))


class _FakeAttendeeServer:
    def __init__(self, upstream, fail=False):
        self.upstream = upstream
        self.fail = fail
        self.port = 0
        self.access = []
        self.hands = []
        self.stopped = False

    def start(self, host, port):
        if self.fail:
            raise OSError("port in use")
        self.port = port

    def stop(self):
        self.stopped = True

    def publish_access(self, access):
        self.access.append(access)

    def publish_hands(self, hands, attendance):
        self.hands.append((list(hands), attendance))


SETTINGS = MeetingSettings(server=ServerSettings(api_key="SOLk", api_secret="s" * 43))


@pytest.fixture
def parts():
    server, publisher, api = _FakeServer(), _FakePublisher(), _FakeApi()
    web = []
    service = SolinMeetingService(
        server, publisher, room_api_factory=lambda _s: api,
        attendee_server_factory=lambda upstream: web.append(_FakeAttendeeServer(upstream)) or web[-1],
        lan_address=lambda: "192.168.1.20", poll_interval_ms=50,
    )
    service.web = web
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
    assert ("metadata", '{"solin":1,"locked":true}') in api.calls


def test_attendee_tokens_need_an_open_meeting_and_a_sane_device_id(parts):
    service, server, *_ = parts
    service.configure(SETTINGS)
    assert service.attendee_token("phone") is None
    _open(service, server)
    assert service.attendee_token("phone-001", "Ann").count(".") == 2
    for bad in ("", "   ", "x" * 65, "line\nbreak", "phone"):
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


def test_the_attendee_page_opens_with_the_meeting_and_follows_it(parts):
    service, server, _publisher, api = parts
    _open(service, server)
    (web,) = service.web
    assert web.upstream == f"http://127.0.0.1:{SETTINGS.server.http_port}"
    assert web.port == SETTINGS.web_port
    access = web.access[-1]
    assert access.room == "solin-meeting" and access.stage_secret
    assert service.join_url == f"http://192.168.1.20:{SETTINGS.web_port}/join#k={service.e2ee_key}"
    assert service.stage_url.endswith("/stage/" + access.stage_secret)

    api.participants = [ParticipantInfo("att:phone-001", "Ann", attributes={"hand": "1"})]
    service.admit("att:phone-001")
    wait_until(lambda: web.hands and web.hands[-1][0], description="hands published",
               timeout_ms=3000)
    assert web.hands[-1][0][0].name == "Ann"

    service.remove("att:phone-001")
    assert "att:phone-001" in web.access[-1].removed
    service.stop()
    assert web.stopped and service.join_url == ""


def test_an_attendee_page_that_cannot_start_fails_the_meeting(parts):
    service, server, publisher, _api = parts
    service._attendee_server_factory = lambda upstream: _FakeAttendeeServer(upstream, fail=True)
    service.configure(SETTINGS)
    service.start()
    server.become(ServerState.RUNNING)
    wait_until(lambda: service.state is MeetingState.FAILED, description="failed", timeout_ms=3000)
    assert publisher.configs == []


def test_hall_audio_settings_reach_the_publisher_and_its_warning_is_surfaced(parts):
    from dataclasses import replace

    from solin.core.conference.meeting_service import HallAudioSettings

    service, server, publisher, _api = parts
    hall = HallAudioSettings(recording_device="{mixer-in}", playout_device="{mixer-out}",
                             noise_suppression=False)
    service.configure(replace(SETTINGS, hall_audio=hall))
    service.start()
    server.become(ServerState.RUNNING)
    wait_until(lambda: publisher.configs, description="publisher started", timeout_ms=3000)
    (config,) = publisher.configs
    assert config.hall_audio and config.recording_device == "{mixer-in}"
    assert config.playout_device == "{mixer-out}" and not config.noise_suppression
    changes = []
    service.hall_audio_available_changed.connect(changes.append)
    publisher.warning.emit("hall_audio_unavailable")
    publisher.warning.emit("hall_audio_unavailable")
    assert not service.hall_audio_available and changes == [False]
    service.stop()
    assert service.hall_audio_available and changes == [False, True]


def test_the_publisher_follows_the_spotlight(parts):
    service, server, publisher, api = parts
    _open(service, server)
    ingress = []
    service.participant_ingress_changed.connect(ingress.append)
    publisher.participant_ingress_changed.emit("descriptor")
    assert ingress == ["descriptor"]
    api.participants = [ParticipantInfo("att:phone-001", "Ann")]
    service.admit("att:phone-001")
    service.spotlight("att:phone-001")
    assert service.spotlighted == "att:phone-001" and publisher.spotlights[-1] == "att:phone-001"
    service.to_lobby("att:phone-001")  # leaving the meeting ends the spotlight
    assert publisher.spotlights[-1] == "" and service.spotlighted == ""
