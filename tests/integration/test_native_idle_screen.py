"""Isolated native idle playback, silent recording and shared-decoder measurements.

Run with ``python -m pytest tests/integration/test_native_idle_screen.py -q -s``.
Each child has the same 30-second timeout as the native media startup suite.
Metrics describe small offscreen consumer surfaces, not physical monitor costs;
CPU and peak RSS are informative measurements, never machine-specific gates.
Preparation measures the complete ``apply`` transaction, including publication
and retirement. Swap measures only native atomic scene-item publication.
Cold reset measures opening a fresh inactive decoder after the previous live
decoder is retired; it excludes retirement and render reconciliation latency.
"""

from __future__ import annotations

import array
from collections.abc import Callable
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from typing import Any

import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        importlib.util.find_spec("pylibobs") is None
        or not shutil.which("ffmpeg")
        or not shutil.which("ffprobe"),
        reason="Native libobs and FFmpeg fixture tools required",
    ),
]

_PROCESS_TIMEOUT_SECONDS = 30
_SCENARIO_TIMEOUT_SECONDS = 24
_WIDTH, _HEIGHT, _FPS = 320, 180, 30
_READBACK_WIDTH, _READBACK_HEIGHT = 160, 90
_CLIP_DURATION_MS = 800
_PREPARATION_SAMPLES = 8


def _remaining(deadline: float, maximum: float = 5.0) -> float:
    remaining = min(maximum, deadline - time.monotonic())
    assert remaining > 0, "Native idle scenario exceeded its deadline"
    return remaining


def _wait_for(predicate: Callable[[], bool], deadline: float, maximum: float = 3.0) -> None:
    stop = time.monotonic() + _remaining(deadline, maximum)
    while time.monotonic() < stop:
        if predicate():
            return
        time.sleep(1 / 120)
    raise AssertionError("Native idle state was not acknowledged before its deadline")


def _command(arguments: list[str], deadline: float, *, binary: bool = False):
    return subprocess.run(
        arguments,
        capture_output=True,
        text=not binary,
        timeout=_remaining(deadline),
        check=True,
    )


def _fixtures(directory: Path, deadline: float, *, background: bool = False):
    video = directory / "moving-with-tone.mp4"
    image = directory / "fallback.png"
    _command(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=750:sample_rate=48000",
            "-t",
            "0.8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video),
        ],
        deadline,
    )
    _command(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=blue:size=160x120",
            "-frames:v",
            "1",
            str(image),
        ],
        deadline,
    )
    audio = directory / "background-tone.wav"
    if background:
        _command(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=400:sample_rate=48000:duration=4",
                str(audio),
            ],
            deadline,
        )
    return video, image, audio


def _audio_observation(path: Path, deadline: float) -> dict[str, int | float]:
    raw = _command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-",
        ],
        deadline,
        binary=True,
    ).stdout
    samples = array.array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return {"samples": len(samples), "peak": max(map(abs, samples), default=0.0)}


def _native_decoder_count() -> int:
    from pylibobs._ffi import ffi, get_lib

    lib: Any = get_lib()
    count = 0

    @ffi.callback("bool(void *, obs_source_t *)")
    def collect(_data, source):
        nonlocal count
        if ffi.string(lib.obs_source_get_id(source)) == b"ffmpeg_source":
            count += 1
        return True

    # Source.release() removes the native source, even for a strong wrapper
    # produced by enumeration. A measurement must borrow and never remove it.
    lib.obs_enum_all_sources(collect, ffi.NULL)
    return count


def _shared_decoder_ids(sources) -> set[int]:
    from pylibobs._ffi import ffi, get_lib

    lib: Any = get_lib()
    identifiers: set[int] = set()

    @ffi.callback("void(obs_source_t *, obs_source_t *, void *)")
    def collect(_parent, source, _data):
        if ffi.string(lib.obs_source_get_id(source)) == b"ffmpeg_source":
            identifiers.add(int(ffi.cast("uintptr_t", source)))

    for source in sources:
        lib.obs_source_enum_full_tree(source._ptr, collect, ffi.NULL)
    return identifiers


