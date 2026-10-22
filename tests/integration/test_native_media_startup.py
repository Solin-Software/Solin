"""Native startup frame and transport tests; video readiness is not a seek acknowledgement."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import json
from queue import SimpleQueue
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time

import pytest


pytestmark = pytest.mark.integration

_SIZE = 64
_FPS = 10
_DURATION_SECONDS = 10
_TARGET_MS = 2000
_RED_BGRA = bytes((0, 0, 255, 255))
_BLUE_BGRA = bytes((255, 0, 0, 255))


def _write_segmented_avi(path: Path, *, duration_seconds: float = _DURATION_SECONDS) -> None:
    """Write a seekable uncompressed AVI; every frame is independently decodable."""
    frame_size = _SIZE * _SIZE * 3
    frame_count = int(_FPS * duration_seconds)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return kind + struct.pack("<I", len(data)) + data + b"\0" * (len(data) % 2)

    header = struct.pack(
        "<14I", 1_000_000 // _FPS, frame_size * _FPS, 0, 0x10, frame_count,
        0, 1, frame_size, _SIZE, _SIZE, 0, 0, 0, 0,
    )
    stream = struct.pack(
        "<4s4sIHH8I4h", b"vids", b"DIB ", 0, 0, 0,
        0, 1, _FPS, 0, frame_count, frame_size, 0xFFFFFFFF, 0,
        0, 0, _SIZE, _SIZE,
    )
    bitmap = struct.pack("<IiiHHIIiiII", 40, _SIZE, _SIZE, 1, 24, 0, frame_size, 0, 0, 0, 0)
    stream_list = chunk(b"LIST", b"strl" + chunk(b"strh", stream) + chunk(b"strf", bitmap))
    headers = chunk(b"LIST", b"hdrl" + chunk(b"avih", header) + stream_list)
    frames = []
    index = []
    offset = 4  # idx1 offsets are relative to the movi list's data.
    for number in range(frame_count):
        pixel = _RED_BGRA[:3] if number < _TARGET_MS * _FPS // 1000 else _BLUE_BGRA[:3]
        frame = chunk(b"00db", pixel * (_SIZE * _SIZE))
        frames.append(frame)
        index.append(struct.pack("<4sIII", b"00db", 0x10, offset, frame_size))
        offset += len(frame)
    data = b"AVI " + headers + chunk(b"LIST", b"movi" + b"".join(frames))
    data += chunk(b"idx1", b"".join(index))
    path.write_bytes(b"RIFF" + struct.pack("<I", len(data)) + data)


@contextmanager
def _serve_clip(path: Path) -> Iterator[str]:
    from tests._http import ThreadingLoopbackHTTPServer

    data = path.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            start, end = 0, len(data) - 1
            byte_range = self.headers.get("Range")
            if byte_range is not None:
                unit, interval = byte_range.split("=", 1)
                assert unit == "bytes"
                first, last = interval.split("-", 1)
                start = int(first or 0)
                end = min(int(last) if last else end, end)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            self.send_response(206 if byte_range is not None else 200)
            self.send_header("Content-Type", "video/x-msvideo")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if byte_range is not None:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.end_headers()
            try:
                self.wfile.write(data[start:end + 1])
            except (BrokenPipeError, ConnectionResetError):
                # Decoder rebuild/teardown can cancel an HTTP read in progress.
                return

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingLoopbackHTTPServer(Handler)
    thread = Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{path.name}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "The fixture HTTP server did not terminate"


@contextmanager
def _native_media() -> Iterator[tuple[object, object, Path]]:
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.media.obs_source_render import shutdown as shutdown_readback
    from solin.core.scenes.libobs_media_source import LibobsMediaSource

    directory = TemporaryDirectory()
    runtime = ObsRuntime()
    media = LibobsMediaSource(runtime)
    try:
        path = Path(directory.name) / "red-then-blue.avi"
        _write_segmented_avi(path)
        runtime.ensure_started(width=_SIZE, height=_SIZE, fps=_FPS)
        yield runtime, media, path
    finally:
        try:
            media.close()
        finally:
            shutdown_readback()
            runtime.shutdown()
            # Native decoder handles must be released before Windows removes the AVI.
            directory.cleanup()


@contextmanager
def _hold_tick(runtime) -> Iterator[None]:
    entered = Event()
    release = Event()
    expired = Event()

    def hold(_seconds: float) -> None:
        if not entered.is_set():
            entered.set()
            if not release.wait(5):
                expired.set()

    callback = runtime.ob.add_tick_callback(hold)
    try:
        assert entered.wait(5), "OBS never reached the tick barrier"
        yield
    finally:
        release.set()
        runtime.ob.remove_tick_callback(callback)
        assert not expired.is_set(), "Control operations exceeded the tick barrier deadline"


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(1 / 240)
    return False


@contextmanager
def _open_before_first_input_tick(runtime, media, path: Path) -> Iterator[None]:
    """Gate only the native input update; source/callback registration must run first."""
    original_update = runtime.ob.Source.update
    gated = Event()
    with ExitStack() as barriers, pytest.MonkeyPatch.context() as patch:
        def update(source, settings):
            if source is media.source and settings.get("local_file") == str(path):
                assert not gated.is_set(), "The initial input was updated more than once"
                barriers.enter_context(_hold_tick(runtime))
                gated.set()
            return original_update(source, settings)

        patch.setattr(runtime.ob.Source, "update", update)
        assert media.open(str(path), autoplay=False)
        assert gated.is_set(), "The native first-input barrier was never installed"
        yield


def _observation(media) -> dict[str, object]:
    source = media.source
    return {
        "native_state": source.media_state,
        "reported_state": media.state,
        "position_ms": media.position_ms,
        "volume": source.volume,
        "video_ready": media._video_readiness.ready,
    }


def _capture_prepared_source(runtime, media) -> dict[str, object]:
    """Read an actual native texture in the render phase, before any scene Take."""
    from solin.core.media.obs_source_render import render_source_to_bgra

    result = SimpleQueue()
    captured = Event()

    def sample(_width: int, _height: int) -> None:
        if captured.is_set() or not media._video_readiness.ready:
            return
        try:
            frame = render_source_to_bgra(
                media.source, _SIZE, _SIZE, canvas_width=_SIZE, canvas_height=_SIZE,
            )
            if frame is None:
                return
            pixels, stride = frame
            offset = (_SIZE // 2) * stride + (_SIZE // 2) * 4
            result.put({**_observation(media), "center_bgra": pixels[offset:offset + 4].hex()})
        except Exception as error:  # noqa: BLE001 - forward callback failures to the control thread
            result.put(error)
        captured.set()

    callback = runtime.ob.add_main_render_callback(sample)
    try:
        observation = result.get(timeout=5)
        if isinstance(observation, Exception):
            raise observation
        return observation
    finally:
        runtime.ob.remove_main_render_callback(callback)


def _assert_stop_before_first_input_update() -> None:
    from solin.core.scenes.libobs_media_source import STATE_STOPPED

    with _native_media() as (runtime, media, path):
        with _open_before_first_input_tick(runtime, media, path):
            media.stop()

        def stopped() -> bool:
            media.apply_pending_resume()
            return media.source.media_state == STATE_STOPPED

        acknowledged = _wait_for(stopped)
        observation = _observation(media)
        print("stop_before_update", observation, flush=True)
        assert acknowledged, f"Stop was lost before input creation: {observation}"
        states = SimpleQueue()
        callback = runtime.ob.add_main_render_callback(
            lambda _width, _height: states.put(media.source.media_state),
        )
        try:
            samples = [states.get(timeout=5) for _ in range(8)]
        finally:
            runtime.ob.remove_main_render_callback(callback)
        assert samples == [STATE_STOPPED] * 8, samples


def _write_video_offset_clip(path: Path, *, audio_first: bool = False) -> Path:
    offset_path = path.with_suffix(".mkv")
    _write_segmented_avi(path, duration_seconds=7)
    mapping = ["-map", "1:a", "-map", "[v]"] if audio_first else ["-map", "[v]", "-map", "1:a"]
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=8",
        "-filter_complex", "[0:v]setpts=PTS+1/TB[v]",
        *mapping, "-c:v", "ffv1", "-g", "1", "-pix_fmt", "bgr0",
        "-c:a", "pcm_s16le", str(offset_path),
    ], capture_output=True, text=True, timeout=15, check=True)
    streams = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time",
        "-of", "json", str(offset_path),
    ], text=True, timeout=5))["streams"]
    starts = {stream["codec_type"]: float(stream["start_time"]) for stream in streams}
    assert starts == {"video": 1.0, "audio": 0.0}, starts
    return offset_path


def _assert_visual_wait_does_not_pause_before_the_first_video(*, poll_before_wait: bool = False) -> None:
    from solin.core.scenes.libobs_media_source import STATE_PAUSED

    with _native_media() as (runtime, media, path), pytest.MonkeyPatch.context() as patch:
        offset_path = _write_video_offset_clip(path, audio_first=True)
        pauses = []
        original_pause = runtime.ob.Source.media_play_pause

        def pause(source, paused):
            if source is media.source and paused:
                pauses.append({**_observation(media), "width": source.width, "height": source.height})
            return original_pause(source, paused)

        patch.setattr(runtime.ob.Source, "media_play_pause", pause)
        assert media.open(str(offset_path), autoplay=False)
        if poll_before_wait:
            def initially_paused():
                media.apply_pending_resume()
                return media.source.media_state == STATE_PAUSED

            assert _wait_for(initially_paused), _observation(media)
            assert not media._video_readiness.ready, _observation(media)
        ready = media.wait_for_video_frame(deadline=time.monotonic() + 4)
        observation = {**_observation(media), "take_ready": ready, "pause_requests": pauses}
        print("visual_wait_audio_before_video", observation, flush=True)
        assert ready, observation
        frame = _capture_prepared_source(runtime, media)
        assert frame["native_state"] == STATE_PAUSED, frame
        assert frame["center_bgra"] == _RED_BGRA.hex(), frame


def _assert_speed_before_initial_pause_acknowledgement() -> None:
    from solin.core.scenes.libobs_media_source import STATE_PAUSED

    with _native_media() as (runtime, media, path), _serve_clip(path) as url:
        assert media.open(url, autoplay=False)
        # Watch the native upload directly; wait_for_video_frame would also queue the pause.
        assert _wait_for(lambda: media._video_readiness.ready), _observation(media)
        with _hold_tick(runtime):
            media.apply_pending_resume()
            assert media.source.media_state != STATE_PAUSED, "Tick barrier did not precede pause ack"
            media.set_speed(150)
        ready = media.wait_for_video_frame(deadline=time.monotonic() + 4)
        observation = _observation(media)
        print("speed_before_pause_ack", {**observation, "take_ready": ready}, flush=True)
        assert ready, f"Rate rebuild stranded the initial pause: {observation}"
        assert observation["native_state"] == STATE_PAUSED, observation
        frame = _capture_prepared_source(runtime, media)
        assert frame["center_bgra"] == _RED_BGRA.hex(), frame


def _assert_startup_forwards_the_requested_seek(scenario: str) -> None:
    from solin.core.scenes.libobs_media_source import STATE_PAUSED

    with _native_media() as (runtime, media, path), pytest.MonkeyPatch.context() as patch:
        requested = []
        original_time = runtime.ob.Source.media_time

        def set_time(source, target):
            if source is media.source:
                requested.append(target)
            original_time.fset(source, target)

        patch.setattr(runtime.ob.Source, "media_time", property(original_time.fget, set_time))
        if scenario == "delayed":
            _write_segmented_avi(path, duration_seconds=2.2)
        elif scenario in ("offset", "audio-rearm"):
            path = _write_video_offset_clip(path, audio_first=scenario == "audio-rearm")

        target = _TARGET_MS
        if scenario == "initial":
            with _open_before_first_input_tick(runtime, media, path):
                media.seek(target)
        else:
            assert media.open(str(path), autoplay=False)
            if scenario == "audio-rearm":
                media.seek(1000)
                media.seek(target)

                def audio_first_pause_acknowledged():
                    media.apply_pending_resume()
                    return media.source.media_state == STATE_PAUSED and media._startup is None

                assert _wait_for(audio_first_pause_acknowledged), _observation(media)
                assert not media._video_readiness.ready, _observation(media)
            else:
                assert _wait_for(lambda: media._video_readiness.ready), _observation(media)
                if scenario == "delayed":
                    assert _wait_for(lambda: media.position_ms >= 400), _observation(media)
                if scenario == "pause-ack":
                    media.apply_pending_resume()
                    assert _wait_for(lambda: media.source.media_state == STATE_PAUSED), _observation(media)
                if scenario == "latest":
                    target = 1000
                    with _hold_tick(runtime):
                        media.seek(2000)
                        media.apply_pending_resume()
                        media.seek(target)
                        media.apply_pending_resume()
                else:
                    media.seek(target)

        assert media.wait_for_video_frame(deadline=time.monotonic() + 4), _observation(media)
        assert requested and requested[-1] == target, requested
        if scenario == "audio-rearm":
            assert requested == [target, target], requested
        frame = _capture_prepared_source(runtime, media)
        print("startup_seek_request", {"scenario": scenario, "requested": requested, **frame}, flush=True)
        assert frame["native_state"] == STATE_PAUSED, frame
        # Readiness proves a real fixture texture, not the picture at the seek target.
        assert frame["center_bgra"] in {_RED_BGRA.hex(), _BLUE_BGRA.hex()}, frame


def _run_native(scenario: str) -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()), scenario],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as error:
        output = []
        for stream in (error.stdout, error.stderr):
            output.append(stream.decode(errors="replace") if isinstance(stream, bytes) else stream or "")
        raise AssertionError(f"Native scenario {scenario!r} timed out:\n{''.join(output)}") from error
    # Keep the native assertion and teardown diagnostics without repeating the plugin inventory.
    diagnostics = "\n".join((result.stdout + result.stderr).splitlines()[-80:])
    assert result.returncode == 0, f"Native scenario {scenario!r} exited {result.returncode}:\n{diagnostics}"


def _assert_system_default_audio_monitoring() -> None:
    from pylibobs._ffi import ffi, get_lib
    from solin.core.scenes.libobs_media_source import STATE_PAUSED

    with _native_media() as (runtime, media, path):
        assert runtime.ob.audio_monitoring_available()
        assert runtime.ob.get_audio_monitoring_device()[1] == "default"
        if sys.platform == "win32":
            # This module subscribes to OS default-output changes, including in
            # the headless sidecar. Without it, the sentinel alone is insufficient.
            assert "wasapi_output_capture" in runtime.ob.enum_source_types()
        audio = Path(__file__).resolve().parents[1] / "fixtures" / "media_extract" / "audio.mp3"
        for media_path in (path, audio):
            assert media.open(str(media_path), autoplay=False, volume_percent=0)

            def paused():
                media.apply_pending_resume()
                return (
                    media.source.media_state == STATE_PAUSED
                    and media._startup is None
                )

            assert _wait_for(paused), _observation(media)
            source = media.source
            reset_done = Event()

            @ffi.callback("void(void *)")
            def reset_monitoring(_parameter, completed=reset_done):
                runtime._context.reset_audio_monitoring()
                completed.set()

            # Native device-change handlers reset monitoring, not the decoder.
            # Exercise that boundary without changing the operator's OS settings.
            if sys.platform == "win32":
                # win-wasapi dispatches through OBS_TASK_UI while IPC can be idle.
                get_lib().obs_queue_task(
                    0, ffi.cast("obs_task_t", reset_monitoring), ffi.NULL, False,
                )
                assert reset_done.wait(4), "Native default-output reset was not dispatched"
            else:
                reset_monitoring(ffi.NULL)

            assert media.source is source
            assert source.media_state == STATE_PAUSED
            assert runtime.ob.get_audio_monitoring_device()[1] == "default"

        if sys.platform == "win32":
            import ctypes

            ole32 = ctypes.WinDLL("ole32")
            ole32.CoGetApartmentType.argtypes = (
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            )
            ole32.CoGetApartmentType.restype = ctypes.c_int32
            apartments = []

            @ffi.callback("void(void *)")
            def inspect_apartment(_parameter):
                apartment, qualifier = ctypes.c_int(), ctypes.c_int()
                result = ole32.CoGetApartmentType(
                    ctypes.byref(apartment), ctypes.byref(qualifier),
                )
                apartments.append((result, apartment.value))

            # Monitoring resources still exist here. Their native destruction
            # must retain MTA support after new UI tasks have been rejected.
            runtime._native_tasks.quiesce()
            get_lib().obs_queue_task(
                3, ffi.cast("obs_task_t", inspect_apartment), ffi.NULL, True,
            )
            assert apartments == [(0, 1)], apartments  # S_OK, APTTYPE_MTA


def test_native_monitoring_uses_system_default_and_reset_preserves_playback() -> None:
    _run_native("audio-monitoring")


def _assert_monitor_only_media_stays_out_of_the_recording_mix() -> None:
    from array import array

    from solin.core.media.obs_runtime import (
        MONITORING_MONITOR_AND_OUTPUT,
        RECORDING_AUDIO_MIXERS,
        RECORDING_MIX_INDEX,
    )

    with _native_media() as (runtime, media, path):
        tone = path.with_name("tone.wav")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=8", "-ac", "2", str(tone)],
            check=True,
        )
        peaks: list[float] = []

        def listen(_mix, planes, _frames, _timestamp) -> None:
            samples = array("f", planes[0])
            peaks.append(max((abs(value) for value in samples), default=0.0))

        def loudest_over(seconds: float) -> float:
            peaks.clear()
            time.sleep(seconds)
            return max(peaks, default=0.0)

        assert media.open(str(tone), autoplay=True, volume_percent=100)
        scene = runtime.ob.Scene.create("program")
        scene.add(media.source)
        channel = runtime.acquire_channel()
        runtime.set_channel_source(channel, scene.as_source())
        handle = runtime.ob.add_raw_audio_callback(listen, mix_idx=RECORDING_MIX_INDEX, channels=2)
        try:
            assert _wait_for(lambda: media.position_ms > 300), _observation(media)
            # Heard on the speakers only: the system-audio capture is what puts it
            # in the recording, so a direct copy here would be heard twice.
            assert media.source.audio_mixers == 0
            assert loudest_over(1.0) < 1e-4
            # Control: the same playing source does reach the track once routed.
            # Stock libobs 32 drops monitor-only audio before any mix whatever the
            # tracks, so routing it takes monitor-and-output as well.
            runtime.set_source_monitoring(media.source, MONITORING_MONITOR_AND_OUTPUT)
            media.source.audio_mixers = RECORDING_AUDIO_MIXERS
            assert loudest_over(1.0) > 0.05
        finally:
            runtime.ob.remove_raw_audio_callback(handle, mix_idx=RECORDING_MIX_INDEX)
            runtime.set_channel_source(channel, None)
            runtime.release_channel(channel)
            scene.release()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg fixtures required")
def test_native_monitor_only_media_stays_out_of_the_recording_mix() -> None:
    _run_native("monitor-only-mix")


def _assert_audio_monitoring_reset_overlaps_media_replacement() -> None:
    from pylibobs._ffi import ffi, get_lib
    from solin.core.media.obs_runtime import MONITORING_MONITOR_ONLY

    with _native_media() as (runtime, media, path):
        assert runtime.ob.audio_monitoring_available()
        assert runtime.ob.get_audio_monitoring_device()[1] == "default"
        audio = Path(__file__).resolve().parents[1] / "fixtures" / "media_extract" / "audio.mp3"
        stop = Event()
        first_reset = Event()
        reset_completed = Event()
        resets = []
        errors = []

        @ffi.callback("void(void *)")
        def reset_monitoring(_parameter):
            try:
                runtime._context.reset_audio_monitoring()
                resets.append(True)
            except Exception as error:  # noqa: BLE001 - surface native callback failures
                errors.append(error)
            finally:
                first_reset.set()
                reset_completed.set()

        def submit_resets():
            try:
                while not stop.is_set():
                    # Match win-wasapi's asynchronous default-device notification.
                    reset_completed.clear()
                    get_lib().obs_queue_task(
                        0, ffi.cast("obs_task_t", reset_monitoring), ffi.NULL, False,
                    )
                    # Keep one notification in flight so slow device initialization
                    # cannot turn this overlap check into an unbounded queue flood.
                    if not reset_completed.wait(4):
                        raise TimeoutError("Native default-output reset did not complete")
                    stop.wait(0.01)
            except Exception as error:  # noqa: BLE001 - report producer failures to the owner
                errors.append(error)
                first_reset.set()

        producer = Thread(target=submit_resets)
        producer.start()
        try:
            assert first_reset.wait(4), "Native default-output reset was not dispatched"
            for index in range(80):
                media_path = audio if index % 2 else path
                assert media.open(str(media_path), autoplay=False, volume_percent=0)
                assert get_lib().obs_source_get_monitoring_type(
                    media.source._ptr,
                ) == MONITORING_MONITOR_ONLY, "Media monitoring was not enabled"
                # Allow resets to overlap monitor creation and deferred destruction.
                time.sleep(0.005)
                media.close()
        finally:
            stop.set()
            producer.join(timeout=3)
            assert not producer.is_alive(), "Audio reset producer did not terminate"

        # Finish accepted callbacks before inspecting their results. No destruction
        # drain or per-media exclusion may hide the lifetime race during replacement.
        get_lib().obs_queue_task(
            0, ffi.cast("obs_task_t", reset_monitoring), ffi.NULL, True,
        )
        assert not errors, errors
        assert len(resets) > 2, "Resets did not continue during media replacement"
        assert runtime.ob.get_audio_monitoring_device()[1] == "default"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native default-output notifications")
def test_native_audio_monitoring_reset_overlaps_media_replacement() -> None:
    _run_native("audio-monitoring-overlap")


def test_native_stop_before_first_input_update_is_acknowledged() -> None:
    _run_native("stop")


@pytest.mark.parametrize("scenario", ["initial", "delayed", "latest", "pause-ack"])
def test_native_startup_forwards_the_latest_seek_request(scenario) -> None:
    _run_native(f"seek-{scenario}")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg fixtures required")
@pytest.mark.parametrize("scenario", ["offset", "audio-rearm"])
def test_native_offset_video_preserves_explicit_seek_during_startup(scenario) -> None:
    _run_native(f"seek-{scenario}")


def test_native_speed_change_before_initial_pause_acknowledgement_recovers() -> None:
    _run_native("speed")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg fixtures required")
def test_native_visual_wait_does_not_pause_on_audio_before_the_first_video() -> None:
    _run_native("audio-before-video")


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg fixtures required")
def test_native_visual_wait_recovers_an_acknowledged_audio_first_pause() -> None:
    _run_native("audio-before-video-paused")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    if sys.argv[1].startswith("seek-"):
        _assert_startup_forwards_the_requested_seek(sys.argv[1].removeprefix("seek-"))
        raise SystemExit(0)
    {
        "audio-monitoring": _assert_system_default_audio_monitoring,
        "audio-monitoring-overlap": _assert_audio_monitoring_reset_overlaps_media_replacement,
        "monitor-only-mix": _assert_monitor_only_media_stays_out_of_the_recording_mix,
        "stop": _assert_stop_before_first_input_update,
        "speed": _assert_speed_before_initial_pause_acknowledgement,
        "audio-before-video": _assert_visual_wait_does_not_pause_before_the_first_video,
        "audio-before-video-paused": lambda: _assert_visual_wait_does_not_pause_before_the_first_video(poll_before_wait=True),
    }[sys.argv[1]]()
