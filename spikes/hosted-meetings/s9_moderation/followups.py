"""S9 follow-ups: measure actual media flow, not only events.

F1: after can_subscribe is revoked, does the participant keep receiving program audio?
F2: does UpdateSubscriptions / remove+rejoin stop it?
F3: revoking can_publish while publishing: does media from the participant stop reaching others?
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
from livekit import api, rtc

URL, HTTP_URL, KEY, SECRET = "ws://127.0.0.1:7880", "http://127.0.0.1:7880", "devkey", "secret"
ROOM = f"s9f-{int(time.time())}"
MIC = api.TrackSource.MICROPHONE


def token(identity: str, **grants) -> str:
    return (api.AccessToken(KEY, SECRET).with_identity(identity).with_name(identity)
            .with_grants(api.VideoGrants(room_join=True, room=ROOM, can_update_own_metadata=True, **grants)).to_jwt())


async def publish_tone(room: rtc.Room) -> rtc.LocalTrackPublication:
    src = rtc.AudioSource(48000, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", src)
    pub = await room.local_participant.publish_track(track, rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_MICROPHONE))
    tone = (np.sin(np.arange(480) / 48000 * 2 * np.pi * 440) * 8000).astype(np.int16).tobytes()

    async def feed():
        while room.isconnected():
            await src.capture_frame(rtc.AudioFrame(tone, 48000, 1, 480))
    asyncio.ensure_future(feed())
    return pub


class Counter:
    """Counts non-silent audio frames received from any remote audio track."""

    def __init__(self, room: rtc.Room) -> None:
        self.frames = 0
        room.on("track_subscribed", self._on_sub)

    def _on_sub(self, track, _pub, _rp) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.ensure_future(self._drain(rtc.AudioStream(track)))

    async def _drain(self, stream: rtc.AudioStream) -> None:
        async for ev in stream:
            if np.abs(np.frombuffer(ev.frame.data, dtype=np.int16)).max() > 100:
                self.frames += 1


async def rate(counter: Counter, seconds: float = 2.0) -> float:
    start = counter.frames
    await asyncio.sleep(seconds)
    return (counter.frames - start) / seconds


async def main() -> None:
    lk = api.LiveKitAPI(HTTP_URL, KEY, SECRET)
    perm = lambda **p: api.ParticipantPermission(can_update_metadata=True, **p)  # noqa: E731

    hall = rtc.Room()
    await hall.connect(URL, token("hall"))
    hall_pub = await publish_tone(hall)
    hall_counter = Counter(hall)

    # F1 / F2: listener loses can_subscribe
    a = rtc.Room()
    a_counter = Counter(a)
    await a.connect(URL, token("att-a", can_subscribe=True, can_publish=False))
    await asyncio.sleep(2)
    print(f"F1 admitted listener receives program: {await rate(a_counter):.0f} frames/s")
    await lk.room.update_participant(api.UpdateParticipantRequest(
        room=ROOM, identity="att-a", permission=perm(can_subscribe=False, can_publish=False)))
    await asyncio.sleep(1)
    print(f"F1 after can_subscribe=False: {await rate(a_counter, 4):.0f} frames/s "
          f"(subscribed={[p.subscribed for rp in a.remote_participants.values() for p in rp.track_publications.values()]})")
    await lk.room.update_subscriptions(api.UpdateSubscriptionsRequest(
        room=ROOM, identity="att-a", track_sids=[hall_pub.sid], subscribe=False))
    await asyncio.sleep(1)
    print(f"F2 after server UpdateSubscriptions(subscribe=False): {await rate(a_counter, 3):.0f} frames/s")

    # F2b: does the client re-subscribe on its own afterwards (auto_subscribe / manual set_subscribed)?
    for rp in a.remote_participants.values():
        for p in rp.track_publications.values():
            p.set_subscribed(True)
    await asyncio.sleep(2)
    print(f"F2b client tries set_subscribed(True) while can_subscribe=False: {await rate(a_counter, 3):.0f} frames/s")

    # F3: speaker loses can_publish while publishing
    b = rtc.Room()
    await b.connect(URL, token("att-b", can_subscribe=True, can_publish=True))
    await publish_tone(b)
    await asyncio.sleep(2)
    print(f"F3 hall receives speaker B: {await rate(hall_counter):.0f} frames/s")
    await lk.room.update_participant(api.UpdateParticipantRequest(
        room=ROOM, identity="att-b", permission=perm(can_subscribe=True, can_publish=False)))
    await asyncio.sleep(1)
    print(f"F3 after can_publish=False: hall receives {await rate(hall_counter, 3):.0f} frames/s; "
          f"B local publications={len(b.local_participant.track_publications)}")

    for r in (hall, a, b):
        await r.disconnect()
    await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
