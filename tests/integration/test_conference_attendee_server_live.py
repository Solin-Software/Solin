"""The attendee server on its own thread, with signaling proxied to a real livekit-server."""

from __future__ import annotations

import asyncio
import json
import urllib.request

import pytest

from solin.core.conference.attendee_access import AttendeeAccess
from solin.core.conference.attendee_server import AttendeeHttpApplication, AttendeeServer
from solin.core.conference.room_api import RoomApi
from tests._livekit import requires_livekit_server

rtc = pytest.importorskip("livekit.rtc")
pytestmark = requires_livekit_server
ROOM = "solin-meeting"


def _post_json(url: str, body: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback test
        return json.loads(response.read())


def test_an_attendee_joins_through_the_proxy(livekit_server, tmp_path):
    url, key, secret = livekit_server
    http_url = url.replace("ws://", "http://")
    RoomApi(http_url, key, secret).create_room(ROOM)
    (tmp_path / "join.html").write_text("join")
    server = AttendeeServer(AttendeeHttpApplication(
        pages_directory=tmp_path, client_directory=None, livekit_http_url=http_url))
    server.start("127.0.0.1", 0)
    try:
        server.publish_access(AttendeeAccess("m-1", "Hall", ROOM, key, secret, "stage-secret"))
        origin = f"http://127.0.0.1:{server.port}"
        token = _post_json(f"{origin}/api/join", {"deviceId": "phone-12345", "name": "Ann"})["token"]

        async def join():
            room = rtc.Room()
            # The page's only origin: signaling goes through the attendee server.
            await room.connect(origin.replace("http", "ws"), token)
            await asyncio.sleep(0.5)
            participants = await asyncio.to_thread(RoomApi(http_url, key, secret).list_participants, ROOM)
            await room.disconnect()
            return participants

        (participant,) = asyncio.run(join())
        assert participant.identity == "att:phone-12345" and participant.name == "Ann"
        assert participant.permission.hidden  # waiting in the lobby
    finally:
        server.stop()
