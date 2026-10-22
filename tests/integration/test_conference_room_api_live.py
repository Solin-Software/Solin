"""RoomApi against a real livekit-server, with a real participant in the room."""

from __future__ import annotations

import asyncio

import pytest

from solin.core.conference.room_api import (
    ParticipantNotFoundError,
    Permission,
    RoomApi,
    TrackSource,
)
from solin.core.conference.tokens import AccessTokenRequest, RoomGrants, mint_access_token
from tests._livekit import requires_livekit_server

rtc = pytest.importorskip("livekit.rtc")
pytestmark = requires_livekit_server
ROOM = "solin-moderation"


def test_moderation_primitives_on_a_real_server(livekit_server):
    url, key, secret = livekit_server
    api = RoomApi(url.replace("ws://", "http://"), key, secret)
    api.create_room(ROOM, metadata="meeting")

    async def scenario():
        room = rtc.Room()
        lobby = RoomGrants(ROOM, can_publish=False, can_subscribe=False, hidden=True,
                           can_update_own_metadata=True)
        await room.connect(url, mint_access_token(key, secret, AccessTokenRequest(
            "device-1", lobby, name="Ann", attributes={"attendance": "2"})))
        await room.local_participant.set_attributes({"hand": "1700000000000"})
        await asyncio.sleep(0.3)

        (waiting,) = await asyncio.to_thread(api.list_participants, ROOM)
        assert waiting.identity == "device-1" and waiting.name == "Ann"
        assert waiting.permission.hidden and not waiting.permission.can_subscribe
        assert waiting.attributes == {"attendance": "2", "hand": "1700000000000"}

        speaking = Permission(can_subscribe=True, can_publish=True,
                              can_publish_sources=(TrackSource.MICROPHONE,),
                              can_update_metadata=True)
        updated = await asyncio.to_thread(
            api.update_participant, ROOM, "device-1", permission=speaking,
            name="Ann B.", attributes={"hand": ""},
        )
        assert updated.permission == speaking
        assert updated.name == "Ann B."
        assert updated.attributes == {"attendance": "2"}  # merged, hand deleted

        with pytest.raises(ParticipantNotFoundError):
            await asyncio.to_thread(api.update_participant, ROOM, "nobody", name="x")

        await asyncio.to_thread(api.remove_participant, ROOM, "device-1")
        await asyncio.sleep(0.3)
        assert await asyncio.to_thread(api.list_participants, ROOM) == []
        await room.disconnect()

    asyncio.run(scenario())
    api.delete_room(ROOM)
