"""The real publisher process against a real livekit-server, checked by a real subscriber."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time

import pytest

from solin.core.conference.media_ring import MediaRingWriter
from solin.core.conference.tokens import AccessTokenRequest, RoomGrants, mint_access_token
from tests._livekit import requires_livekit_server

rtc = pytest.importorskip("livekit.rtc")

W, H, FPS = 320, 180, 15
ROOM = "solin-test"


pytestmark = requires_livekit_server


def _token(key, secret, identity, **grants):
    return mint_access_token(key, secret, AccessTokenRequest(identity, RoomGrants(ROOM, **grants)))


@pytest.mark.parametrize("e2ee_key", ["", "shared-meeting-key"])
def test_published_program_reaches_a_subscriber(livekit_server, e2ee_key):
    url, key, secret = livekit_server
    video = MediaRingWriter(None, 4, W * H * 3 // 2, create=True)
    audio = MediaRingWriter(None, 64, 16384, create=True)
    stop = threading.Event()

    def produce():
        n = 0
        while not stop.is_set():
            ts = time.monotonic_ns()
            video.write(bytes([n % 255]) * (W * H) + bytes([128]) * (W * H // 2),
                        timestamp_ns=ts, meta=W << 16 | H)
            for _ in range(3):  # ~64 ms of 1024-frame chunks per video frame at 15 fps
                audio.write(b"\0" * (1024 * 2 * 4), timestamp_ns=ts, meta=1024 | 2 << 16)
            n += 1
            time.sleep(1 / FPS)

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    publisher = subprocess.Popen(
        [sys.executable, "-m", "solin.core.conference.publisher_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert json.loads(publisher.stdout.readline()) == {"ready": 1}
        publisher.stdin.write(json.dumps({
            "command": "start", "url": url, "token": _token(key, secret, "hall"),
            "video_ring": video.name, "audio_ring": audio.name, "width": W, "height": H,
            "fps": FPS, "e2ee_key": e2ee_key,
        }) + "\n")
        publisher.stdin.flush()
        assert json.loads(publisher.stdout.readline()) == {"state": "connecting"}
        assert json.loads(publisher.stdout.readline()) == {"state": "publishing"}
        received = asyncio.run(_subscribe(url, _token(key, secret, "viewer", can_publish=False), e2ee_key))
        assert received["video"] >= 5, received
        assert received["audio"] >= 5, received
        publisher.stdin.write('{"command":"stop"}\n')
        publisher.stdin.flush()
        assert publisher.wait(15) == 0
    finally:
        stop.set()
        producer.join(2)
        if publisher.poll() is None:
            publisher.kill()
            publisher.wait()
        for ring in (video, audio):
            ring.close()
            ring.unlink()


async def _subscribe(url: str, token: str, e2ee_key: str) -> dict[str, int]:
    counts = {"video": 0, "audio": 0, "width": 0}
    options = rtc.RoomOptions(auto_subscribe=True)
    if e2ee_key:
        options.encryption = rtc.E2EEOptions(
            key_provider_options=rtc.KeyProviderOptions(shared_key=e2ee_key.encode())
        )
    room = rtc.Room()
    tasks: list[asyncio.Task] = []

    async def count(stream, kind):
        async for event in stream:
            counts[kind] += 1
            if kind == "video":
                counts["width"] = max(counts["width"], event.frame.width)

    def on_track(track, *_):
        if track.kind == rtc.TrackKind.KIND_VIDEO:
            tasks.append(asyncio.create_task(count(rtc.VideoStream(track), "video")))
        elif track.kind == rtc.TrackKind.KIND_AUDIO:
            tasks.append(asyncio.create_task(count(rtc.AudioStream(track), "audio")))

    room.on("track_subscribed", on_track)
    await room.connect(url, token, options=options)
    deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < deadline and not (counts["video"] >= 5 and counts["audio"] >= 5):
        await asyncio.sleep(0.2)
    for task in tasks:
        task.cancel()
    await room.disconnect()
    return counts


def test_service_drives_the_real_publisher(livekit_server):
    from solin.core.conference.publisher_service import (
        MeetingPublisherService, PublisherConfig, PublisherState,
    )
    from tests._qt import wait_until

    url, key, secret = livekit_server
    service = MeetingPublisherService()
    egress = []
    service.egress_changed.connect(egress.append)
    stop = threading.Event()
    try:
        service.start(PublisherConfig(url=url, api_key=key, api_secret=secret, room=ROOM,
                                      width=W, height=H, fps=FPS))
        descriptor = egress[0]
        video = MediaRingWriter(descriptor.video_ring)
        audio = MediaRingWriter(descriptor.audio_ring)

        def produce():  # what the sidecar's meeting egress does
            while not stop.is_set():
                video.write(bytes([90]) * (W * H * 3 // 2), timestamp_ns=time.monotonic_ns(),
                            meta=W << 16 | H)
                audio.write(b"\0" * 8192, meta=1024 | 2 << 16)
                time.sleep(1 / FPS)

        threading.Thread(target=produce, daemon=True).start()
        wait_until(lambda: service.state is PublisherState.PUBLISHING,
                   description="publishing", timeout_ms=20000)
        received = asyncio.run(_subscribe(url, _token(key, secret, "viewer", can_publish=False), ""))
        assert received["video"] >= 5 and received["audio"] >= 5, received
    finally:
        stop.set()
        service.stop()
        service.deleteLater()
    assert egress[-1] is None
