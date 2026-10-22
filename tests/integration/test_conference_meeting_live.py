"""A whole Solin-hosted meeting: real server, real publisher, a real attendee client."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading

import pytest

from solin.core.conference.meeting_core import ParticipantStatus
from solin.core.conference.meeting_service import (
    MeetingSettings,
    MeetingState,
    SolinMeetingService,
)
from solin.core.conference.publisher_service import MeetingPublisherService
from solin.core.conference.server_config import ServerRole, ServerSettings, generate_credentials
from solin.core.conference.server_process import ConferenceServerService
from tests._livekit import requires_livekit_server
from tests._qt import wait_until

rtc = pytest.importorskip("livekit.rtc")
pytestmark = requires_livekit_server


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Attendee:
    """A LiveKit client on its own asyncio loop, so the Qt loop keeps running."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.room = None
        self.permissions: list = []
        self.subscribed: list[str] = []
        self.disconnected = threading.Event()

    def run(self, coroutine, timeout=15):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    async def _connect(self, url, token, key):
        options = rtc.RoomOptions(auto_subscribe=True)
        if key:
            options.encryption = rtc.E2EEOptions(
                key_provider_options=rtc.KeyProviderOptions(shared_key=key.encode()))
        room = rtc.Room()
        room.on("participant_permissions_changed",
                lambda participant, permission: participant.identity == room.local_participant.identity
                and self.permissions.append(permission))
        room.on("track_subscribed", lambda track, *_: self.subscribed.append(track.name))
        room.on("disconnected", lambda *_: self.disconnected.set())
        await room.connect(url, token, options=options)
        await room.local_participant.set_attributes({"attendance": "3"})
        self.room = room

    def connect(self, url, token, key):
        self.run(self._connect(url, token, key))

    def set_attributes(self, attributes):
        self.run(self.room.local_participant.set_attributes(attributes))

    def close(self):
        if self.room is not None:
            with contextlib.suppress(Exception):  # already removed by the meeting
                self.run(self.room.disconnect(), timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture
def meeting(tmp_path):
    key, secret = generate_credentials()
    settings = MeetingSettings(
        server=ServerSettings(api_key=key, api_secret=secret, role=ServerRole.HALL,
                              http_port=_free_port(), rtc_tcp_port=_free_port(),
                              rtc_udp_port=_free_port(), bind_addresses=("127.0.0.1",),
                              node_ip="127.0.0.1"),
        width=320, height=180, fps=15,
    )
    server = ConferenceServerService(tmp_path / "conference")
    publisher = MeetingPublisherService()
    service = SolinMeetingService(server, publisher, poll_interval_ms=200)
    service.configure(settings)
    attendee = _Attendee()
    yield service, settings, attendee
    attendee.close()
    service.stop(wait=True)
    for obj in (service, publisher, server):
        obj.deleteLater()


def _status(service, identity):
    return next((p.status for p in service.participants() if p.identity == identity), None)


def test_a_meeting_from_lobby_to_removal(meeting):
    service, settings, attendee = meeting
    results, egress = [], []
    service.operation_finished.connect(results.append)
    service.egress_changed.connect(egress.append)

    service.start()
    wait_until(lambda: service.state is MeetingState.OPEN and service.is_connected,
               description="meeting open and hall publishing", timeout_ms=30000)
    assert egress and egress[0] is not None  # the scene runtime gets the rings
    url = f"ws://127.0.0.1:{settings.server.http_port}"

    token = service.attendee_token("phone-1", "Ann")
    attendee.connect(url, token, service.e2ee_key)
    wait_until(lambda: _status(service, "att:phone-1") is ParticipantStatus.LOBBY,
               description="attendee in the lobby", timeout_ms=10000)
    assert attendee.subscribed == []  # the lobby hears nothing

    service.admit("att:phone-1")
    wait_until(lambda: "program-audio" in attendee.subscribed,
               description="admitted attendee hears the program", timeout_ms=15000)
    assert service.attendance() == 3

    assert service.request_pre_meeting()
    wait_until(lambda: results, description="lock finished", timeout_ms=10000)
    assert results[0].sequence == "pre_meeting" and results[0].ok
    wait_until(lambda: attendee.permissions and not attendee.permissions[-1].can_publish,
               description="attendee locked", timeout_ms=10000)

    attendee.set_attributes({"hand": "1"})
    wait_until(lambda: [p.identity for p in service.hands()] == ["att:phone-1"],
               description="hand in the queue", timeout_ms=10000)

    service.call_on("att:phone-1")
    wait_until(lambda: attendee.permissions[-1].can_publish,
               description="attendee may speak", timeout_ms=10000)
    assert list(attendee.permissions[-1].can_publish_sources) == [2]  # MICROPHONE only
    assert _status(service, "att:phone-1") is ParticipantStatus.SPEAKING

    service.finish("att:phone-1")
    wait_until(lambda: service.hands() == [] and not attendee.permissions[-1].can_publish,
               description="comment finished", timeout_ms=10000)

    service.remove("att:phone-1")
    wait_until(attendee.disconnected.is_set, description="attendee removed", timeout_ms=10000)
    assert service.attendee_token("phone-1") is None
    assert service.attendee_token("phone-2") is not None

    service.stop(wait=True)
    assert service.state is MeetingState.IDLE and egress[-1] is None
