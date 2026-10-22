"""Measure the flash/beep A/V offset in libobs' own timestamps (raw callbacks).

Spike code. Tells whether an A/V offset seen in the browser already exists in
libobs (capture latency) or is added by the transport. Point the webcam at the
running probe page (it flashes and beeps at the same instant every 2 s), then:

    .venv/bin/python spikes/galene/g1/raw_check.py --webcam /dev/video0 --mic <source> --seconds 40
"""

from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path

import numpy as np

import pylibobs as ob
from pylibobs import OBSContext

from publish_d import build_program

RATE = 48000


def onsets(ts: np.ndarray, signal: np.ndarray, debounce_ns: int = 1_000_000_000) -> list[int]:
    lo, hi = np.percentile(signal, 5), np.percentile(signal, 99.5)
    above = signal >= (lo + hi) / 2
    out, last = [], -10**18
    for i in np.flatnonzero(above[1:] & ~above[:-1]) + 1:
        if ts[i] - last > debounce_ns:
            out.append(int(ts[i]))
            last = ts[i]
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--webcam", default="/dev/video0")
    p.add_argument("--mic", default="")
    p.add_argument("--clip", type=Path, default=None)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--seconds", type=float, default=40)
    args = p.parse_args()

    video = []           # (ts_ns, mean luma of the centre)
    audio = []           # (ts_ns of first sample, mono float32 chunk)

    def on_video(planes, linesizes, w, h, _fmt, ts_ns):
        y = np.frombuffer(planes[0], np.uint8)[: int(linesizes[0]) * h].reshape(h, int(linesizes[0]))[:, :w]
        video.append((ts_ns, float(y[h // 4: 3 * h // 4, w // 4: 3 * w // 4].mean())))

    def on_audio(_mix, planes, frames, ts_ns):
        audio.append((ts_ns, np.frombuffer(planes[0], np.float32)[:frames].copy()))

    with OBSContext(locale="en-US") as ctx:
        ctx.set_video(width=1920, height=1080, fps_num=args.fps, fps_den=1)
        ctx.set_audio(samples_per_sec=RATE)
        ctx.load_modules()
        keep = build_program(args)
        vcb = ob.add_raw_video_callback(on_video, format=int(ob.VideoFormat.I420), width=320, height=180)
        acb = ob.add_raw_audio_callback(on_audio, channels=1)
        time.sleep(args.seconds)
        ob.remove_raw_video_callback(vcb)
        ob.remove_raw_audio_callback(acb)
        del keep

    vts = np.array([t for t, _ in video], np.int64)
    luma = np.array([y for _, y in video])
    # Audio: per-sample timestamps, 1 kHz band via FFT mask, 5 ms envelope.
    samples = np.concatenate([c for _, c in audio])
    ats = np.concatenate([t + (np.arange(len(c)) * 1e9 / RATE).astype(np.int64) for t, c in audio])
    spec = np.fft.rfft(samples)
    f = np.fft.rfftfreq(len(samples), 1 / RATE)
    spec[(f < 900) | (f > 1100)] = 0
    band = np.abs(np.fft.irfft(spec, len(samples)))
    win = RATE // 200
    n = len(band) // win * win
    env, ets = band[:n].reshape(-1, win).max(axis=1), ats[:n:win]

    v, a = onsets(vts, luma), onsets(ets, env)
    offsets = []
    for tv in v:
        ta = min(a, key=lambda x: abs(x - tv), default=None)
        if ta is not None and abs(ta - tv) < 1e9:
            offsets.append(round((ta - tv) / 1e6))
    print(f"{len(video)} frames, {len(v)} flashes, {len(a)} beeps, {len(offsets)} pairs")
    if offsets:
        print(f"audio - video in libobs timestamps: median {statistics.median(offsets)} ms "
              f"(min {min(offsets)}, max {max(offsets)}); negative = audio early")


if __name__ == "__main__":
    main()
