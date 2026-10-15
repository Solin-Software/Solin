"""Exercise passive async observation and deferred updates without camera hardware."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
import gc
import importlib.util
import json
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Event, get_ident
import time
from typing import Any

import pytest


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(importlib.util.find_spec("pylibobs") is None, reason="libobs required"),
]
_TIMEOUT = 6.0
_REPORT = "CAMERA_CAPTURE_REPORT="


@pytest.mark.parametrize("scenario", ["passive", "deferred_update"])
def test_native_camera_capture(scenario: str) -> None:
    result = subprocess.run(
        [sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()), scenario],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=30, check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-16000:]
    assert "Exception ignored from cffi callback" not in output, output[-16000:]
    reports = [line.removeprefix(_REPORT) for line in result.stdout.splitlines()
               if line.startswith(_REPORT)]
    assert len(reports) == 1, output[-16000:]
    report = json.loads(reports[0])
    assert report["observed"] >= 3
    assert report["rendered"] >= 3
    assert report["updates"] == (2 if scenario == "deferred_update" else 0)


@dataclass(frozen=True)
class _Sample:
    dimensions: tuple[int, int]
    pixel: bytes
    metadata: tuple[int, int, int] | None
    state: Any


def _wait(samples: Queue, predicate: Callable[[_Sample], bool], reason: str) -> _Sample:
    deadline = time.monotonic() + _TIMEOUT
    last = None
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            sample = samples.get(timeout=remaining)
        except Empty:
            break
        if isinstance(sample, BaseException):
            raise sample
        last = sample
        if predicate(sample):
            return sample
    raise AssertionError(f"{reason}; last native sample: {last!r}")


def _updating_source(runtime: Any, applied: list, errors: Queue) -> tuple[Any, tuple]:
    """Register the OBS 32.1.2 source-info prefix through update.

    The standard synthetic source has no update callback, so OBS cannot emit its
    deferred update signal. This fixture supplies one and owns its C callbacks
    until context shutdown. Upload still uses Solin's normal async frame path.
    """
    import cffi
    from pylibobs._ffi import ffi, get_lib

    from solin.core.media.obs_frame_source import ObsFrameSource

    head = cffi.FFI()
    head.cdef("""
        typedef struct obs_source obs_source_t;
        typedef struct obs_data obs_data_t;
        struct source_info_head {
            const char *id; int type; uint32_t output_flags;
            const char *(*get_name)(void *);
            void *(*create)(obs_data_t *, obs_source_t *);
            void (*destroy)(void *);
            void *get_width; void *get_height; void *get_defaults;
            void *get_properties;
            void (*update)(void *, obs_data_t *);
        };
    """)
    identifier = head.new("char[]", b"camera-update-fixture")
    lib: Any = get_lib()

    @head.callback("const char *(void *)")
    def get_name(_data):
        return identifier

    @head.callback("void *(obs_data_t *, obs_source_t *)")
    def create(_settings, source):
        return head.cast("void *", source)

    @head.callback("void (void *)")
    def destroy(_data):
        return None

    @head.callback("void (void *, obs_data_t *)")
    def update(_data, settings):
        try:
            pointer = ffi.cast("obs_data_t *", int(head.cast("uintptr_t", settings)))
            applied.append((int(lib.obs_data_get_int(pointer, b"generation")), get_ident()))
        except BaseException as error:  # noqa: BLE001 - forward native callback failures
            errors.put(error)

    info: Any = head.new("struct source_info_head *")
    info.id, info.type, info.output_flags = identifier, 0, 5  # input, async video
    info.get_name, info.create, info.destroy = get_name, create, destroy
    info.update = update
    owners = (head, identifier, info, get_name, create, destroy, update)
    lib.obs_register_source_s(
        ffi.cast("struct obs_source_info *", int(head.cast("uintptr_t", info))),
        head.sizeof("struct source_info_head"),
    )
    source = runtime.ob.Source.create("camera-update-fixture", "fixture", {})
    lib.obs_source_set_async_unbuffered(source._ptr, True)

    def unexpected_preload(_upload):
        raise AssertionError("The fixture must exercise async filtering, not preload")

    return ObsFrameSource(source, upload_first_frame=unexpected_preload), owners


def _scenario(scenario: str) -> None:
    from solin.core.media.obs_frame_source import create_frame_source
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import render_source_to_bgra, shutdown

    samples: Queue = Queue()
    applied: list[tuple[int, int]] = []
    signals: list[tuple[tuple[int, int], int]] = []
    update_received, producing, paused = Event(), Event(), Event()
    observed: list[tuple[int, int, int]] = []
    downstream: list[tuple[int, int, int]] = []
    mode = [(64, 48, bytes((35, 101, 211, 255)))]
    checking_chain = True
    rendered_count = 0
    owners = None
    with ExitStack() as stack:
        runtime = ObsRuntime()
        stack.callback(runtime.shutdown)
        runtime.ensure_started(width=128, height=96, fps=30)
        stack.callback(shutdown)
        if scenario == "deferred_update":
            source, owners = _updating_source(runtime, applied, samples)
        else:
            source = create_frame_source(runtime, "camera-observation-fixture")
        assert source is not None
        stack.callback(source.release)
        native_source = source.source
        assert native_source is not None
        channel = runtime.acquire_channel()
        stack.callback(runtime.release_channel, channel)
        runtime.set_channel_source(channel, native_source)
        close_first = runtime.observe_source_video(native_source, lambda *frame: observed.append(frame))
        stack.callback(close_first)
        close_second = runtime.observe_source_video(native_source, lambda *frame: downstream.append(frame))
        stack.callback(close_second)
        probe = runtime.watch_camera_video(native_source, width=64, height=48)
        stack.callback(probe.close)
        assert probe.state.generation == 1 and probe.state.status == "starting"

        def updated() -> None:
            signals.append((applied[-1], get_ident()))
            update_received.set()

        if scenario == "deferred_update":
            stack.callback(runtime.observe_source_updates(native_source, updated))

        def produce(_seconds: float) -> None:
            try:
                if not producing.is_set():
                    paused.set()
                    return
                width, height, pixel = mode[0]
                assert source.push_bgra(pixel * (width * height), width, height, width * 4)
            except BaseException as error:  # noqa: BLE001 - forward native callback failures
                samples.put(error)

        producer = runtime.ob.add_tick_callback(produce)
        stack.callback(runtime.ob.remove_tick_callback, producer)

        def sample(_width: int, _height: int) -> None:
            nonlocal rendered_count
            try:
                # Both observers run in async_tick before this render callback.
                if checking_chain:
                    assert observed == downstream, "A passive filter lost or changed a frame"
                width, height = native_source.width, native_source.height
                if not width or not height:
                    return
                rendered = render_source_to_bgra(
                    native_source, width, height, canvas_width=width, canvas_height=height,
                )
                if rendered is not None:
                    pixels, stride = rendered
                    offset = (height // 2) * stride + (width // 2) * 4
                    rendered_count += 1
                    samples.put(_Sample((width, height), pixels[offset:offset + 4],
                                        observed[-1] if observed else None, probe.state))
            except BaseException as error:  # noqa: BLE001 - forward native callback failures
                samples.put(error)

        reader = runtime.ob.add_main_render_callback(sample)
        stack.callback(runtime.ob.remove_main_render_callback, reader)
        gc.collect()  # Registration must own callbacks beyond temporary callers.
        producing.set()

        def matches(value: _Sample, expected: tuple, generation: int) -> bool:
            width, height, pixel = expected
            return (value.dimensions == (width, height) and value.pixel == pixel
                    and value.metadata is not None and value.metadata[:2] == (width, height)
                    and value.state.status == "ready" and value.state.generation == generation
                    and (value.state.width, value.state.height) == (width, height))

        for _ in range(3):
            _wait(samples, lambda value: matches(value, mode[0], 1), "Initial frame not confirmed")

        if scenario == "deferred_update":
            for generation, next_mode in enumerate([
                (96, 64, bytes((23, 213, 71, 255))),
                (96, 64, bytes((219, 57, 19, 255))),
            ], start=2):
                paused.clear()
                producing.clear()
                assert paused.wait(_TIMEOUT), "Native producer did not pause"
                previous = mode[0]
                update_received.clear()
                probe.update({"generation": generation}, width=next_mode[0], height=next_mode[1])
                assert probe.state.generation == generation and probe.state.status == "starting"
                assert update_received.wait(_TIMEOUT), "OBS did not emit its deferred update signal"
                assert signals[-1][0][0] == generation
                assert signals[-1][0][1] == signals[-1][1] != get_ident()
                _wait(samples, lambda value, gen=generation, old=previous: (
                    value.state.generation == gen and value.state.status == "starting"
                    and value.dimensions == old[:2] and value.pixel == old[2]
                ), "A retained pre-update frame incorrectly confirmed the new generation")
                mode[0] = next_mode
                producing.set()
                _wait(samples, lambda value, target=next_mode, gen=generation: matches(value, target, gen),
                      "Updated frame dimensions/pixels were not confirmed")
            assert len(signals) == 2

        assert all(timestamp > 0 for _, _, timestamp in observed)
        probe.close()
        probe.close()
        checking_chain = False
        close_first()
        close_second()
        close_first()
        close_second()
        count = len(observed)
        mode[0] = (64, 48, bytes((197, 181, 29, 255)))
        _wait(samples, lambda value: value.dimensions == mode[0][:2] and value.pixel == mode[0][2],
              "Removing passive filters interrupted source rendering")
        assert len(observed) == count
    # Keep fixture callbacks alive through context shutdown as OBS requires.
    assert owners is not None or scenario == "passive"
    print(_REPORT + json.dumps({"observed": len(observed), "rendered": rendered_count,
                               "updates": len(signals)}))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    _scenario(sys.argv[1])
