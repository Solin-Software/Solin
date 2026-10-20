"""S1 path B: libobs main mix -> raw I420/PCM callbacks -> LiveKit Python SDK.

Spike code, not product code. Starts a headless libobs with a looping test clip as
the program, mirrors the main mix into a LiveKit room as one simulcast video track
plus one audio track, and logs CPU/encoder stats as JSON lines.

Run a local server first:
    docker run --rm --network host livekit/livekit-server --dev --bind 0.0.0.0

Then:
    .venv/bin/python s1_publish/publish_b.py --encoder software --codec vp8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import threading
import time
from pathlib import Path

import numpy as np
import psutil
from livekit import api, rtc

import pylibobs as ob
from pylibobs import OBSContext, OBSData, Scene, Source
from pylibobs._ffi import get_lib

HERE = Path(__file__).resolve().parent
DEFAULT_CLIP = HERE / "media" / "testclip.mp4"

ENCODERS = {
    "auto": rtc.VideoEncoderBackend.ENCODER_BACKEND_AUTO,
    "software": rtc.VideoEncoderBackend.ENCODER_BACKEND_SOFTWARE,
    "hardware": rtc.VideoEncoderBackend.ENCODER_BACKEND_HARDWARE,
    "vaapi": rtc.VideoEncoderBackend.ENCODER_BACKEND_VAAPI,
    "nvenc": rtc.VideoEncoderBackend.ENCODER_BACKEND_NVENC,
}
CODECS = {"vp8": rtc.VideoCodec.VP8, "h264": rtc.VideoCodec.H264, "vp9": rtc.VideoCodec.VP9}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="ws://127.0.0.1:7880")
    p.add_argument("--api-key", default="devkey")
    p.add_argument("--api-secret", default="secret")
    p.add_argument("--room", default="s1")
    p.add_argument("--clip", type=Path, default=DEFAULT_CLIP)
    p.add_argument("--webcam", default="", help="V4L2 device (e.g. /dev/video0) instead of the clip")
    p.add_argument("--mic", default="", help="PulseAudio/PipeWire source name for the program audio")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--codec", choices=CODECS, default="vp8")
    p.add_argument("--encoder", choices=ENCODERS, default="software")
    p.add_argument("--max-bitrate", type=int, default=1_700_000, help="top layer, bps")
    p.add_argument("--no-simulcast", action="store_true")
    p.add_argument("--e2ee-key", default="", help="shared E2EE key; empty = off")
    p.add_argument("--baseline", action="store_true", help="libobs program only: no callbacks, no publish")
    p.add_argument("--no-video", action="store_true", help="no video callback/track")
    p.add_argument("--no-audio", action="store_true", help="no audio callback/track")
    p.add_argument("--no-livekit", action="store_true", help="run callbacks and build frames, but don't connect")
    p.add_argument("--tracemalloc", action="store_true", help="log top Python allocation growth")
    p.add_argument("--duration", type=float, default=120.0, help="seconds")
    p.add_argument("--interval", type=float, default=5.0, help="stats interval, seconds")
    p.add_argument("--out", type=Path, default=None, help="JSON-lines results file")
    return p.parse_args()


class ObsProgram:
    """Headless libobs with a looping clip as the program, mirrored via raw callbacks."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.ctx = OBSContext(locale="en-US")
        self.video_cb = None
        self.audio_cb = None
        self.video_frames = 0
        self.video_cb_seconds = 0.0
        self.audio_chunks = 0

    def start(self, on_video, on_audio) -> None:
        self.ctx.__enter__()
        # Solin renders a 1080p canvas; the callback asks libobs for a scaled I420 copy.
        self.ctx.set_video(width=1920, height=1080, fps_num=self.args.fps, fps_den=1)
        self.ctx.set_audio(samples_per_sec=48000)
        self.ctx.load_modules()
        scene = Scene.create("program")
        sources = []
        if self.args.webcam:
            # Same settings shape as Solin's camera_source: MJPEG 1080p30.
            mjpg = sum(ord(c) << (8 * i) for i, c in enumerate("MJPG"))
            sources.append(Source.create("v4l2_input", "camera", OBSData({
                "device_id": self.args.webcam, "pixelformat": mjpg,
                "resolution": (1920 << 32) | 1080, "framerate": (1 << 32) | self.args.fps,
            })))
        else:
            sources.append(Source.create(
                "ffmpeg_source",
                "clip",
                OBSData({"local_file": str(self.args.clip), "is_local_file": True, "looping": True}),
            ))
        if self.args.mic:
            sources.append(Source.create("pulse_input_capture", "mic", OBSData({"device_id": self.args.mic})))
        for source in sources:
            scene.add(source)
        get_lib().obs_set_output_source(0, scene.as_source()._ptr)
        self._keep = (scene, sources)

        w, h = self.args.width, self.args.height

        def video(planes, linesizes, _w, _h, _fmt, ts_ns):
            t0 = time.perf_counter()
            on_video(planes, linesizes, w, h, ts_ns)
            self.video_cb_seconds += time.perf_counter() - t0
            self.video_frames += 1

        def audio(_mix, planes, frames, ts_ns):
            self.audio_chunks += 1
            on_audio(planes, frames, ts_ns)

        if self.args.baseline:
            return
        if not self.args.no_video:
            self.video_cb = ob.add_raw_video_callback(video, format=int(ob.VideoFormat.I420), width=w, height=h)
        if not self.args.no_audio:
            self.audio_cb = ob.add_raw_audio_callback(audio, channels=2)

    def stop(self) -> None:
        if self.video_cb is not None:
            ob.remove_raw_video_callback(self.video_cb)
        if self.audio_cb is not None:
            ob.remove_raw_audio_callback(self.audio_cb)
        self.ctx.__exit__(None, None, None)


