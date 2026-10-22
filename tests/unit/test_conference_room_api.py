from __future__ import annotations

import pytest

from solin.core.conference.room_api import (
    ParticipantInfo,
    ParticipantNotFoundError,
    Permission,
    RoomApi,
    RoomApiError,
    TrackSource,
)
from solin.core.network.http import HttpStatusError, HttpTransportError

# A ListParticipants entry as livekit-server 1.13 returns it.
SERVER_PARTICIPANT = {
    "sid": "PA_x", "identity": "dev1", "state": "ACTIVE", "joined_at_ms": "1792642801610",
    "name": "Ann", "attributes": {"attendance": "2", "hand": "123"},
    "permission": {"can_subscribe": False, "can_publish": False, "can_publish_data": True,
                   "can_publish_sources": ["MICROPHONE", "SOMETHING_NEW"], "hidden": True,
                   "can_update_metadata": True, "recorder": False},
    "tracks": [{"sid": "TR_a", "type": "AUDIO", "source": "MICROPHONE", "muted": False}],
}


class _Poster:
    def __init__(self, replies=None, error=None):
        self.calls = []
        self.replies = list(replies or [])
        self.error = error

    def __call__(self, url, body, *, timeout, headers):
        self.calls.append((url, body, timeout, headers))
        if self.error:
            raise self.error
        return self.replies.pop(0) if self.replies else {}


def test_participants_are_parsed_from_server_records():
    poster = _Poster([{"participants": [SERVER_PARTICIPANT]}])
    (participant,) = RoomApi("http://127.0.0.1:7880/", "k", "s", post=poster).list_participants("m")
    assert participant.identity == "dev1" and participant.joined_at_ms == 1792642801610
    assert participant.attributes == {"attendance": "2", "hand": "123"}
    assert participant.permission == Permission(
        can_publish_data=True, can_publish_sources=(TrackSource.MICROPHONE,), hidden=True,
        can_update_metadata=True,
    )
    assert participant.publishing(TrackSource.MICROPHONE)
    url, body, timeout, headers = poster.calls[0]
    assert url == "http://127.0.0.1:7880/twirp/livekit.RoomService/ListParticipants"
    assert body == {"room": "m"} and timeout == 3.0
    assert headers["Authorization"].startswith("Bearer ")


def test_update_always_sends_the_complete_permission():
    poster = _Poster([SERVER_PARTICIPANT])
    api = RoomApi("http://h", "k", "s", post=poster)
    api.update_participant("m", "dev1", permission=Permission(can_subscribe=True),
                           attributes={"hand": ""})
    body = poster.calls[0][1]
    assert body["permission"] == {
        "can_subscribe": True, "can_publish": False, "can_publish_data": False,
        "can_publish_sources": [], "hidden": False, "can_update_metadata": False,
    }
    assert body["attributes"] == {"hand": ""} and "name" not in body


def test_errors_are_classified():
    missing = HttpStatusError("u", 404, '{"code":"not_found"}', method="POST")
    with pytest.raises(ParticipantNotFoundError) as error:
        RoomApi("http://h", "k", "s", post=_Poster(error=missing)).remove_participant("m", "x")
    assert error.value.code == "not_found"
    with pytest.raises(RoomApiError) as error:
        RoomApi("http://h", "k", "s", post=_Poster(error=HttpTransportError("down"))).delete_room("m")
    assert error.value.code == "unavailable"


def test_empty_records_parse_safely():
    participant = ParticipantInfo.from_record({"identity": "x", "joined_at_ms": "bad"})
    assert participant.joined_at_ms == 0 and participant.permission == Permission()
