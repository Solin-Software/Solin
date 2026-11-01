"""Observe the rendered Windows monitoring stream in an isolated process."""

from __future__ import annotations

from array import array
from contextlib import ExitStack, contextmanager
import ctypes
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
import time
import wave

import pytest

from tests.integration.test_native_media_startup import _wait_for

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="WASAPI process loopback requires Windows"),
]

_MODULE = "tests.integration.test_native_audio_monitoring"
_SAMPLE_RATE = 48000
_FREQUENCY = 1000
_PLAY_SECONDS = 24
_SKIP = 77


def _command(*arguments):
    return [sys.executable, "-B", "-X", "faulthandler", "-m", _MODULE, *arguments]


@contextmanager
def _runtime():
    from solin.core.media.obs_runtime import ObsRuntime

    runtime = ObsRuntime()
    try:
        runtime.ensure_started(width=64, height=64, fps=60)
        # Both processes explicitly configure the callback's global float-plane rate.
        runtime.context.set_audio(samples_per_sec=_SAMPLE_RATE)
        yield runtime
    finally:
        runtime.shutdown()


@contextmanager
def _window():
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    user.CreateWindowExW.restype = wintypes.HWND
    user.DestroyWindow.argtypes = [wintypes.HWND]
    title = f"solin-monitor-contract-{time.monotonic_ns()}"
    # OBS selects a different process by its visible top-level window. This
    # offscreen, non-activating window never takes focus or changes other apps.
    window = user.CreateWindowExW(
        0x08000000,
        "Static",
        title,
        0x90000000,
        -32000,
        -32000,
        1,
        1,
        None,
        None,
        None,
        None,
    )
    assert window, ctypes.get_last_error()
    try:
        yield title
    finally:
        assert user.DestroyWindow(window)


