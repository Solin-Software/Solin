"""Real libobs: I420 frames from a remote participant render with the right colours.

Runs in a subprocess (libobs is process-global). Pure red, green and blue are pushed as
BT.709 limited-range I420 through ``ObsFrameSource.push_i420`` and read back as BGRA, which
checks the hard-coded colour matrix and range against what libobs actually draws.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from queue import Empty, Queue

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(importlib.util.find_spec("pylibobs") is None, reason="libobs required"),
]
_REPORT = "PARTICIPANT_I420_REPORT="
W, H = 64, 48
# (Y, U, V) for BT.709 limited range, and the expected BGR.
_COLOURS = {
    "red": ((63, 102, 240), (0, 0, 255)),
    "green": ((173, 42, 26), (0, 255, 0)),
    "blue": ((32, 240, 118), (255, 0, 0)),
}


def test_i420_participant_frames_render_with_bt709_colours() -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()), "run"],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=60, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-8000:]
    (line,) = [entry for entry in result.stdout.splitlines() if entry.startswith(_REPORT)]
    rendered = json.loads(line.removeprefix(_REPORT))
    for name, (_yuv, expected) in _COLOURS.items():
        actual = rendered[name]
        assert all(abs(a - e) <= 6 for a, e in zip(actual, expected, strict=True)), (name, actual)


def _frame(y: int, u: int, v: int) -> bytes:
    return bytes([y]) * (W * H) + bytes([u]) * (W * H // 4) + bytes([v]) * (W * H // 4)


def _run() -> None:
    from contextlib import ExitStack

    from solin.core.media.obs_frame_source import create_frame_source
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import render_source_to_bgra, shutdown

    results: dict[str, list[int]] = {}
    current: list[str] = ["red"]
    samples: Queue = Queue()
    with ExitStack() as stack:
        runtime = ObsRuntime()
        stack.callback(runtime.shutdown)
        runtime.ensure_started(width=128, height=96, fps=30)
        stack.callback(shutdown)
        source = create_frame_source(runtime, "participant-i420-fixture")
        assert source is not None
        stack.callback(source.release)
        native = source.source
        channel = runtime.acquire_channel()
        stack.callback(runtime.release_channel, channel)
        runtime.set_channel_source(channel, native)

        def produce(_seconds: float) -> None:
            yuv, _bgr = _COLOURS[current[0]]
            source.push_i420(_frame(*yuv), W, H)

        def sample(_width: int, _height: int) -> None:
            width, height = native.width, native.height
            if (width, height) != (W, H):
                return
            rendered = render_source_to_bgra(native, width, height, canvas_width=width,
                                             canvas_height=height)
            if rendered is not None:
                pixels, stride = rendered
                offset = (height // 2) * stride + (width // 2) * 4
                samples.put((current[0], list(pixels[offset:offset + 3])))

        producer = runtime.ob.add_tick_callback(produce)
        stack.callback(runtime.ob.remove_tick_callback, producer)
        reader = runtime.ob.add_main_render_callback(sample)
        stack.callback(runtime.ob.remove_main_render_callback, reader)
        for name in _COLOURS:
            current[0] = name
            seen = 0
            while seen < 4:  # let the new colour settle through the async queue
                try:
                    colour, bgr = samples.get(timeout=6)
                except Empty as exc:
                    raise AssertionError(f"no rendered frame for {name}") from exc
                if colour == name:
                    seen += 1
                    results[name] = bgr
    print(_REPORT + json.dumps(results), flush=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    _run()
