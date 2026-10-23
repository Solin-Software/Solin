"""start/stop_meeting_output over the real libobs sidecar IPC (fake bridge executable)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from solin.core.scenes.engine import MeetingOutputEvent, MeetingOutputSettings, SceneEngineStatus
from solin.core.scenes.libobs_engine import create_libobs_scene_engine
from solin.core.scenes.recording import AudioDeviceSelection, AudioSelectionMode

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(importlib.util.find_spec("pylibobs") is None or sys.platform == "win32",
                       reason="Native libobs on a POSIX host required"),
]

_BRIDGE = Path(__file__).resolve().parents[1] / "fixtures" / "fake_galene_bridge.py"


def test_meeting_output_round_trip(tmp_path, monkeypatch):
    report = tmp_path / "bridge.json"
    monkeypatch.setenv("FAKE_BRIDGE_REPORT", str(report))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    events: list[MeetingOutputEvent] = []
    got_live = threading.Event()

    def on_event(event: object) -> None:
        if isinstance(event, MeetingOutputEvent):
            events.append(event)
            if event.state == "live":
                got_live.set()

    engine = create_libobs_scene_engine()
    engine.subscribe(on_event)
    try:
        engine.start(session_id="meetingoutputtest", deadline_ms=30_000).result(timeout=40.0)
        deadline = time.monotonic() + 15.0
        while engine.health.status != SceneEngineStatus.READY and time.monotonic() < deadline:
            time.sleep(0.05)
        assert engine.health.status == SceneEngineStatus.READY
        settings = MeetingOutputSettings(
            bridge=str(_BRIDGE), whip_url="http://127.0.0.1:9/group/x/.whip", token="t",
            width=320, height=180, video_kbps=300,
            hall_feed=AudioDeviceSelection(AudioSelectionMode.NONE),
        )
        ack = engine.start_meeting_output(settings, request_id="meeting-start", deadline_ms=15_000).result(20.0)
        assert ack.applied, ack
        assert got_live.wait(10.0), events
        time.sleep(2.0)
        stop = engine.stop_meeting_output(request_id="meeting-stop", deadline_ms=10_000).result(15.0)
        assert stop.applied
    finally:
        engine.stop()
    counts = json.loads(report.read_text())
    assert counts["argv"][:3] == ["publish", "--whip", "http://127.0.0.1:9/group/x/.whip"]
    assert counts["pcm"] > 40, counts
    # The program may be idle (no scene hydrated) but the encoder still produces frames.
    assert counts["video"] > 0, counts
    assert [e.state for e in events][:1] == ["starting"]
