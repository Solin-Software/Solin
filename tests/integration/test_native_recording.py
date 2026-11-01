"""Qualify encoded dimensions and decodability using the bundled media runtime."""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.integration

_CANVAS_WIDTH, _CANVAS_HEIGHT, _FPS = 320, 180, 10
_RECORDING_FRAMES = 10
_RECORDING_TIMEOUT_SECONDS = 20


def _wait_for_recorded_frames(output) -> None:
    """Observe encoded video progress before finalizing an actual MP4."""
    deadline = time.monotonic() + _RECORDING_TIMEOUT_SECONDS
    while output.active and time.monotonic() < deadline:
        if output.total_frames >= _RECORDING_FRAMES:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"Recording produced only {output.total_frames} video frames "
        f"(target: {_RECORDING_FRAMES}, last error: {output.last_error})"
    )


def _boxes(data: bytes):
    offset = 0
    while offset + 8 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = len(data) - offset
        assert header <= size <= len(data) - offset, "Invalid MP4 box"
        yield kind, data[offset + header:offset + size]
        offset += size


def _video_dimensions(path: Path) -> tuple[int, int]:
    for kind, movie in _boxes(path.read_bytes()):
        if kind != b"moov":
            continue
        for kind, track in _boxes(movie):
            if kind != b"trak":
                continue
            for kind, header in _boxes(track):
                if kind == b"tkhd":
                    width, height = struct.unpack_from(">II", header, len(header) - 8)
                    if width and height:
                        return width >> 16, height >> 16
    raise AssertionError("MP4 has no video track dimensions")


def _record(directory: Path) -> None:
    from solin.core.media.media_extract import MediaExtractRequest, MediaExtractService
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.scenes.libobs_recorder import LibobsRecorder

    runtime = ObsRuntime()
    reports = []
    with ExitStack() as stack:
        stack.callback(runtime.shutdown)
        # A small canvas exercises independent encoder scaling without making
        # GPU throughput a prerequisite of the recording contract.
        runtime.ensure_started(width=_CANVAS_WIDTH, height=_CANVAS_HEIGHT, fps=_FPS)
        source = runtime.ob.Source.create("color_source_v3", "recording-fixture", {
            "width": _CANVAS_WIDTH, "height": _CANVAS_HEIGHT, "color": 0xFF3388DD,
        })
        stack.callback(source.release)
        channel = runtime.acquire_channel()
        stack.callback(runtime.release_channel, channel)
        runtime.set_channel_source(channel, source)
        recorder = LibobsRecorder(runtime)
        stack.callback(recorder.stop)
        for height in (360, 540, 720, 1080):
            path = directory / f"recording-{height}.mp4"
            start = time.monotonic()
            assert recorder.start(str(path), width=height * 16 // 9, height=height)
            encoder_id = recorder._video_encoder.id
            _wait_for_recorded_frames(recorder._output)
            recorder.stop()
            assert _video_dimensions(path) == (height * 16 // 9, height)
            assert (runtime.video.width, runtime.video.height) == (_CANVAS_WIDTH, _CANVAS_HEIGHT)
            decoded = MediaExtractService().extract(MediaExtractRequest(str(path)))
            assert decoded.has_video and decoded.image is not None
            assert decoded.duration_ms >= 500
            # A decoded colored frame rules out a valid container with black output.
            assert max(decoded.image.data[0:3]) - min(decoded.image.data[0:3]) > 30
            reports.append({
                "height": height, "encoder": encoder_id, "bytes": path.stat().st_size,
                "duration_ms": decoded.duration_ms,
                "elapsed_ms": round((time.monotonic() - start) * 1000),
            })
    (directory / "report.json").write_text(json.dumps(reports), encoding="utf-8")


def test_native_recording_encoder_scale_does_not_resize_program():
    from solin.core.media.obs_runtime import ObsRuntime

    runtime = ObsRuntime()
    with ExitStack() as stack:
        stack.callback(runtime.shutdown)
        runtime.ensure_started(width=_CANVAS_WIDTH, height=_CANVAS_HEIGHT, fps=_FPS)
        for height in (360, 540, 720, 1080):
            encoder = runtime.ob.VideoEncoder.create("obs_x264", "recording-scale-check")
            try:
                runtime.scale_video_encoder(encoder, width=height * 16 // 9, height=height)
                assert (runtime.video.width, runtime.video.height) == (
                    _CANVAS_WIDTH, _CANVAS_HEIGHT,
                )
            finally:
                encoder.release()


@pytest.mark.skipif(
    sys.platform == "darwin" and os.environ.get("GITHUB_ACTIONS") == "true",
    reason="GitHub-hosted macOS virtual GPUs cannot reliably produce encoded video frames",
)
@pytest.mark.parametrize("hardware", [False, True], ids=["software", "hardware-with-fallback"])
def test_native_recording_resolutions_are_independent_and_playable(tmp_path, hardware):
    environment = dict(os.environ, SOLIN_RECORD_HW_ENCODE="1" if hardware else "0")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        capture_output=True, text=True, env=environment, timeout=120, check=False,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-12000:]
    reports = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    print("NATIVE_RECORDING", json.dumps(reports))
    assert [report["height"] for report in reports] == [360, 540, 720, 1080]


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    _record(Path(sys.argv[1]))
