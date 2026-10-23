"""The hosted-meeting output with real libobs: encoded program + meeting-mix PCM.

A child process boots libobs offscreen, shows a color source, starts
``LibobsMeetingOutput`` with a fake bridge that captures the framed stdin stream, and
reports what arrived. Run with ``python -m pytest tests/integration/test_native_hosted_meeting_output.py -q``.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(importlib.util.find_spec("pylibobs") is None, reason="Native libobs required"),
]

_TIMEOUT = 40


def _scenario(directory: Path) -> dict:
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.scenes.libobs_meeting_output import (
        KIND_PCM, KIND_VIDEO, LibobsMeetingOutput, MeetingOutputRequest,
    )

    class Stdin:
        def __init__(self) -> None:
            self.data = bytearray()
            self.lock = threading.Lock()
            self.video = 0
            self.pcm = 0

        def write(self, chunk: bytes) -> int:
            with self.lock:
                self.data += chunk
                # The writer sends one framed packet per write.
                if chunk[:1] == bytes([KIND_VIDEO]):
                    self.video += 1
                elif chunk[:1] == bytes([KIND_PCM]):
                    self.pcm += 1
            return len(chunk)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeBridge:
        def __init__(self, *_args, **_kwargs) -> None:
            self.stdin = Stdin()
            read_fd, self._write_fd = os.pipe()
            self.stdout = os.fdopen(read_fd, "rb")
            self.command = _args[0] if _args else []

        def terminate(self) -> None:
            if self._write_fd is not None:
                os.close(self._write_fd)
                self._write_fd = None

        def wait(self, _timeout=None) -> int:
            return 0

        def kill(self) -> None:
            self.terminate()

    bridges: list[FakeBridge] = []

    def popen(*args, **kwargs):
        bridge = FakeBridge(*args, **kwargs)
        bridges.append(bridge)
        return bridge

    runtime = ObsRuntime()
    try:
        runtime.ensure_started(width=640, height=360, fps=30)
        color = runtime.ob.Source.create("color_source_v3", "meeting-test-color",
                                         {"color": 0xFF3366CC, "width": 640, "height": 360})
        channel = runtime.acquire_channel()
        runtime.set_channel_source(channel, color)
        states: list[str] = []
        output = LibobsMeetingOutput(runtime, popen=popen, on_state=lambda s, _d: states.append(s))
        started = output.start(MeetingOutputRequest(bridge="bridge", whip_url="http://x/.whip", token="t",
                                                    width=320, height=180, video_kbps=300,
                                                    hall_feed_mode="none"))
        # Wait for enough media instead of a fixed time: slow CI runners start late.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            stdin = bridges[0].stdin if bridges else None
            if stdin is not None and stdin.video >= 60 and stdin.pcm >= 100:
                break
            time.sleep(0.1)
        data = bytes(bridges[0].stdin.data) if bridges else b""
        output.stop()
        runtime.set_channel_source(channel, None)
        runtime.release_channel(channel)
        color.release()
        del color
    finally:
        runtime.shutdown()

    frames: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset + 13 <= len(data):
        kind, pts, length = struct.unpack(">BqI", data[offset:offset + 13])
        frames.append((kind, pts, data[offset + 13:offset + 13 + length]))
        offset += 13 + length
    video = [f for f in frames if f[0] == KIND_VIDEO]
    pcm = [f for f in frames if f[0] == KIND_PCM]

    def nal_types(unit: bytes) -> list[int]:
        types, i = [], 0
        while True:
            i = unit.find(b"\x00\x00\x01", i)
            if i < 0:
                return types
            types.append(unit[i + 3] & 0x1F)
            i += 3

    first_types = nal_types(video[0][2]) if video else []
    return {
        "started": started,
        "states": states,
        "command": list(bridges[0].command) if bridges else [],
        "video_frames": len(video),
        "pcm_chunks": len(pcm),
        "first_video_nals": first_types,
        "video_pts_increasing": all(b[1] > a[1] for a, b in zip(video, video[1:], strict=False)),
        "pcm_pts_increasing": all(b[1] > a[1] for a, b in zip(pcm, pcm[1:], strict=False)),
        "clock_gap_ms": abs(video[0][1] - pcm[0][1]) / 1e6 if video and pcm else None,
        "pcm_bytes_per_chunk": len(pcm[0][2]) if pcm else 0,
        "trailing_bytes": len(data) - offset,
    }


def test_meeting_output_sends_encoded_program_and_meeting_mix(tmp_path):
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    result = subprocess.run([sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()), str(tmp_path)],
                            capture_output=True, text=True, timeout=_TIMEOUT, check=False, env=environment)
    tail = "\n".join((result.stdout + result.stderr).splitlines()[-60:])
    assert result.returncode == 0, tail
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["started"], report
    assert report["command"][:4] == ["bridge", "publish", "--whip", "http://x/.whip"]
    assert report["video_frames"] >= 60, report          # ~2 s of 30 fps video
    assert 7 in report["first_video_nals"] and 5 in report["first_video_nals"], report  # SPS + IDR first
    assert report["pcm_chunks"] >= 100, report           # 48 kHz / 1024 frames ≈ 47 per second
    assert report["pcm_bytes_per_chunk"] == 1024 * 2 * 4, report
    assert report["video_pts_increasing"] and report["pcm_pts_increasing"], report
    # Both use one system clock: the gap is the encoder's start-up delay, not a clock offset.
    assert report["clock_gap_ms"] is not None and report["clock_gap_ms"] < 5000, report
    assert report["trailing_bytes"] == 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    out = Path(sys.argv[1])
    report = _scenario(out)
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
