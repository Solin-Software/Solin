"""HostedMeetingService relays the bridge's video slot events to the scene engine."""

from __future__ import annotations

import json
import secrets
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from solin.core.foundation.settings_store import SettingsStore
from solin.core.hosted_meeting.service import HostedMeetingService
from solin.core.hosted_meeting.settings import HostedMeetingSettingsStore


class FakeEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def set_participant_video(self, slot, active, *, request_id, deadline_ms):
        self.calls.append((slot, active))
        future: Future = Future()
        future.set_result(SimpleNamespace(applied=True))
        return future

    def stop_meeting_output(self, *, request_id, deadline_ms):
        future: Future = Future()
        future.set_result(SimpleNamespace(applied=True))
        return future


@pytest.fixture
def service(tmp_path):
    settings = SettingsStore.for_namespace("SolinTests", f"hosted-meeting-video-{secrets.token_hex(4)}")
    engine = FakeEngine()
    service = HostedMeetingService(HostedMeetingSettingsStore(settings), tmp_path,
                                   scene_runtime=SimpleNamespace(scene_engine=engine))
    service.engine = engine  # type: ignore[attr-defined]
    yield service
    for key in settings.all_keys():
        settings.set_value(key, None)


def line(**event) -> str:
    return json.dumps(event)


def test_video_events_reach_the_engine_and_the_console(service):
    engine = service.engine
    service._on_receiver_line("2026/10/24 stream abc from \"Ana\" started")  # a log line, ignored
    service._on_receiver_line(line(event="stream", id="a", user="Ana", state="started"))
    service._on_receiver_line(line(event="video", slot=1, id="a", user="Ana", state="started"))
    service._on_receiver_line(line(event="video", slot=2, id="b", user="Bruno", state="started"))
    QCoreApplication.processEvents()
    assert engine.calls == [(1, True), (2, True)]
    assert service.info["participantVideo"] == {"1": "Ana", "2": "Bruno"}

    service._on_receiver_line(line(event="video", slot=1, id="a", user="Ana", state="stopped"))
    QCoreApplication.processEvents()
    assert engine.calls[-1] == (1, False)
    assert service.info["participantVideo"] == {"2": "Bruno"}


@pytest.mark.parametrize("bad", [
    line(event="video", slot=3, id="x", user="X", state="started"),
    line(event="video", slot=True, id="x", user="X", state="started"),
    line(event="video", slot=1, id="x", user="X", state="paused"),
    "{not json",
])
def test_malformed_video_events_are_ignored(service, bad):
    service._on_receiver_line(bad)
    QCoreApplication.processEvents()
    assert service.engine.calls == []
    assert service.info["participantVideo"] == {}


def test_stopping_the_output_clears_every_held_slot(service):
    service._on_receiver_line(line(event="video", slot=2, id="b", user="Bruno", state="started"))
    QCoreApplication.processEvents()
    service._stop_output()
    assert service.engine.calls == [(2, True), (2, False)]
    assert service.info["participantVideo"] == {}