def _terminate(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _route(runtime, stack, source):
    channel = runtime.acquire_channel()
    stack.callback(runtime.release_channel, channel)
    runtime.set_channel_source(channel, source)


def _capture(title, directory):
    from pylibobs.callbacks import (
        add_source_audio_capture_callback,
        remove_source_audio_capture_callback,
    )

    directory = Path(directory)
    with _runtime() as runtime, ExitStack() as stack:
        assert "wasapi_process_output_capture" in runtime.ob.enum_source_types()
        source = runtime.ob.Source.create(
            "wasapi_process_output_capture",
            "monitor-contract-loopback",
            {"window": f"{title}:Static:{Path(sys.executable).name}", "priority": 1},
        )
        stack.callback(source.release)
        source.audio_mixers = 0
        _route(runtime, stack, source)
        blocks = []
        lock = Lock()

        def capture(planes, frames, timestamp_ns, muted):
            with lock:
                blocks.append((time.monotonic(), timestamp_ns, frames, muted, planes[0]))

        callback = add_source_audio_capture_callback(source, capture, channels=1)
        stack.callback(remove_source_audio_capture_callback, source, callback)
        time.sleep(0.6)
        (directory / "ready").touch()
        time.sleep(_PLAY_SECONDS + 2)
        with lock:
            recorded = list(blocks)
        (directory / "loopback.f32").write_bytes(b"".join(block[4] for block in recorded))
        (directory / "blocks.json").write_text(
            json.dumps([block[:4] for block in recorded]),
            encoding="utf-8",
        )


def _write_tone(path):
    period_frames = _SAMPLE_RATE // _FREQUENCY
    period = b"".join(
        struct.pack("<h", round(6000 * math.sin(2 * math.pi * i / period_frames))) * 2
        for i in range(period_frames)
    )
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, _SAMPLE_RATE, 0, "NONE", "not compressed"))
        output.writeframes(period * (_SAMPLE_RATE // period_frames) * (_PLAY_SECONDS + 8))


def _assert_continuous_tone(directory, started):
    metadata = json.loads((directory / "blocks.json").read_text(encoding="utf-8"))
    raw = (directory / "loopback.f32").read_bytes()
    selected = []
    offset = 0
    for arrival, timestamp, frames, muted in metadata:
        size = frames * 4
        if started + 1 <= arrival <= started + _PLAY_SECONDS - 1:
            selected.append((timestamp, frames, muted, raw[offset : offset + size]))
        offset += size
    assert offset == len(raw)
    pcm = array("f", b"".join(block[3] for block in selected))
    expected_seconds = _PLAY_SECONDS - 2
    assert abs(len(pcm) / _SAMPLE_RATE - expected_seconds) < 0.1, len(pcm)
    assert all(math.isfinite(value) for value in pcm)
    assert not any(block[2] for block in selected)
    rms = math.sqrt(sum(value * value for value in pcm) / len(pcm))
    assert rms > 0.005, ("No monitored tone", rms)

    # A pure sine satisfies x[n] - 2*cos(w)*x[n-1] + x[n-2] == 0.
    # Eight 16-bit quantization steps cover rounding and the independent
    # monitor/capture resamplers. Even a gap at a sine zero crossing violates
    # this bound, unlike an RMS or peak-only silence check.
    precision_budget = 8 / 32768
    coefficient = 2 * math.cos(2 * math.pi * _FREQUENCY / _SAMPLE_RATE)
    maximum_residual = max(
        abs(pcm[i] - coefficient * pcm[i - 1] + pcm[i - 2]) for i in range(2, len(pcm))
    )
    peak = max(abs(value) for value in pcm)
    maximum_step = max(abs(b - a) for a, b in zip(pcm, pcm[1:], strict=False))
    maximum_sine_step = 2 * peak * math.sin(math.pi * _FREQUENCY / _SAMPLE_RATE)
    gaps = [
        (current[0] - previous[0]) / 1e9 - previous[1] / _SAMPLE_RATE
        for previous, current in zip(selected, selected[1:], strict=False)
    ]
    print(
        "monitor waveform",
        {
            "frames": len(pcm),
            "rms": rms,
            "max_step": maximum_step,
            "max_sine_residual": maximum_residual,
            "precision_budget": precision_budget,
            "max_timestamp_error": max(map(abs, gaps), default=0),
        },
        flush=True,
    )
    assert maximum_residual <= precision_budget, maximum_residual
    assert maximum_step <= maximum_sine_step + precision_budget, maximum_step
    assert max(map(abs, gaps), default=0) <= 1 / _SAMPLE_RATE, gaps


def _assert_source_sample_clock(blocks, started):
    selected = [b for b in blocks if started + 1 <= b[0] <= started + _PLAY_SECONDS - 1]
    assert selected, "No source audio callbacks"
    assert not any(block[3] for block in selected), "Source audio was muted"
    frames = sum(block[2] for block in selected[:-1])
    duration_ns = frames * 1_000_000_000 / _SAMPLE_RATE
    elapsed_ns = selected[-1][1] - selected[0][1]
    clock_error_ns = elapsed_ns - duration_ns
    errors = [
        (current[1] - previous[1]) - previous[2] * 1_000_000_000 / _SAMPLE_RATE
        for previous, current in zip(selected, selected[1:], strict=False)
    ]
    arrivals = [
        current[0] - previous[0] for previous, current in zip(selected, selected[1:], strict=False)
    ]
    assert len(selected) > 2
    print(
        "source sample clock",
        {
            "frames": frames,
            "clock_error_ms": clock_error_ns / 1e6,
            "max_block_error_ms": max(map(abs, errors)) / 1e6,
            "arrival_p95_ms": sorted(arrivals)[math.ceil(len(arrivals) * 0.95) - 1] * 1000,
            "arrival_max_ms": max(arrivals) * 1000,
        },
        flush=True,
    )
    # Allow two sample periods for timestamp and resampler rounding, while
    # measuring the entire window so small repeated gaps cannot accumulate.
    assert abs(clock_error_ns) <= 2 * 1_000_000_000 / _SAMPLE_RATE, clock_error_ns / 1e6
    assert max(map(abs, errors)) <= 2 * 1_000_000_000 / _SAMPLE_RATE, max(map(abs, errors)) / 1e6


def _play(source_id, speed=100, video=False):
    from solin.core.media.obs_runtime import MONITORING_MONITOR_ONLY, MONITORING_NONE

    if sys.getwindowsversion().build < 20348:
        print("Process loopback requires Windows build 20348 or newer", flush=True)
        return _SKIP
    with TemporaryDirectory() as temporary, _runtime() as runtime, ExitStack() as stack:
        directory = Path(temporary)
        if not runtime.available_monitoring_devices():
            print("No Windows audio rendering device is available", flush=True)
            return _SKIP
        path = directory / ("tone.avi" if video else "tone.wav")
        if video:
            from tests.integration.test_native_media_rate import _write_av_clip

            _write_av_clip(
                path, duration=(_PLAY_SECONDS + 8) * 2, periodic=True, constant_frequency=_FREQUENCY
            )
        else:
            _write_tone(path)
        title = stack.enter_context(_window())
        log_path = directory / "capture.log"
        log = stack.enter_context(log_path.open("w", encoding="utf-8"))
        process = subprocess.Popen(
            _command("capture", title, temporary),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        stack.callback(_terminate, process)
        assert _wait_for(
            lambda: (directory / "ready").exists() or process.poll() is not None,
            timeout=12,
        ), log_path.read_text(encoding="utf-8")
        assert process.poll() is None, log_path.read_text(encoding="utf-8")
        source = runtime.ob.Source.create(
            source_id,
            "monitor-contract-media",
            {
                "is_local_file": True,
                "local_file": "",
                "speed_percent": speed,
                "restart_on_activate": False,
                "close_when_inactive": False,
                "hw_decode": False,
            },
        )
        stack.callback(source.release)
        source.volume = 0.5
        audio_controls = (source.muted, source.volume)
        runtime.set_source_monitoring(source, MONITORING_MONITOR_ONLY)
        stack.callback(runtime.set_source_monitoring, source, MONITORING_NONE)
        assert source.monitoring_enabled is True
        assert source.audio_mixers == 0
        assert (source.muted, source.volume) == audio_controls
        _route(runtime, stack, source)
        source_blocks = []
        lock = Lock()
        if video:
            from pylibobs.callbacks import (
                add_source_audio_capture_callback,
                remove_source_audio_capture_callback,
            )
            from solin.core.media.obs_source_render import (
                render_source_to_bgra,
                shutdown as shutdown_readback,
            )

            stack.callback(shutdown_readback)

            def capture_source(_planes, frames, timestamp_ns, muted):
                with lock:
                    source_blocks.append((time.monotonic(), timestamp_ns, frames, muted))

            callback = add_source_audio_capture_callback(source, capture_source, channels=1)
            stack.callback(remove_source_audio_capture_callback, source, callback)
        started = time.monotonic()
        source.update({"local_file": str(path)})
        if video:
            colors = set()
            positions = []
            while time.monotonic() < started + _PLAY_SECONDS:
                frame = render_source_to_bgra(source, 64, 64, canvas_width=64, canvas_height=64)
                if frame:
                    pixels, stride = frame
                    offset = 32 * stride + 32 * 4
                    colors.add(pixels[offset : offset + 4])
                    positions.append(source.media_time)
                time.sleep(0.03)
            print(
                "rendered video",
                {"colors": [x.hex() for x in colors], "positions": positions[-3:]},
                flush=True,
            )
            assert {bytes((0, 0, 255, 255)), bytes((255, 0, 0, 255))} <= colors, colors
            assert positions and positions[-1] > 1000, "Video did not advance"
        assert process.wait(timeout=_PLAY_SECONDS + 8) == 0, log_path.read_text(encoding="utf-8")
        errors = []
        if video:
            with lock:
                recorded = list(source_blocks)
            try:
                _assert_source_sample_clock(recorded, started)
            except AssertionError as error:
                errors.append(f"source sample clock: {error}")
        try:
            _assert_continuous_tone(directory, started)
        except AssertionError as error:
            errors.append(f"monitor output: {error}")
        assert not errors, "; ".join(errors)
    return 0


@pytest.mark.parametrize("source_id", ["ffmpeg_source", "solin_media_source"])
def test_native_monitoring_preserves_continuous_pcm(source_id):
    from tests.e2e._windows_process import run_windows_process_tree

    # Own the capture grandchild as well when a native crash skips finalizers.
    result = run_windows_process_tree(_command("play", source_id), timeout=50)
    diagnostics = "\n".join((result.stdout + result.stderr).splitlines()[-70:])
    if result.returncode == _SKIP:
        pytest.skip(diagnostics)
    assert result.returncode == 0, f"Native monitoring exited {result.returncode}:\n{diagnostics}"


@pytest.mark.parametrize("speed", [100, 200])
def test_native_av_monitoring_preserves_sample_clock_and_pcm(speed):
    from tests.e2e._windows_process import run_windows_process_tree

    result = run_windows_process_tree(_command("play-av", str(speed)), timeout=60)
    diagnostics = "\n".join((result.stdout + result.stderr).splitlines()[-70:])
    if result.returncode == _SKIP:
        pytest.skip(diagnostics)
    assert result.returncode == 0, (
        f"Native A/V monitoring exited {result.returncode}:\n{diagnostics}"
    )


if __name__ == "__main__":
    if sys.argv[1] == "capture":
        _capture(*sys.argv[2:])
    elif sys.argv[1] == "play":
        raise SystemExit(_play(sys.argv[2]))
    elif sys.argv[1] == "play-av":
        raise SystemExit(_play("solin_media_source", int(sys.argv[2]), True))
    else:
        raise ValueError(sys.argv[1])
