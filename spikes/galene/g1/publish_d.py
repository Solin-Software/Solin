"""G1 path D: libobs encodes video (x264), whippub sends to Galene over WHIP.

Spike code, not product code. Headless libobs renders the program on a 1080p
canvas, outputs 720p, and encodes it with x264 into a null output. A packet
callback hands every encoded video packet to `whippub -stdin`.

Audio, `--audio pcm` (default): libobs' raw program mix goes to whippub as PCM,
and whippub encodes it with libopus with in-band FEC (libobs' Opus encoder has
no FEC setting). Video is then stamped with the packet's system time
(`sys_dts_usec`), the same clock as the raw audio timestamps. `--audio libobs`
sends libobs' own Opus packets with encoder pts instead.

    .venv/bin/python spikes/galene/g1/publish_d.py --webcam /dev/video0 \
        --mic alsa_input.usb-046d_Brio_105_2445ZBE4ARA8-02.mono-fallback
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import psutil

import pylibobs as ob
from pylibobs import AudioEncoder, OBSContext, OBSData, Output, Scene, Source, VideoEncoder
from pylibobs._ffi import ffi, get_lib

HERE = Path(__file__).resolve().parent
DEFAULT_CLIP = HERE.parent.parent / "hosted-meetings" / "s1_publish" / "media" / "testclip.mp4"
WHIPPUB = HERE / "whippub" / "whippub"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8443/group/g1/.whip")
    p.add_argument("--token", default="")
    p.add_argument("--sr", choices=("pts", "send"), default="pts", help="whippub sender-report clock")
    p.add_argument("--audio", choices=("pcm", "libobs"), default="pcm",
                   help="pcm: whippub encodes Opus with FEC; libobs: libobs' Opus packets")
    p.add_argument("--opus-loss", type=int, default=10, help="pcm: expected loss %% for Opus FEC")
    p.add_argument("--no-fec", action="store_true", help="pcm: disable Opus in-band FEC")
    p.add_argument("--clip", type=Path, default=DEFAULT_CLIP)
    p.add_argument("--webcam", default="", help="V4L2 device (e.g. /dev/video0) instead of the clip")
    p.add_argument("--mic", default="", help="PulseAudio/PipeWire source name for the program audio")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--bitrate", type=int, default=1700, help="video kbps")
    p.add_argument("--x264opts", default="bframes=0 scenecut=0", help="extra x264 options")
    p.add_argument("--duration", type=float, default=0, help="seconds; 0 = until Ctrl+C")
    p.add_argument("--interval", type=float, default=10.0, help="stats interval, seconds")
    p.add_argument("--out", type=Path, default=None, help="JSON-lines stats file")
    return p.parse_args()


def build_program(args: argparse.Namespace) -> tuple:
    scene = Scene.create("program")
    sources = []
    if args.webcam:
        # Same settings shape as Solin's camera_source: MJPEG 1080p30.
        mjpg = sum(ord(c) << (8 * i) for i, c in enumerate("MJPG"))
        sources.append(Source.create("v4l2_input", "camera", OBSData({
            "device_id": args.webcam, "pixelformat": mjpg,
            "resolution": (1920 << 32) | 1080, "framerate": (1 << 32) | args.fps,
        })))
    else:
        sources.append(Source.create("ffmpeg_source", "clip", OBSData({
            "local_file": str(args.clip), "is_local_file": True, "looping": True,
        })))
    if args.mic:
        sources.append(Source.create("pulse_input_capture", "mic", OBSData({"device_id": args.mic})))
    for source in sources:
        scene.add(source)
    get_lib().obs_set_output_source(0, scene.as_source()._ptr)
    return scene, sources


def main() -> None:
    args = parse_args()
    pub = subprocess.Popen(
        [str(WHIPPUB), "-stdin", "-sr", args.sr, "-url", args.url,
         f"-opus-loss={args.opus_loss}", f"-opus-fec={'false' if args.no_fec else 'true'}"]
        + (["-token", args.token] if args.token else []),
        stdin=subprocess.PIPE,
    )
    pipe = pub.stdin
    lock = threading.Lock()
    first_pts: dict[int, int] = {}
    counts = {"video": 0, "audio": 0, "video_bytes": 0, "keyframes": 0, "write_errors": 0}

    def send(kind: int, pts_ns: int, data: bytes) -> None:
        try:
            with lock:
                pipe.write(struct.pack(">BqI", kind, pts_ns, len(data)) + data)
                pipe.flush()
        except OSError:
            counts["write_errors"] += 1

    @ffi.callback("void(void *, struct encoder_packet *)")
    def on_packet(_param, pkt) -> None:
        try:
            is_video = pkt.type == 1
            if args.audio == "pcm":
                if not is_video:
                    return          # libobs' Opus packets are unused in pcm mode
                # sys_dts_usec keeps system time (the output rebases dts_usec); bframes=0 so dts == pts
                pts_ns = pkt.sys_dts_usec * 1000
            else:
                pts_ns = pkt.pts * pkt.timebase_num * 1_000_000_000 // pkt.timebase_den
            if pkt.type not in first_pts:
                first_pts[pkt.type] = pts_ns
                print(f"first {'video' if is_video else 'audio'} pts {pts_ns / 1e6:.1f} ms", flush=True)
            if is_video:
                counts["video"] += 1
                counts["video_bytes"] += pkt.size
                counts["keyframes"] += pkt.keyframe
            else:
                counts["audio"] += 1
            send(1 if is_video else 0, pts_ns, bytes(ffi.buffer(pkt.data, pkt.size)))
        except Exception as e:  # noqa: BLE001 - never raise into libobs
            print(f"packet callback: {e}", flush=True)

    def on_pcm(_mix, planes, frames, ts_ns) -> None:
        left = np.frombuffer(planes[0], np.float32)[:frames]
        right = np.frombuffer(planes[1], np.float32)[:frames] if planes[1] else left
        counts["audio"] += 1
        send(2, ts_ns, np.column_stack((left, right)).astype("<f4").tobytes())

    with OBSContext(locale="en-US") as ctx:
        ctx.set_video(width=1920, height=1080, fps_num=args.fps, fps_den=1,
                      output_width=args.width, output_height=args.height)
        ctx.set_audio(samples_per_sec=48000)
        ctx.load_modules()
        keep = build_program(args)

        venc = VideoEncoder.create("obs_x264", "program_h264", {
            "rate_control": "CBR", "bitrate": args.bitrate, "keyint_sec": 2,
            "preset": "veryfast", "tune": "zerolatency", "profile": "baseline",
            "x264opts": args.x264opts, "repeat_headers": True,
        })
        aenc = AudioEncoder.create("ffmpeg_opus", "program_opus", {"bitrate": 64})
        out = Output.create("null_output", "whip_feed")
        out.set_video_encoder(venc)
        out.set_audio_encoder(aenc, 0)
        get_lib().obs_output_add_packet_callback(out._ptr, on_packet, ffi.NULL)
        pcm_cb = ob.add_raw_audio_callback(on_pcm, channels=2) if args.audio == "pcm" else None
        if not out.start():
            raise SystemExit(f"null_output failed to start: {out.last_error}")

        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        me = psutil.Process()
        me.cpu_percent()
        t0 = time.monotonic()
        last = dict(counts)
        log = args.out.open("a") if args.out else None
        while not stop.wait(args.interval):
            if pub.poll() is not None:
                print("whippub exited", flush=True)
                break
            row = {
                "t": round(time.monotonic() - t0),
                "cpu_cores": round(me.cpu_percent() / 100, 2),
                "rss_mb": round(me.memory_info().rss / 2**20),
                "video_fps": round((counts["video"] - last["video"]) / args.interval, 1),
                "video_kbps": round((counts["video_bytes"] - last["video_bytes"]) * 8 / args.interval / 1000),
                "audio_pps": round((counts["audio"] - last["audio"]) / args.interval, 1),
                "keyframes": counts["keyframes"] - last["keyframes"],
                "write_errors": counts["write_errors"],
                "lagged_frames": int(get_lib().obs_get_lagged_frames()),
            }
            last = dict(counts)
            print(json.dumps(row), flush=True)
            if log:
                log.write(json.dumps(row) + "\n")
                log.flush()
            if args.duration and row["t"] >= args.duration:
                break

        out.stop()
        if pcm_cb is not None:
            ob.remove_raw_audio_callback(pcm_cb)
        get_lib().obs_output_remove_packet_callback(out._ptr, on_packet, ffi.NULL)
        del keep
    pipe.close()
    pub.send_signal(signal.SIGINT)
    pub.wait(5)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
