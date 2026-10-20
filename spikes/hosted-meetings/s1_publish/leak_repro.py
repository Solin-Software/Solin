"""Minimal reproducer: publisher RSS growth with the VAAPI video encoder.

Only the `livekit` SDK: synthetic I420 frames (a moving gradient + noise block)
pushed into a VideoSource at a fixed rate, published to a LiveKit server. Logs
process RSS and encoded bytes as JSON lines.

    docker run --rm --network host livekit/livekit-server --dev --bind 0.0.0.0
    python leak_repro.py --encoder vaapi            # grows
    python leak_repro.py --encoder software         # flat
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import time
from importlib.metadata import version

import numpy as np
import psutil
from livekit import api, rtc

ENCODERS = {
    "software": rtc.VideoEncoderBackend.ENCODER_BACKEND_SOFTWARE,
    "vaapi": rtc.VideoEncoderBackend.ENCODER_BACKEND_VAAPI,
    "hardware": rtc.VideoEncoderBackend.ENCODER_BACKEND_HARDWARE,
    "nvenc": rtc.VideoEncoderBackend.ENCODER_BACKEND_NVENC,
}
CODECS = {"h264": rtc.VideoCodec.H264, "vp8": rtc.VideoCodec.VP8}


def make_frames(w: int, h: int, count: int) -> list[bytes]:
    """Precompute a loop of I420 frames so the producer itself allocates nothing."""
    rng = np.random.default_rng(1)
    xs = np.arange(w, dtype=np.uint16)
    frames = []
    for i in range(count):
        y = ((xs[None, :] + np.arange(h, dtype=np.uint16)[:, None] + i * 8) % 256).astype(np.uint8)
        y[h // 4 : h // 2, w // 4 : w // 2] = rng.integers(0, 256, (h // 4, w // 4), dtype=np.uint8)
        uv = np.full((h // 2) * (w // 2), 128 + (i % 64), dtype=np.uint8).tobytes()
        frames.append(y.tobytes() + uv + uv)
    return frames


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--room", default="repro")
    p.add_argument("--encoder", choices=ENCODERS, default="vaapi")
    p.add_argument("--codec", choices=CODECS, default="h264")
    p.add_argument("--no-simulcast", action="store_true")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--max-bitrate", type=int, default=1_700_000)
    p.add_argument("--duration", type=float, default=600)
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--out", default="")
    p.add_argument("--trim", action="store_true", help="call glibc malloc_trim(0) before each sample")
    a = p.parse_args()

    out = open(a.out, "a") if a.out else None

    def log(rec: dict) -> None:
        line = json.dumps(rec)
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    frames = make_frames(a.width, a.height, 60)
    proc = psutil.Process(os.getpid())
    token = (
        api.AccessToken("devkey", "secret").with_identity(f"repro-{a.encoder}")
        .with_grants(api.VideoGrants(room_join=True, room=a.room)).to_jwt()
    )
    room = rtc.Room()
    await room.connect(a.url, token, options=rtc.RoomOptions(auto_subscribe=False, dynacast=False))
    source = rtc.VideoSource(a.width, a.height)
    track = rtc.LocalVideoTrack.create_video_track("synthetic", source)
    await room.local_participant.publish_track(track, rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA, simulcast=not a.no_simulcast,
        video_codec=CODECS[a.codec], video_encoder=ENCODERS[a.encoder],
        video_encoding=rtc.VideoEncoding(max_bitrate=a.max_bitrate, max_framerate=a.fps),
    ))
    log({"event": "start", "encoder": a.encoder, "codec": a.codec, "simulcast": not a.no_simulcast,
         "size": f"{a.width}x{a.height}", "fps": a.fps, "max_bitrate": a.max_bitrate, "livekit": version("livekit"),
         "python": platform.python_version(), "os": platform.platform()})

    async def stats() -> tuple[int, str]:
        total, impl = 0, ""
        for s in await track.get_stats():
            if s.WhichOneof("stats") == "outbound_rtp":
                total += s.outbound_rtp.sent.bytes_sent
                impl = s.outbound_rtp.outbound.encoder_implementation or impl
        return total, impl

    period = 1 / a.fps
    start = next_tick = time.monotonic()
    next_log = start + a.interval
    i = 0
    while time.monotonic() - start < a.duration:
        source.capture_frame(rtc.VideoFrame(a.width, a.height, rtc.VideoBufferType.I420, frames[i % len(frames)]),
                             timestamp_us=int((time.monotonic() - start) * 1e6))
        i += 1
        next_tick += period
        await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        if time.monotonic() >= next_log:
            next_log += a.interval
            trimmed_from = None
            if a.trim:
                import ctypes
                trimmed_from = round(proc.memory_info().rss / 1e6, 1)
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            sent, impl = await stats()
            log({"event": "stats", "t": round(time.monotonic() - start), "frames": i,
                 "rss_mb": round(proc.memory_info().rss / 1e6, 1), "rss_before_trim_mb": trimmed_from, "sent_mb": round(sent / 1e6, 1), "encoder_impl": impl})
    await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