def tight_i420(planes, linesizes, w: int, h: int) -> bytes:
    """Pack possibly-strided I420 planes into one contiguous buffer."""
    dims = ((w, h), (w // 2, h // 2), (w // 2, h // 2))
    if all(int(linesizes[i]) == dims[i][0] for i in range(3)):
        return planes[0][: w * h] + planes[1][: (w // 2) * (h // 2)] + planes[2][: (w // 2) * (h // 2)]
    out = []
    for i, (pw, ph) in enumerate(dims):
        arr = np.frombuffer(planes[i], dtype=np.uint8)[: int(linesizes[i]) * ph]
        out.append(arr.reshape(ph, int(linesizes[i]))[:, :pw].tobytes())
    return b"".join(out)


async def main() -> None:
    args = parse_args()
    proc = psutil.Process(os.getpid())
    out = open(args.out, "a") if args.out else None

    def log(record: dict) -> None:
        line = json.dumps(record)
        print(line, flush=True)
        if out:
            out.write(line + "\n")
            out.flush()

    loop = asyncio.get_running_loop()
    room = rtc.Room()
    token = (
        api.AccessToken(args.api_key, args.api_secret)
        .with_identity("hall")
        .with_name("Kingdom Hall")
        .with_grants(api.VideoGrants(room_join=True, room=args.room))
        .to_jwt()
    )
    # Dynacast off: keep every simulcast layer encoding, the worst case for CPU.
    options = rtc.RoomOptions(auto_subscribe=False, dynacast=False)
    if args.e2ee_key:
        options.encryption = rtc.E2EEOptions(
            key_provider_options=rtc.KeyProviderOptions(shared_key=args.e2ee_key.encode())
        )
    use_livekit = not (args.baseline or args.no_livekit)
    if use_livekit:
        await room.connect(args.url, token, options=options)

    video_source = rtc.VideoSource(args.width, args.height)
    audio_source = rtc.AudioSource(48000, 2, queue_size_ms=200)
    video_track = rtc.LocalVideoTrack.create_video_track("program", video_source)
    audio_track = rtc.LocalAudioTrack.create_audio_track("program-audio", audio_source)

    vopts = rtc.TrackPublishOptions(
        source=rtc.TrackSource.SOURCE_CAMERA,
        simulcast=not args.no_simulcast,
        video_codec=CODECS[args.codec],
        video_encoder=ENCODERS[args.encoder],
        video_encoding=rtc.VideoEncoding(max_bitrate=args.max_bitrate, max_framerate=args.fps),
    )
    if use_livekit and not args.no_video:
        await room.local_participant.publish_track(video_track, vopts)
    if use_livekit and not args.no_audio:
        await room.local_participant.publish_track(
            audio_track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

    audio_q: queue.Queue[rtc.AudioFrame] = queue.Queue(maxsize=50)
    dropped = {"audio": 0, "video": 0}

    def on_video(planes, linesizes, w, h, ts_ns):
        frame = rtc.VideoFrame(w, h, rtc.VideoBufferType.I420, tight_i420(planes, linesizes, w, h))
        if not use_livekit:
            return
        try:
            video_source.capture_frame(frame, timestamp_us=ts_ns // 1000)
        except Exception:  # noqa: BLE001 - spike: count and keep going
            dropped["video"] += 1

    def on_audio(planes, frames, _ts_ns):
        left = np.frombuffer(planes[0], dtype=np.float32)
        right = np.frombuffer(planes[1], dtype=np.float32) if planes[1] else left
        pcm = np.empty(frames * 2, dtype=np.int16)
        pcm[0::2] = np.clip(left * 32767, -32768, 32767)
        pcm[1::2] = np.clip(right * 32767, -32768, 32767)
        frame = rtc.AudioFrame(pcm.tobytes(), 48000, 2, frames)
        if not use_livekit:
            return
        try:
            audio_q.put_nowait(frame)
        except queue.Full:
            dropped["audio"] += 1

    async def pump_audio():
        while True:
            try:
                frame = await loop.run_in_executor(None, audio_q.get, True, 0.5)
            except queue.Empty:
                continue
            await audio_source.capture_frame(frame)

    program = ObsProgram(args)
    await asyncio.to_thread(program.start, on_video, on_audio)
    pump = asyncio.create_task(pump_audio())

    if args.tracemalloc:
        import tracemalloc
        tracemalloc.start(10)
        snap0 = tracemalloc.take_snapshot()
    log({"event": "start", "baseline": args.baseline, "no_video": args.no_video, "no_audio": args.no_audio,
         "no_livekit": args.no_livekit, "webcam": args.webcam, "mic": args.mic, "codec": args.codec, "encoder": args.encoder, "simulcast": not args.no_simulcast,
         "size": f"{args.width}x{args.height}", "fps": args.fps, "e2ee": bool(args.e2ee_key),
         "cpus_allowed": len(proc.cpu_affinity())})

    proc.cpu_percent(None)
    started = time.monotonic()
    last_frames = 0
    try:
        while time.monotonic() - started < args.duration:
            await asyncio.sleep(args.interval)
            cpu_cores = proc.cpu_percent(None) / 100.0
            frames = program.video_frames
            fps = (frames - last_frames) / args.interval
            cb_ms = 1000 * program.video_cb_seconds / max(frames, 1)
            last_frames = frames
            layers = []
            try:
                for s in ([] if not use_livekit or args.no_video else await video_track.get_stats()):
                    if s.WhichOneof("stats") == "outbound_rtp":
                        o = s.outbound_rtp
                        layers.append({
                            "rid": o.outbound.rid,
                            "w": o.outbound.frame_width,
                            "h": o.outbound.frame_height,
                            "fps": round(o.outbound.frames_per_second, 1),
                            "kbytes": o.sent.bytes_sent // 1000,
                            "encoder": o.outbound.encoder_implementation,
                            "limit": o.outbound.quality_limitation_reason,
                        })
            except Exception as exc:  # noqa: BLE001 - stats shape differs across SDK versions
                layers = [{"error": repr(exc)}]
            py_growth = None
            if args.tracemalloc:
                import tracemalloc
                top = tracemalloc.take_snapshot().compare_to(snap0, "lineno")[:3]
                py_growth = {"traced_mb": tracemalloc.get_traced_memory()[0] // 1_000_000,
                             "top": [f"{st.traceback[0].filename.split('/')[-1]}:{st.traceback[0].lineno} +{st.size_diff // 1000}kB" for st in top]}
            log({"event": "stats", "py": py_growth, "t": round(time.monotonic() - started, 1), "cpu_cores": round(cpu_cores, 2),
                 "rss_mb": proc.memory_info().rss // 1_000_000, "obs_fps": fps, "cb_ms": round(cb_ms, 2),
                 "dropped": dict(dropped), "audio_chunks": program.audio_chunks, "layers": layers})
    finally:
        pump.cancel()
        await asyncio.to_thread(program.stop)
        await room.disconnect()
        if out:
            out.close()


if __name__ == "__main__":
    asyncio.run(main())
