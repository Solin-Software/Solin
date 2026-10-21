"""S6: edge-node failover for the hall (publisher) and attendees (subscribers).

Both roles know an ordered list of edge nodes (primary, backup). When the current node fails
they move to the next one, keeping their identity (the attendee's device id), so the meeting
continues on the backup. Two strategies:
  wait   - let the SDK try to resume on the current node; switch only on `disconnected`
  eager  - switch as soon as the SDK reports `reconnecting`

Every event is logged with an epoch-ms timestamp so the run script can line them up with the
moment node A was killed.

    python failover.py hall --nodes ws://127.0.0.1:7880 ws://127.0.0.1:7890
    python failover.py attendees --n 10 --nodes ws://127.0.0.1:7880 ws://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import aiohttp
import numpy as np
from livekit import api, rtc

ROOM = "s6"


def now_ms() -> float:
    return time.time() * 1000


def token(identity: str, publish: bool) -> str:
    return (api.AccessToken("devkey", "secret").with_identity(identity)
            .with_grants(api.VideoGrants(room_join=True, room=ROOM, can_publish=publish)).to_jwt())


class FailoverClient:
    def __init__(self, identity: str, nodes: list[str], strategy: str, log, publish: bool) -> None:
        self.identity, self.nodes, self.strategy, self.log, self.publish = identity, nodes, strategy, log, publish
        self.node = 0
        self.room: rtc.Room | None = None
        self.switching = False
        self.audio_frames = 0
        self.last_audio_ms = 0.0
        self.first_audio_after_switch: float | None = None
        self.live_room: rtc.Room | None = None   # set once connect() has completed

    async def start(self, probe: bool = False) -> None:
        await self._connect(self.node)
        if probe:
            asyncio.ensure_future(self._probe())

    async def _probe(self) -> None:
        """Health probe: GET the node's HTTP root every second; 2 misses in a row -> fail over.
        Catches silent failures (power/link loss) long before the SDK's ping timeout."""
        misses = 0
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.8)) as http:
            while True:
                await asyncio.sleep(1)
                if self.switching or self.room is None:
                    misses = 0
                    continue
                url = self.nodes[self.node].replace("ws://", "http://").replace("wss://", "https://")
                try:
                    async with http.get(url + "/") as r:
                        ok = r.status == 200
                except Exception:  # noqa: BLE001 - timeouts and refusals are both "miss"
                    ok = False
                misses = 0 if ok else misses + 1
                if misses >= 2:
                    misses = 0
                    self.log({"event": "probe_failed", "who": self.identity, "node": self.node, "t": now_ms()})
                    if not self.switching:
                        asyncio.ensure_future(self._failover(self.room))

    async def _connect(self, node: int) -> None:
        room = rtc.Room()
        self.room = room
        room.on("reconnecting", lambda: self._on_trouble(room, "reconnecting"))
        room.on("disconnected", lambda reason: self._on_trouble(room, "disconnected"))
        room.on("track_subscribed", self._on_track)
        await asyncio.wait_for(room.connect(self.nodes[node], token(self.identity, self.publish)), 10)
        self.live_room = room
        self.log({"event": "connected", "who": self.identity, "node": node, "t": now_ms()})
        if self.publish:
            await self._publish(room)

    def _on_trouble(self, room: rtc.Room, kind: str) -> None:
        if room is not self.room or self.switching:
            return
        self.log({"event": kind, "who": self.identity, "node": self.node, "t": now_ms()})
        if kind == "disconnected" or self.strategy == "eager":
            asyncio.ensure_future(self._failover(room))

    async def _failover(self, old: rtc.Room) -> None:
        self.switching = True
        # Don't wait for a goodbye from a dead node: tear the old room down in the background.
        asyncio.ensure_future(asyncio.wait_for(old.disconnect(), 5))
        attempt = 0
        while True:
            self.node = (self.node + 1) % len(self.nodes)
            attempt += 1
            try:
                self.first_audio_after_switch = None
                await self._connect(self.node)
                self.log({"event": "failed_over", "who": self.identity, "node": self.node,
                          "attempts": attempt, "t": now_ms()})
                break
            except Exception as exc:  # noqa: BLE001 - try the next node
                self.log({"event": "connect_failed", "who": self.identity, "node": self.node,
                          "error": type(exc).__name__, "t": now_ms()})
                await asyncio.sleep(min(2 ** attempt * 0.25, 4))
        self.switching = False

    def _on_track(self, track, _pub, _rp) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        room = self.room

        async def drain(stream: rtc.AudioStream):
            async for ev in stream:
                # Only audio from the room we're currently connected to counts (not buffered
                # frames still draining from the dead node).
                if room is not self.live_room or room is not self.room:
                    continue
                if np.abs(np.frombuffer(ev.frame.data, dtype=np.int16)).max() > 100:
                    self.audio_frames += 1
                    self.last_audio_ms = now_ms()
                    if self.first_audio_after_switch is None:
                        self.first_audio_after_switch = self.last_audio_ms
                        self.log({"event": "audio", "who": self.identity, "node": self.node,
                                  "t": self.last_audio_ms})
        asyncio.ensure_future(drain(rtc.AudioStream(track)))

    async def _publish(self, room: rtc.Room) -> None:
        asrc = rtc.AudioSource(48000, 1)
        await room.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("program-audio", asrc),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        vsrc = rtc.VideoSource(640, 360)
        await room.local_participant.publish_track(
            rtc.LocalVideoTrack.create_video_track("program", vsrc),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA, simulcast=True,
                                    video_codec=rtc.VideoCodec.H264,
                                    video_encoder=rtc.VideoEncoderBackend.ENCODER_BACKEND_SOFTWARE))
        self.log({"event": "published", "who": self.identity, "node": self.node, "t": now_ms()})
        tone = (np.sin(np.arange(480) / 48000 * 2 * np.pi * 440) * 8000).astype(np.int16).tobytes()
        frame = rtc.VideoFrame(640, 360, rtc.VideoBufferType.I420, bytes(640 * 360 * 3 // 2))

        async def feed():
            i = 0
            while room is self.room and room.isconnected():
                await asrc.capture_frame(rtc.AudioFrame(tone, 48000, 1, 480))
                i += 1
                if i % 3 == 0:
                    vsrc.capture_frame(frame)
        asyncio.ensure_future(feed())


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("role", choices=["hall", "attendees"])
    p.add_argument("--nodes", nargs="+", required=True)
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--first", type=int, default=0, help="index of the first attendee (device-<i>)")
    p.add_argument("--strategy", choices=["wait", "eager"], default="eager")
    p.add_argument("--probe", action="store_true", help="enable the HTTP health probe")
    p.add_argument("--duration", type=float, default=120)
    p.add_argument("--out", default="")
    a = p.parse_args()
    out = open(a.out, "a") if a.out else None

    def log(rec: dict) -> None:
        line = json.dumps({"role": a.role, "strategy": a.strategy, **rec})
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    if a.role == "hall":
        clients = [FailoverClient("hall", a.nodes, a.strategy, log, publish=True)]
    else:
        clients = [FailoverClient(f"device-{i}", a.nodes, a.strategy, log, publish=False)
                   for i in range(a.first, a.first + a.n)]
    for c in clients:
        await c.start(probe=a.probe)
    await asyncio.sleep(a.duration)
    import os
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
