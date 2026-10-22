from __future__ import annotations

import asyncio
import io
import json
import struct

import numpy as np
import pytest

from solin.core.conference import publisher_worker as worker
from solin.core.conference.media_ring import MediaRingWriter
from solin.core.conference.publisher_worker import (
    MediaPump,
    PublishRequest,
    float_planes_to_s16,
)

W, H = 32, 16
FRAME = W * H * 3 // 2


@pytest.fixture
def rings():
    video = MediaRingWriter(None, 4, FRAME, create=True)
    audio = MediaRingWriter(None, 16, 8192, create=True)
    yield video, audio
    for ring in (video, audio):
        ring.close()
        ring.unlink()


def _request(**overrides) -> dict:
    return {
        "command": "start", "url": "ws://127.0.0.1:7880", "token": "t",
        "video_ring": "v", "audio_ring": "a", "width": 640, "height": 360, "fps": 30,
        **overrides,
    }


def test_publish_request_accepts_a_valid_start_message():
    request = PublishRequest.from_message(_request(e2ee_key="k", simulcast=False))
    assert request.width == 640 and request.e2ee_key == "k" and not request.simulcast


@pytest.mark.parametrize("overrides", [
    {"url": "http://host"}, {"token": ""}, {"width": 641}, {"height": 8}, {"fps": 0},
    {"max_bitrate": 10}, {"simulcast": 1}, {"unknown": True}, {"video_ring": ""},
    {"audio_ring": ""}, {"fps": 30.0},
])
def test_publish_request_rejects_bad_values(overrides):
    with pytest.raises(ValueError):
        PublishRequest.from_message(_request(**overrides))


def test_audio_only_needs_no_video_ring():
    assert PublishRequest.from_message(_request(audio_only=True, video_ring="")).audio_only


def test_float_planes_interleave_and_clip_to_s16():
    left = struct.pack("<3f", 0.0, 0.5, 2.0)
    right = struct.pack("<3f", -0.5, -1.0, -3.0)
    pcm = np.frombuffer(float_planes_to_s16(left + right, 3), dtype=np.int16)
    assert pcm.tolist() == [0, -16383, 16383, -32767, 32767, -32768]


def test_pump_keeps_only_the_newest_video_and_converts_audio(rings):
    video, audio = rings
    pump = MediaPump(worker.MediaRingReader(video.name), worker.MediaRingReader(audio.name))
    for n in range(3):
        video.write(bytes([n]) * FRAME, timestamp_ns=n * 1000, meta=W << 16 | H)
    audio.write(struct.pack("<4f", 0.5, 0.5, -0.5, -0.5), meta=2 | 2 << 16)
    frame, chunks = pump.drain()
    assert frame.payload == bytes([2]) * FRAME and frame.timestamp_us == 2
    assert pump.skipped_video == 2
    assert [c.frames for c in chunks] == [2]
    assert np.frombuffer(chunks[0].pcm, dtype=np.int16).tolist() == [16383, -16383] * 2
    assert pump.drain() == (None, [])


def test_pump_counts_malformed_items(rings):
    video, audio = rings
    pump = MediaPump(worker.MediaRingReader(video.name), worker.MediaRingReader(audio.name))
    video.write(b"\0" * 10, meta=W << 16 | H)
    audio.write(b"\0" * 8, meta=4 | 2 << 16)  # claims 4 frames, carries 1
    audio.write(b"\0" * 8, meta=1 | 1 << 16)  # mono is not what the sidecar writes
    assert pump.drain() == (None, [])
    assert pump.bad_items == 3


class _FakeSession:
    instances: list[_FakeSession] = []
    fail = False

    def __init__(self, request, out):
        self.request = request
        self.video: list = []
        self.audio: list = []
        self.closed = asyncio.Event()
        self.disconnected = False
        _FakeSession.instances.append(self)

    async def connect(self):
        if _FakeSession.fail:
            raise ConnectionError("no server")

    def send_video(self, item):
        self.video.append(item)

    async def send_audio(self, item):
        self.audio.append(item)

    async def close(self):
        self.disconnected = True


class _Stdin:
    """Feeds lines on demand so the test controls when ``stop`` arrives."""

    def __init__(self):
        import queue

        self._lines: queue.Queue = queue.Queue()

    def push(self, message):
        self._lines.put(message if message is None else json.dumps(message) + "\n")

    def __iter__(self):
        while (line := self._lines.get()) is not None:
            yield line


def _run(stdin, monkeypatch, until=None):
    _FakeSession.instances.clear()
    monkeypatch.setattr(worker, "_LiveKitSession", _FakeSession)
    output = io.StringIO()

    async def main():
        task = asyncio.create_task(worker.run(worker._Output(output), stdin))
        if until is not None:
            await until()
        return await asyncio.wait_for(task, 5)

    code = asyncio.run(main())
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


def test_worker_publishes_ring_media_until_stopped(rings, monkeypatch):
    video, audio = rings
    stdin = _Stdin()
    stdin.push(_request(video_ring=video.name, audio_ring=audio.name, width=W, height=H))

    async def feed_then_stop():
        while not _FakeSession.instances:
            await asyncio.sleep(0.005)
        video.write(b"\1" * FRAME, timestamp_ns=5000, meta=W << 16 | H)
        audio.write(struct.pack("<2f", 0.1, 0.1), meta=1 | 2 << 16)
        session = _FakeSession.instances[0]
        while not (session.video and session.audio):
            await asyncio.sleep(0.005)
        stdin.push({"command": "stop"})

    code, messages = _run(stdin, monkeypatch, feed_then_stop)
    assert code == 0
    assert messages[:3] == [{"ready": 1}, {"state": "connecting"}, {"state": "publishing"}]
    assert _FakeSession.instances[0].disconnected


def test_worker_exits_when_stdin_closes(rings, monkeypatch):
    video, audio = rings
    stdin = _Stdin()
    stdin.push(_request(video_ring=video.name, audio_ring=audio.name))

    async def close_stdin():
        while not _FakeSession.instances:
            await asyncio.sleep(0.005)
        stdin.push(None)

    code, _ = _run(stdin, monkeypatch, close_stdin)
    assert code == 0 and _FakeSession.instances[0].disconnected


def test_worker_reports_a_room_disconnect(rings, monkeypatch):
    video, audio = rings
    stdin = _Stdin()
    stdin.push(_request(video_ring=video.name, audio_ring=audio.name))

    async def drop_room():
        while not _FakeSession.instances:
            await asyncio.sleep(0.005)
        _FakeSession.instances[0].closed.set()

    code, messages = _run(stdin, monkeypatch, drop_room)
    assert code == 3 and messages[-1] == {"error": "disconnected"}


@pytest.mark.parametrize(("message", "error"), [
    ({"command": "launch"}, "invalid_command"),
    (_request(width=7), "invalid_request"),
    (_request(video_ring="psm_missing_ring", audio_ring="psm_missing_ring"), "rings_unavailable"),
])
def test_worker_rejects_bad_starts(message, error, monkeypatch):
    stdin = _Stdin()
    stdin.push(message)
    code, messages = _run(stdin, monkeypatch)
    assert code == 1 and messages[-1] == {"error": error}


def test_worker_reports_connect_failure(rings, monkeypatch):
    video, audio = rings
    stdin = _Stdin()
    stdin.push(_request(video_ring=video.name, audio_ring=audio.name))
    monkeypatch.setattr(_FakeSession, "fail", True)
    code, messages = _run(stdin, monkeypatch)
    assert code == 2 and messages[-1] == {"error": "connect_failed"}