def _frame(source):
    from solin.core.media.obs_source_render import render_source_to_bgra

    return render_source_to_bgra(
        source,
        _READBACK_WIDTH,
        _READBACK_HEIGHT,
        canvas_width=_WIDTH,
        canvas_height=_HEIGHT,
    )


def _assert_visible_contain(frame: tuple[bytes, int] | None) -> tuple[bytes, int]:
    assert frame is not None, "No uploaded native texture"
    pixels, stride = frame
    assert pixels[3] == 0, "The uncovered canvas border must remain transparent"
    center = (_READBACK_HEIGHT // 2) * stride + (_READBACK_WIDTH // 2) * 4
    assert pixels[center + 3] == 255, "The video/image did not fill its contained region"
    return frame


def _resources(stack: ExitStack):
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import shutdown as shutdown_readback
    from solin.core.scenes.libobs_idle_source import LibobsIdleSource

    runtime = ObsRuntime()
    stack.callback(runtime.shutdown)
    # Register cleanup before startup/preparation so every partial failure closes.
    runtime.ensure_started(width=_WIDTH, height=_HEIGHT, fps=_FPS)
    stack.callback(shutdown_readback)
    owner = LibobsIdleSource(runtime)
    stack.callback(owner.close)
    requested = (False, False)
    set_demand = owner.set_demand

    def request_demand(demanded, *, live=False):
        nonlocal requested
        requested = (bool(demanded), bool(demanded and live))

    def reconcile(_width, _height):
        demanded, live = requested
        set_demand(demanded, live=live)

    # Direct-provider scenarios reproduce the sidecar's render reconciliation:
    # a rapid return can leave the immutable frame visible while reset prepares.
    # Only the render thread changes the actual transport, so a sampled old
    # demand cannot overwrite a concurrent control-thread request.
    patch = stack.enter_context(pytest.MonkeyPatch.context())
    patch.setattr(owner, "set_demand", request_demand)
    callback = runtime.ob.add_main_render_callback(reconcile)
    stack.callback(runtime.ob.remove_main_render_callback, callback)
    return runtime, owner


def _idle_decoder(owner) -> Any:
    producer = owner._producer
    return producer.transport.decoder if producer is not None and producer.transport else None


def _wait_decoder(owner, predicate, deadline) -> Any:
    def ready():
        decoder = _idle_decoder(owner)
        return decoder is not None and predicate(decoder)

    _wait_for(ready, deadline)
    decoder = _idle_decoder(owner)
    assert decoder is not None
    return decoder


def _playback(directory: Path, deadline: float) -> dict:
    from solin.core.scenes.idle import IdleScreenState
    from solin.core.scenes.libobs_media_source import (
        LibobsMediaSource,
        STATE_PAUSED,
        STATE_PLAYING,
    )
    from solin.core.scenes.libobs_recorder import LibobsRecorder
    from pylibobs._ffi import get_lib

    video, image, audio = _fixtures(directory, deadline, background=True)
    report: dict[str, Any] = {"fixture_audio": _audio_observation(video, deadline)}
    assert report["fixture_audio"]["peak"] > 0.01, "Silence assertion needs an audible input"
    recording = directory / "silent-recording.mp4"
    with ExitStack() as stack:
        runtime, owner = _resources(stack)
        owner.apply(
            IdleScreenState(1, str(video), str(image), 1),
            int((time.monotonic() + _remaining(deadline)) * 1000),
        )
        source = owner.source
        assert owner._producer is not None
        idle = _idle_decoder(owner)
        assert idle is not None
        assert source.width == _WIDTH and source.height == _HEIGHT
        initial_hash = hashlib.sha256(_assert_visible_contain(_frame(source))[0]).hexdigest()
        assert _native_decoder_count() == 1
        assert idle.muted and idle.volume == 0 and idle.audio_mixers == 0
        lib: Any = get_lib()
        assert lib.obs_source_get_monitoring_type(idle._ptr) == 0
        report["silent_routes"] = {
            "muted": idle.muted,
            "volume": idle.volume,
            "mixers": idle.audio_mixers,
        }
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        original_restart = runtime.ob.Source.media_restart
        restarts = 0

        def restart(source):
            nonlocal restarts
            decoder = _idle_decoder(owner)
            if decoder is not None and source._ptr == decoder._ptr:
                restarts += 1
            original_restart(source)

        patch.setattr(runtime.ob.Source, "media_restart", restart)

        channel = runtime.acquire_channel()
        stack.callback(runtime.release_channel, channel)
        runtime.set_channel_source(channel, source)
        owner.set_demand(True)
        time.sleep(0.2)
        assert (
            hashlib.sha256(_assert_visible_contain(_frame(source))[0]).hexdigest() == initial_hash
        )
        assert restarts == 0 and idle.media_state == STATE_PAUSED
        report["preview_before_first_live_ms"] = idle.media_time
        owner.set_demand(True, live=True)
        _wait_for(lambda: idle.media_state == STATE_PLAYING and idle.media_time < 200, deadline)
        report["first_live_start_ms"] = idle.media_time
        assert restarts == 1
        _wait_for(lambda: idle.media_time >= 150, deadline)
        owner.set_demand(True, live=True)  # another live monitor joins
        assert restarts == 1, "A second live consumer restarted the shared decoder"
        recorder = LibobsRecorder(runtime)
        stack.callback(recorder.stop)
        assert recorder.start(str(recording), video_bitrate=1200), "Native recording did not start"
        positions = []
        hashes = set()
        stop = time.monotonic() + _remaining(deadline, 3.0)
        while time.monotonic() < stop:
            frame = _assert_visible_contain(_frame(source))
            hashes.add(hashlib.sha256(frame[0]).hexdigest())
            positions.append(idle.media_time)
            time.sleep(0.04)
        recorder.stop()
        report["unique_native_frames"] = len(hashes)
        report["loop_wraps"] = sum(
            after + 100 < before for before, after in zip(positions, positions[1:], strict=False)
        )
        assert len(hashes) > 10, report
        assert report["loop_wraps"] >= 2, report
        owner.set_demand(True)  # live recording leaves; preview still shows idle
        assert restarts == 1
        _wait_for(
            lambda: (
                not owner._live_demand
                and hashlib.sha256(_assert_visible_contain(_frame(source))[0]).hexdigest()
                == initial_hash
            ),
            deadline,
        )
        idle = _wait_decoder(owner, lambda decoder: decoder.media_state == STATE_PAUSED, deadline)
        report["preview_before_returning_live_ms"] = idle.media_time
        owner.set_demand(True, live=True)
        _wait_for(lambda: idle.media_state == STATE_PLAYING and idle.media_time < 200, deadline)
        report["returning_live_start_ms"] = idle.media_time
        assert restarts == 2

        foreground, background = LibobsMediaSource(runtime), LibobsMediaSource(runtime)
        stack.callback(foreground.close)
        stack.callback(background.close)
        assert foreground.open(str(video), volume_percent=0)
        assert foreground.wait_for_video_frame(deadline=time.monotonic() + _remaining(deadline))
        assert background.open(str(audio), volume_percent=0)
        foreground.pause()
        background.pause()

        def both_paused():
            foreground.apply_pending_resume()
            background.apply_pending_resume()
            return foreground.source.media_state == background.source.media_state == STATE_PAUSED

        _wait_for(both_paused, deadline)
        before = idle.media_time
        time.sleep(0.2)
        report["independent_idle_motion_ms"] = (idle.media_time - before) % _CLIP_DURATION_MS
        assert report["independent_idle_motion_ms"] >= 100, report

        owner.set_demand(False)
        _wait_for(
            lambda: (
                not owner._live_demand
                and hashlib.sha256(_assert_visible_contain(_frame(source))[0]).hexdigest()
                == initial_hash
            ),
            deadline,
        )
        idle = _wait_decoder(owner, lambda decoder: decoder.media_state == STATE_PAUSED, deadline)
        paused = idle.media_time
        foreground.play()
        background.play()
        _wait_for(
            lambda: foreground.source.media_state == background.source.media_state == STATE_PLAYING,
            deadline,
        )
        foreground_before, background_before = foreground.position_ms, background.position_ms
        time.sleep(0.15)
        report["paused_idle_motion_ms"] = abs(idle.media_time - paused)
        report["independent_foreground_motion_ms"] = foreground.position_ms - foreground_before
        report["independent_background_motion_ms"] = background.position_ms - background_before
        assert report["paused_idle_motion_ms"] <= 40, report
        assert report["independent_foreground_motion_ms"] >= 50, report
        assert report["independent_background_motion_ms"] >= 50, report

        owner.set_demand(True, live=True)
        _wait_for(lambda: idle.media_state == STATE_PLAYING and idle.media_time < 200, deadline)
        report["restart_position_ms"] = idle.media_time
        assert restarts == 3, "Simultaneous overall/live edges restarted more than once"
        report["demand_restarts"] = {
            "preview": 0,
            "first_live": 1,
            "second_live": 1,
            "preview_only": 1,
            "returning_live": 2,
            "after_all_removed": restarts,
        }
        owner.apply(
            IdleScreenState(2, str(video), str(image), 2),
            int((time.monotonic() + _remaining(deadline)) * 1000),
        )
        assert owner._producer is not None
        assert _idle_decoder(owner) is idle, "Fallback revision restarted the live decoder"
        owner.apply(
            IdleScreenState(3, "", str(image), 2),
            int((time.monotonic() + _remaining(deadline)) * 1000),
        )
        assert owner.source._ptr == source._ptr, "Fallback replacement changed the shared scene"
        _assert_visible_contain(_frame(source))
        report["stable_scene_after_fallback"] = True

    report["recorded_audio"] = _audio_observation(recording, deadline)
    assert report["recorded_audio"]["samples"] > 48_000, report
    assert report["recorded_audio"]["peak"] == 0, report
    return report


def _peak_rss_bytes() -> int:
    if sys.platform != "win32":
        import resource

        maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(maximum if sys.platform == "darwin" else maximum * 1024)
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("page_faults", wintypes.DWORD),
            *[
                (name, ctypes.c_size_t)
                for name in (
                    "peak_working_set",
                    "working_set",
                    "quota_peak_paged",
                    "quota_paged",
                    "quota_peak_nonpaged",
                    "quota_nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ],
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    get_info = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    # -1 is Windows' pseudo-handle for the current process; no owned handle.
    if not get_info(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.peak_working_set


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _measure_surfaces(directory: Path, deadline: float, count: int) -> dict:
    from solin.core.scenes.idle import IdleScreenState
    from solin.core.scenes.libobs_media_source import STATE_PAUSED, STATE_PLAYING
    from solin.core.media.obs_source_render import render_source_to_bgra
    from pylibobs._ffi import get_lib

    video, image, _audio = _fixtures(directory, deadline)
    with ExitStack() as stack:
        runtime, owner = _resources(stack)
        idle_source = owner.source
        owner.set_demand(True, live=True)
        surfaces = []
        # Each consumer is a real private scene and an independent OBS video
        # mix. Readbacks exercise each at the native render cadence, including
        # mixes with no recorder attached. Every consumer borrows the same idle.
        for index in range(count):
            scene = runtime.ob.Scene.create_private(f"idle-smoke-consumer-{index}")
            stack.callback(scene.release)
            item = scene.add(idle_source)
            stack.callback(item.release)
            stack.callback(item.remove)
            view = runtime.ob.View.create()
            stack.callback(view.release)
            stack.callback(view.remove)
            stack.callback(view.set_source, 0, None)
            source = scene.as_source()
            surfaces.append(source)
            view.set_source(0, source)
            assert view.add() is not None, "Independent OBS video mix was not created"

        latencies = []
        swaps = []
        captured_swaps = []
        original_swap = owner._swap

        def measure_swap(candidate):
            start = time.monotonic()
            result = original_swap(candidate)
            captured_swaps.append((time.monotonic() - start) * 1000)
            return result

        patch = stack.enter_context(pytest.MonkeyPatch.context())
        patch.setattr(owner, "_swap", measure_swap)
        for index in range(_PREPARATION_SAMPLES):
            if index:
                owner.apply(
                    IdleScreenState(index * 2, "", str(image), 1),
                    int((time.monotonic() + _remaining(deadline)) * 1000),
                )
            captured_swaps.clear()  # discard intervening PNG publication
            start = time.monotonic()
            owner.apply(
                IdleScreenState(index * 2 + 1, str(video), str(image), 1),
                int((time.monotonic() + _remaining(deadline)) * 1000),
            )
            latencies.append((time.monotonic() - start) * 1000)
            assert len(captured_swaps) == 1, "Video must have exactly one atomic publication"
            swaps.append(captured_swaps[0])

        assert len(swaps) == _PREPARATION_SAMPLES
        assert all(math.isfinite(value) and value >= 0 for value in swaps)

        producer = owner._producer
        assert producer is not None
        _wait_decoder(owner, lambda decoder: decoder.media_time >= 150, deadline)
        assert _native_decoder_count() == 1
        assert len(_shared_decoder_ids(surfaces)) == 1
        transport = producer.transport
        assert transport is not None
        cold_resets = []
        original_open = transport._open

        def measure_cold_reset(deadline_ms, check_current):
            start = time.monotonic()
            decoder = original_open(deadline_ms, check_current)
            cold_resets.append((time.monotonic() - start) * 1000)
            return decoder

        patch.setattr(transport, "_open", measure_cold_reset)
        for _index in range(_PREPARATION_SAMPLES):
            old_decoder = _idle_decoder(owner)
            assert old_decoder is not None
            owner.set_demand(True, live=False)
            _wait_for(lambda: not owner._live_demand, deadline)
            _wait_decoder(
                owner,
                lambda decoder, previous=old_decoder: (
                    decoder is not previous and decoder.media_state == STATE_PAUSED
                ),
                deadline,
            )
            assert _native_decoder_count() == 1
            owner.set_demand(True, live=True)
            _wait_decoder(owner, lambda decoder: decoder.media_state == STATE_PLAYING, deadline)
        assert len(cold_resets) == _PREPARATION_SAMPLES
        assert all(math.isfinite(value) and value >= 0 for value in cold_resets)
        ready = threading.Event()
        errors = []
        rendered = [0] * count
        last_frame_time = -1
        lib: Any = get_lib()

        def render(_width, _height):
            nonlocal last_frame_time
            try:
                # OBS invokes main-render callbacks for each registered video
                # mix. Measure one readback per consumer per actual video tick,
                # rather than multiplying work again by the number of mixes.
                frame_time = lib.obs_get_video_frame_time()
                if frame_time == last_frame_time:
                    return
                last_frame_time = frame_time
                for index, source in enumerate(surfaces):
                    frame = render_source_to_bgra(
                        source,
                        _READBACK_WIDTH,
                        _READBACK_HEIGHT,
                        canvas_width=_WIDTH,
                        canvas_height=_HEIGHT,
                    )
                    _assert_visible_contain(frame)
                    rendered[index] += 1
                if min(rendered) >= 8:
                    ready.set()
            except Exception as error:  # noqa: BLE001 - forward native callback failures
                errors.append(str(error))
                ready.set()

        callback = runtime.ob.add_main_render_callback(render)
        stack.callback(runtime.ob.remove_main_render_callback, callback)
        assert ready.wait(_remaining(deadline)), "Consumer surfaces never rendered"
        assert not errors, errors
        cpu_before = os.times()
        start = time.monotonic()
        initial_frames = list(rendered)
        time.sleep(_remaining(deadline, 1.5))
        elapsed = time.monotonic() - start
        cpu_after = os.times()
        cpu_seconds = cpu_after.user + cpu_after.system - cpu_before.user - cpu_before.system
        frame_counts = [
            after - before for before, after in zip(initial_frames, rendered, strict=True)
        ]
        assert min(frame_counts) >= 15, frame_counts
        assert not errors, errors
        decoder_count = _native_decoder_count()
        assert decoder_count == 1
        return {
            "consumer_surfaces": count,
            "canvas": [_WIDTH, _HEIGHT],
            "readback": [_READBACK_WIDTH, _READBACK_HEIGHT],
            "fps": _FPS,
            "preparation_samples_ms": [round(value, 3) for value in latencies],
            "preparation_cold_ms": round(latencies[0], 3),
            "preparation_p50_ms": round(statistics.median(latencies), 3),
            "preparation_p95_ms": round(_percentile(latencies, 0.95), 3),
            "swap_samples_ms": [round(value, 3) for value in swaps],
            "swap_p50_ms": round(statistics.median(swaps), 3),
            "swap_p95_ms": round(_percentile(swaps, 0.95), 3),
            "cold_reset_samples_ms": [round(value, 3) for value in cold_resets],
            "cold_reset_p50_ms": round(statistics.median(cold_resets), 3),
            "cold_reset_p95_ms": round(_percentile(cold_resets, 0.95), 3),
            "cpu_one_core_percent": round(100 * cpu_seconds / elapsed, 3),
            "cpu_seconds": round(cpu_seconds, 4),
            "elapsed_seconds": round(elapsed, 4),
            "peak_rss_bytes": _peak_rss_bytes(),
            "rendered_frames_per_surface": frame_counts,
            "native_decoder_count": decoder_count,
            "shared_decoder_count": len(_shared_decoder_ids(surfaces)),
        }


def _run_native(scenario: str, directory: Path) -> dict:
    report = directory / "scenario.json"
    command = [
        sys.executable,
        "-B",
        "-X",
        "faulthandler",
        str(Path(__file__).resolve()),
        scenario,
        str(directory),
    ]
    environment = dict(os.environ, SOLIN_RECORD_HW_ENCODE="0")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_PROCESS_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        diagnostics = []
        for stream in (error.stdout, error.stderr):
            diagnostics.append(
                stream.decode(errors="replace") if isinstance(stream, bytes) else stream or ""
            )
        raise AssertionError(
            f"Native idle scenario {scenario!r} timed out:\n{''.join(diagnostics)[-16000:]}"
        ) from error
    diagnostics = "\n".join((result.stdout + result.stderr).splitlines()[-100:])
    assert result.returncode == 0, (
        f"Native idle scenario {scenario!r} exited {result.returncode}:\n{diagnostics}"
    )
    assert report.is_file(), "Native idle child did not publish its completed report"
    return json.loads(report.read_text(encoding="utf-8"))


def test_native_idle_video_loops_silently_and_keeps_transports_independent(tmp_path, request):
    report = _run_native("playback", tmp_path)
    request.node.user_properties.append(
        ("native_idle_playback", json.dumps(report, sort_keys=True))
    )
    print("NATIVE_IDLE_PLAYBACK", json.dumps(report, sort_keys=True))


def test_native_idle_preparation_and_shared_surface_metrics(tmp_path, request):
    reports = []
    for count in (1, 6):
        directory = tmp_path / f"surfaces-{count}"
        directory.mkdir()
        report = _run_native(f"surfaces-{count}", directory)
        reports.append(report)
    comparison = {
        "scenarios": reports,
        "memory_metric": "per-process peak resident set",
        "preparation_metric": "complete apply: prepare, atomic publication and retirement",
        "swap_metric": "atomic scene-item publication only",
        "cold_reset_metric": "fresh inactive decoder opening after retiring the live decoder",
    }
    (tmp_path / "comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    request.node.user_properties.append(
        ("native_idle_surface_metrics", json.dumps(comparison, sort_keys=True))
    )
    print("NATIVE_IDLE_SURFACE_METRICS", json.dumps(comparison, sort_keys=True))
    assert [report["native_decoder_count"] for report in reports] == [1, 1]
    assert [report["shared_decoder_count"] for report in reports] == [1, 1]
    for report in reports:
        assert len(report["swap_samples_ms"]) == _PREPARATION_SAMPLES
        assert all(value >= 0 for value in report["swap_samples_ms"])
        assert len(report["cold_reset_samples_ms"]) == _PREPARATION_SAMPLES
        assert all(value >= 0 for value in report["cold_reset_samples_ms"])


if __name__ == "__main__":
    # Direct children must find the repository's editable source without importing
    # tests.conftest (which initializes QApplication in the pytest parent).
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    scenario, directory = sys.argv[1], Path(sys.argv[2])
    deadline = time.monotonic() + _SCENARIO_TIMEOUT_SECONDS
    result = (
        _playback(directory, deadline)
        if scenario == "playback"
        else _measure_surfaces(directory, deadline, int(scenario.removeprefix("surfaces-")))
    )
    result["platform"] = platform.platform()
    result["python"] = platform.python_version()
    result["hw_decode_setting"] = os.environ.get("SOLIN_MEDIA_HW_DECODE", "1")
    (directory / "scenario.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
