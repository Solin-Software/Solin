from __future__ import annotations

import json
import sys
import textwrap

import pytest

from solin.core.conference.media_ring import MediaRingWriter
from solin.core.conference.publisher_service import (
    MeetingPublisherService,
    PublisherConfig,
    PublisherState,
)
from solin.core.scenes.engine import MEETING_AUDIO_MIX_INDEX
from tests._qt import wait_until

# A stand-in for the publisher worker, run as a plain script; behaves per argv[1].
FAKE_WORKER = textwrap.dedent(
    """\
    import json, sys, time
    mode, log = sys.argv[1], sys.argv[2]
    if mode == "silent":
        time.sleep(30)
    if mode == "garbage":
        print("not json", flush=True)
        time.sleep(30)
    print(json.dumps({"ready": 1}), flush=True)
    start = json.loads(sys.stdin.readline())
    with open(log, "a") as handle:
        handle.write(json.dumps(start) + "\\n")
    if mode == "crash":
        print(json.dumps({"error": "connect_failed"}), flush=True)
        sys.exit(2)
    print(json.dumps({"state": "connecting"}), flush=True)
    print(json.dumps({"state": "publishing"}), flush=True)
    print(json.dumps({"stats": {"video_frames": 3}}), flush=True)
    for line in sys.stdin:
        if json.loads(line).get("command") == "stop":
            sys.exit(0)
    """
)

CONFIG = PublisherConfig(url="ws://127.0.0.1:7880", api_key="SOLkey", api_secret="s" * 43,
                         room="meeting", width=320, height=180, fps=15, e2ee_key="k")


@pytest.fixture
def make_service(tmp_path):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    log = tmp_path / "starts.jsonl"
    services: list[MeetingPublisherService] = []

    def make(mode: str, **kwargs) -> MeetingPublisherService:
        service = MeetingPublisherService(
            command=lambda: (sys.executable, [str(script), mode, str(log)]), **kwargs
        )
        services.append(service)
        return service

    make.starts = lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    yield make
    for service in services:
        service.stop()
        service.deleteLater()


def test_start_announces_rings_and_reaches_publishing(make_service):
    service = make_service("ok")
    states, egress, stats = [], [], []
    service.state_changed.connect(states.append)
    service.egress_changed.connect(egress.append)
    service.stats_changed.connect(stats.append)

    service.start(CONFIG)
    wait_until(lambda: service.state is PublisherState.PUBLISHING, description="publishing",
               timeout_ms=10000)
    descriptor = egress[0]
    assert (descriptor.width, descriptor.height, descriptor.fps) == (320, 180, 15)
    assert descriptor.audio_mix == MEETING_AUDIO_MIX_INDEX
    # The sidecar's side can attach to both rings the service created.
    for name in (descriptor.video_ring, descriptor.audio_ring):
        MediaRingWriter(name).close()

    (start,) = make_service.starts()
    assert start["command"] == "start"
    assert start["video_ring"] == descriptor.video_ring and start["e2ee_key"] == "k"
    assert start["token"].count(".") == 2
    wait_until(lambda: stats, description="stats", timeout_ms=5000)
    assert stats[0] == {"video_frames": 3}
    assert states[:3] == ["starting", "connecting", "publishing"]

    service.stop()
    assert egress[-1] is None and service.state is PublisherState.STOPPED
    with pytest.raises(FileNotFoundError):
        MediaRingWriter(descriptor.video_ring)


def test_crashes_relaunch_against_the_same_rings_then_fail(make_service):
    service = make_service("crash", max_restarts=2)
    egress, errors = [], []
    service.egress_changed.connect(egress.append)
    service.error.connect(errors.append)
    service._restart_delay_ms = 1  # noqa: SLF001 - keep the test fast

    service.start(CONFIG)
    service._restart_delay_ms = 1  # noqa: SLF001 - start() resets it
    wait_until(lambda: service.state is PublisherState.FAILED, description="failed",
               timeout_ms=20000)
    starts = make_service.starts()
    assert len(starts) == 3
    assert len({s["video_ring"] for s in starts}) == 1
    assert len(egress) == 1  # restarts never re-announce the rings
    assert "connect_failed" in errors[0]


@pytest.mark.parametrize("mode", ["silent", "garbage"])
def test_a_worker_without_a_valid_handshake_is_restarted(make_service, mode):
    service = make_service(mode, handshake_timeout_ms=500)
    service.start(CONFIG)
    wait_until(lambda: service.state is PublisherState.RESTARTING, description="restarting",
               timeout_ms=5000)
    service.stop()
    assert service.state is PublisherState.STOPPED


def test_invalid_config_creates_nothing(make_service):
    service = make_service("ok")
    egress = []
    service.egress_changed.connect(egress.append)
    with pytest.raises(ValueError):
        service.start(PublisherConfig(url="http://nope", api_key="k", api_secret="s", room="r"))
    assert egress == [] and service.state is PublisherState.STOPPED
