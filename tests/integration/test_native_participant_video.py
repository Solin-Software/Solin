"""A remote part-holder's camera, end to end, into a libobs scene source.

A child process runs a real Galene, publishes a "part-holder" stream into it with the
libobs meeting output and the real bridge (user "Ana", 320×180), runs ``bridge receive
--video-ports`` as the hall, and shows video slot 1 with the runtime's
``participant_video_source``. The slot must decode Ana's video, and clear when she stops.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time

import pytest

from solin.core.hosted_meeting.binaries import Helper, find_helper

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(importlib.util.find_spec("pylibobs") is None or sys.platform == "win32",
                       reason="Native libobs on a POSIX host required"),
    pytest.mark.skipif(find_helper(Helper.GALENE) is None or find_helper(Helper.BRIDGE) is None,
                       reason="Galene and solin-galene-bridge binaries not available"),
    # `receive` opens a playback device and libobs needs a rendering session; hosted CI
    # runners have neither. Run locally or with SOLIN_TEST_NATIVE_SIDECAR=1.
    pytest.mark.skipif(bool(os.environ.get("CI")) and not os.environ.get("SOLIN_TEST_NATIVE_SIDECAR"),
                       reason="needs a desktop session and an audio device"),
]

_TIMEOUT = 90


def _free_even_udp_port() -> int:
    """A free UDP port pair (RTP, RTCP) for a test slot, clear of a running Solin's."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1] & ~1
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a, \
                    socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b:
                a.bind(("127.0.0.1", port))
                b.bind(("127.0.0.1", port + 1))
            return port
        except OSError:
            continue
    raise RuntimeError("no free UDP port pair")


def _scenario(directory: Path) -> dict:
    from solin.core.hosted_meeting import participant_video
    from solin.core.hosted_meeting.galene_server import GaleneServer
    from solin.core.hosted_meeting.tokens import OBSERVE, PRESENT, new_group_key, sign_token
    from solin.core.media.obs_runtime import ObsRuntime
    from solin.core.scenes.libobs_meeting_output import LibobsMeetingOutput, MeetingOutputRequest

    participant_video.SLOT_PORTS = (_free_even_udp_port(), _free_even_udp_port())
    participant_video.default_sdp_directory = lambda: directory / "sdp"  # type: ignore[assignment]
    bridge = str(find_helper(Helper.BRIDGE))
    report: dict = {"events": []}

    galene = GaleneServer(directory / "galene")
    galene.start()
    slug = "video-slot-test"
    key = new_group_key()
    galene.layout.write_group(slug, key)
    audience = f"https://solin.invalid/group/{slug}/"
    receiver = subprocess.Popen(  # noqa: S603 - test helper
        [bridge, "receive", "--ws", f"ws://127.0.0.1:{galene.port}/ws", "--group", slug,
         "--token", sign_token(key, username="Solin hall", audience=audience, permissions=OBSERVE, ttl_seconds=600),
         "--ignore-user", "Solin program", "--video-ports", participant_video.bridge_ports_argument()],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    events: queue.Queue = queue.Queue()

    def pump() -> None:
        assert receiver.stdout is not None
        for line in receiver.stdout:
            if line.startswith("{"):
                event = json.loads(line)
                report["events"].append(event)
                events.put(event)

    threading.Thread(target=pump, daemon=True).start()

    def wait_event(predicate, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if predicate(event):
                return event
        return None

    runtime = ObsRuntime()
    try:
        runtime.ensure_started(width=640, height=360, fps=30)
        # The slot is drawn under an opaque color, so the published program stays a
        # plain color while the slot source is still rendered (and decodes).
        slot_channel = runtime.acquire_channel()
        slot_source = runtime.participant_video_source(1)
        runtime.set_channel_source(slot_channel, slot_source)
        color_channel = runtime.acquire_channel()
        color = runtime.ob.Source.create("color_source_v3", "participant-test-color",
                                         {"color": 0xFF3366CC, "width": 640, "height": 360})
        runtime.set_channel_source(color_channel, color)
        report["receiver_connected"] = wait_event(
            lambda e: e.get("event") == "state" and e.get("state") == "connected", 15) is not None

        output = LibobsMeetingOutput(runtime)
        report["published"] = output.start(MeetingOutputRequest(
            bridge=bridge, whip_url=f"http://127.0.0.1:{galene.port}/group/{slug}/.whip",
            token=sign_token(key, username="Ana", audience=audience, permissions=PRESENT, ttl_seconds=600),
            width=320, height=180, video_kbps=300, hall_feed_mode="none",
        ))
        started = wait_event(lambda e: e.get("event") == "video", 20)
        report["video_started"] = started
        began = time.monotonic()
        deadline = began + 15
        while time.monotonic() < deadline and slot_source.width == 0:
            time.sleep(0.05)
        report["slot_size"] = [slot_source.width, slot_source.height]
        report["first_frame_s"] = round(time.monotonic() - began, 2)

        output.stop()
        report["video_stopped"] = wait_event(
            lambda e: e.get("event") == "video" and e.get("state") == "stopped", 15)
        runtime.stop_participant_video(1)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and slot_source.width != 0:
            time.sleep(0.05)
        report["slot_size_after_stop"] = [slot_source.width, slot_source.height]

        for channel in (slot_channel, color_channel):
            runtime.set_channel_source(channel, None)
            runtime.release_channel(channel)
        color.release()
        del color, slot_source
    finally:
        runtime.shutdown()
        receiver.terminate()
        receiver.wait(5)
        galene.stop()
    return report


def test_part_holder_video_reaches_a_scene_slot(tmp_path):
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    result = subprocess.run([sys.executable, "-B", "-X", "faulthandler", str(Path(__file__).resolve()), str(tmp_path)],
                            capture_output=True, text=True, timeout=_TIMEOUT, check=False, env=environment)
    tail = "\n".join((result.stdout + result.stderr).splitlines()[-60:])
    assert result.returncode == 0, tail
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["receiver_connected"], report
    assert report["published"], report
    started = report["video_started"]
    assert started and started["state"] == "started" and started["slot"] == 1 and started["user"] == "Ana", report
    assert report["slot_size"] == [320, 180], report
    stopped = report["video_stopped"]
    assert stopped and stopped["slot"] == 1, report
    assert report["slot_size_after_stop"] == [0, 0], report


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    out = Path(sys.argv[1])
    result = _scenario(out)
    (out / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
