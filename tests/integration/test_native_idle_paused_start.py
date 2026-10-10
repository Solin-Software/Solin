"""Qualify actual idle pixels across static previews and live transport edges.

Run with ``python -m pytest tests/integration/test_native_idle_paused_start.py -q -s``.
The isolated child samples the shared public scene in OBS main-render callbacks.
Transport position and readiness flags never substitute for pixel assertions.
"""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, TypedDict

import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        importlib.util.find_spec("pylibobs") is None or not shutil.which("ffmpeg"),
        reason="Native libobs and FFmpeg fixture tools required",
    ),
]

_PROCESS_TIMEOUT = 30
_SCENARIO_TIMEOUT = 24
_WIDTH, _HEIGHT, _FPS = 320, 180, 30
_READ_WIDTH, _READ_HEIGHT = 160, 90


def _remaining(deadline: float, maximum: float = 5.0) -> float:
    remaining = min(maximum, deadline - time.monotonic())
    assert remaining > 0, "Native paused-start scenario exceeded its deadline"
    return remaining


def _video(path: Path, deadline: float) -> None:
    """Red opening frames, then blue; no loop boundary during a phase."""
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:r=30:d=0.2",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=30:d=5.8",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        timeout=_remaining(deadline),
        check=True,
    )


def _decoder_count() -> int:
    """Borrow native enumeration pointers; owned wrappers remove live sources."""
    from pylibobs._ffi import ffi, get_lib

    lib: Any = get_lib()
    count = 0

    @ffi.callback("bool(void *, obs_source_t *)")
    def collect(_data, source):
        nonlocal count
        if ffi.string(lib.obs_source_get_id(source)) == b"ffmpeg_source":
            count += 1
        return True

    lib.obs_enum_all_sources(collect, ffi.NULL)
    return count


class _FrameSample(TypedDict):
    color: str
    center_bgra: list[int]
    sha256: str
    border_alpha: int
    tick: int


