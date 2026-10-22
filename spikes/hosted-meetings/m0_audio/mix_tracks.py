"""M0: does a media source's audio reach a raw mix callback, per monitoring type and mixer track?

Headless libobs, the S1 test clip (1 kHz beeps), raw audio callbacks on mix 0 (track 1)
and mix 5 (track 6). Reports the RMS each mix receives for:
  A. MONITOR_ONLY (current Solin media setting), default mixers
  B. MONITOR_AND_OUTPUT, mixers = track 6 only (proposed meeting track)
"""

import time
from pathlib import Path

import numpy as np

import pylibobs as ob
from pylibobs import OBSContext, OBSData, Scene, Source
from pylibobs._ffi import get_lib

CLIP = Path(__file__).resolve().parents[1] / "s1_publish" / "media" / "testclip.mp4"
MONITOR_ONLY, MONITOR_AND_OUTPUT = 1, 2
TRACK6 = 1 << 5


def run_case(label: str, monitoring: int, mixers: int | None) -> None:
    energy = {0: [], 5: []}

    def make_cb(mix):
        def cb(_mix, planes, frames, _ts):
            x = np.frombuffer(planes[0], dtype=np.float32)
            energy[mix].append(float(np.sqrt(np.mean(x * x))) if x.size else 0.0)
        return cb

    scene = Scene.create(f"program-{label}")
    src = Source.create("ffmpeg_source", f"clip-{label}",
                        OBSData({"local_file": str(CLIP), "is_local_file": True, "looping": True}))
    get_lib().obs_source_set_monitoring_type(src._ptr, monitoring)
    if mixers is not None:
        src.audio_mixers = mixers
    scene.add(src)
    get_lib().obs_set_output_source(0, scene.as_source()._ptr)
    cbs = {m: ob.add_raw_audio_callback(make_cb(m), mix_idx=m, channels=2) for m in (0, 5)}
    time.sleep(4)
    for m, h in cbs.items():
        ob.remove_raw_audio_callback(h, mix_idx=m)
    get_lib().obs_set_output_source(0, get_lib().obs_scene_get_source(get_lib().obs_scene_create(b"empty")))
    peak = {m: round(max(v) if v else 0.0, 4) for m, v in energy.items()}
    print(f"{label}: monitoring={monitoring} mixers={src.audio_mixers:#04x} -> "
          f"track1(mix0) peak RMS {peak[0]}, track6(mix5) peak RMS {peak[5]}", flush=True)


with OBSContext(locale="en-US") as ctx:
    ctx.set_video(width=640, height=360, fps_num=30, fps_den=1)
    ctx.set_audio(samples_per_sec=48000)
    ctx.load_modules()
    ok = get_lib().obs_set_audio_monitoring_device(b"Default", b"default")
    print("monitoring device set:", bool(ok), flush=True)
    run_case("A-monitor-only", MONITOR_ONLY, None)
    run_case("B-meeting-track", MONITOR_AND_OUTPUT, TRACK6)
    run_case("C-no-monitoring-default", 0, None)