class _Capture:
    def __init__(self, runtime, owner, source, channel):
        self.runtime, self.owner, self.source, self.channel = runtime, owner, source, channel
        self.condition = threading.Condition()
        self.phase = ""
        self.pending = None
        self.frames: dict[str, list[_FrameSample]] = {}
        self.errors: list[str] = []
        self.last_tick = -1
        self.max_decoders = 0
        self.demand = (False, False)
        self.callback = runtime.ob.add_main_render_callback(self.render)

    def begin(self, phase: str, actions: list[tuple[bool, bool]], *, source=None) -> None:
        with self.condition:
            self.frames[phase] = []
            self.pending = phase, actions, source

    def render(self, _width: int, _height: int) -> None:
        from pylibobs._ffi import get_lib
        from solin.core.media.obs_source_render import render_source_to_bgra

        try:
            lib: Any = get_lib()
            tick = lib.obs_get_video_frame_time()
            with self.condition:
                if tick == self.last_tick:
                    return
                self.last_tick = tick
                if self.pending is not None:
                    self.phase, actions, source = self.pending
                    self.pending = None
                    if source is not None:
                        self.source = source
                        self.runtime.set_channel_source(self.channel, source)
                    # The first observation is in the same render tick as the
                    # demand edge, before queued media actions can run next tick.
                    for demanded, live in actions:
                        self.demand = (demanded, live)
                        self.owner.set_demand(demanded, live=live)
                if not self.phase:
                    return
                # Match sidecar reconciliation while a cold decoder is pending;
                # a constant requested live flag must eventually start it.
                self.owner.set_demand(self.demand[0], live=self.demand[1])
                frame = render_source_to_bgra(
                    self.source,
                    _READ_WIDTH,
                    _READ_HEIGHT,
                    canvas_width=_WIDTH,
                    canvas_height=_HEIGHT,
                )
                assert frame is not None, "Public idle scene has no native texture"
                pixels, stride = frame
                center = (_READ_HEIGHT // 2) * stride + (_READ_WIDTH // 2) * 4
                blue, green, red, alpha = pixels[center : center + 4]
                color = "other"
                if red > 200 and green < 40 and blue < 40:
                    color = "red"
                elif blue > 200 and red < 40 and green < 40:
                    color = "blue"
                self.max_decoders = max(self.max_decoders, _decoder_count())
                sample: _FrameSample = {
                    "color": color,
                    "center_bgra": [blue, green, red, alpha],
                    "sha256": hashlib.sha256(pixels).hexdigest(),
                    "border_alpha": pixels[3],
                    "tick": tick,
                }
                if len(self.frames[self.phase]) < 240:
                    self.frames[self.phase].append(sample)
                self.condition.notify_all()
        except Exception as error:  # noqa: BLE001 - report native callback failures in the child
            with self.condition:
                self.errors.append(str(error))
                self.condition.notify_all()

    def wait(self, phase: str, deadline: float, *, frames: int = 1, blue: bool = False) -> None:
        with self.condition:

            def ready():
                observations = self.frames[phase]
                return self.errors or (
                    len(observations) >= frames
                    and (not blue or any(frame["color"] == "blue" for frame in observations))
                )

            assert self.condition.wait_for(ready, timeout=_remaining(deadline)), (
                f"Phase {phase!r} did not render/resume: {self.frames[phase]}"
            )
            assert not self.errors, self.errors

    def close(self):
        self.runtime.ob.remove_main_render_callback(self.callback)


def _scenario(directory: Path) -> dict:
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import shutdown as shutdown_readback
    from solin.core.scenes.idle import IdleScreenState
    from solin.core.scenes.libobs_idle_source import LibobsIdleSource

    deadline = time.monotonic() + _SCENARIO_TIMEOUT
    video = directory / "red-opening-blue-late.mp4"
    _video(video, deadline)
    report: dict[str, Any] = {"fixture": "red 0-200 ms, blue 200-6000 ms"}
    capture = None
    try:
        with ExitStack() as stack:
            runtime = ObsRuntime()
            stack.callback(runtime.shutdown)
            runtime.ensure_started(width=_WIDTH, height=_HEIGHT, fps=_FPS)
            stack.callback(shutdown_readback)
            owner = LibobsIdleSource(runtime)
            stack.callback(owner.close)
            owner.apply(
                IdleScreenState(1, str(video)),
                int((time.monotonic() + _remaining(deadline)) * 1000),
            )
            shared = owner.source
            consumers = []
            for name in ("idle-consumer-A", "idle-consumer-B"):
                scene = runtime.ob.Scene.create_private(name)
                stack.callback(scene.release)
                item = scene.add(shared)
                stack.callback(item.release)
                stack.callback(item.remove)
                consumers.append(scene.as_source())
            channel = runtime.acquire_channel()
            stack.callback(runtime.release_channel, channel)
            runtime.set_channel_source(channel, consumers[0])
            capture = _Capture(runtime, owner, consumers[0], channel)
            stack.callback(capture.close)

            capture.begin("initial_preview", [(True, False)])
            capture.wait("initial_preview", deadline, frames=20)
            capture.begin("first_live", [(True, True)])
            capture.wait("first_live", deadline, frames=12, blue=True)
            capture.begin("second_live", [(True, True)])
            capture.wait("second_live", deadline, frames=8)
            capture.begin("live_scene_handoff", [(True, True)], source=consumers[1])
            capture.wait("live_scene_handoff", deadline, frames=8)
            capture.begin("preview_after_live", [(True, False)])
            capture.wait("preview_after_live", deadline, frames=20)
            capture.begin("returning_live", [(True, True)])
            capture.wait("returning_live", deadline, frames=12, blue=True)
            capture.begin("rapid_leave", [(True, False)])
            capture.wait("rapid_leave", deadline)
            capture.begin("rapid_return", [(True, True)])
            capture.wait("rapid_return", deadline, frames=12, blue=True)
            capture.begin("no_consumers", [(False, False)])
            capture.wait("no_consumers", deadline, frames=10)

            with capture.condition:
                report["phases"] = {key: list(value) for key, value in capture.frames.items()}
                report["max_native_decoders"] = capture.max_decoders
            checks = {}
            for phase in ("initial_preview", "preview_after_live", "rapid_leave", "no_consumers"):
                frames = report["phases"][phase]
                checks[f"{phase}_first_frame_red"] = frames[0]["color"] == "red"
                checks[f"{phase}_every_frame_red"] = all(
                    frame["color"] == "red" for frame in frames
                )
                checks[f"{phase}_immutable_pixels"] = (
                    len({frame["sha256"] for frame in frames}) == 1
                )
            for phase in ("first_live", "returning_live", "rapid_return"):
                frames = report["phases"][phase]
                checks[f"{phase}_first_frame_red"] = frames[0]["color"] == "red"
                checks[f"{phase}_motion_resumes"] = any(
                    frame["color"] == "blue" for frame in frames
                )
            for phase in ("second_live", "live_scene_handoff"):
                checks[f"{phase}_no_restart"] = all(
                    frame["color"] == "blue" for frame in report["phases"][phase]
                )
            checks["one_native_decoder"] = capture.max_decoders <= 1
            checks["contained_alpha_preserved"] = all(
                frame["border_alpha"] == 0 and frame["center_bgra"][3] == 255
                for frames in report["phases"].values()
                for frame in frames
            )
            report["checks"] = checks
            failures = [name for name, passed in checks.items() if not passed]
            assert not failures, f"Native paused-start pixel contract failed: {failures}"
    finally:
        if capture is not None:
            with capture.condition:
                report["phases"] = {key: list(value) for key, value in capture.frames.items()}
                report["callback_errors"] = list(capture.errors)
        (directory / "paused-start.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def test_native_idle_static_previews_first_live_frame_and_rapid_return(tmp_path, request):
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=_PROCESS_TIMEOUT,
        check=False,
    )
    report_path = tmp_path / "paused-start.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        request.node.user_properties.append(("native_idle_paused_start", json.dumps(report)))
        print("NATIVE_IDLE_PAUSED_START", json.dumps(report.get("checks", {}), sort_keys=True))
    assert result.returncode == 0, "\n".join((result.stdout + result.stderr).splitlines()[-60:])
    assert report_path.is_file(), "Native child did not publish pixel observations"


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    _scenario(Path(sys.argv[1]))
