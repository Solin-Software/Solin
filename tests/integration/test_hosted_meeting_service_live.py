"""HostedMeetingService with a real Galene and bridge, and a fake scene engine."""

from __future__ import annotations

import asyncio
import secrets
import time
from concurrent.futures import Future
from types import SimpleNamespace

import aiohttp
import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from solin.core.foundation.settings_store import SettingsStore
from solin.core.hosted_meeting.binaries import Helper, find_helper
from solin.core.hosted_meeting.processes import free_tcp_port
from solin.core.hosted_meeting.service import HostedMeetingService, MeetingState
from solin.core.hosted_meeting.settings import HostedMeetingSettingsStore, MeetingQuality
from solin.core.hosted_meeting.tokens import decode_claims

pytestmark = pytest.mark.skipif(
    find_helper(Helper.GALENE) is None or find_helper(Helper.BRIDGE) is None,
    reason="Galene and solin-galene-bridge binaries not available",
)


class FakeEngine:
    def __init__(self) -> None:
        self.started: list = []
        self.stopped = 0

    def start_meeting_output(self, settings, *, request_id, deadline_ms):
        self.started.append(settings)
        future: Future = Future()
        future.set_result(SimpleNamespace(applied=True, error_message=""))
        return future

    def stop_meeting_output(self, *, request_id, deadline_ms):
        self.stopped += 1
        future: Future = Future()
        future.set_result(SimpleNamespace(applied=True))
        return future


class FakeRuntime(QObject):
    engine_event = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.scene_engine = FakeEngine()
        self.required: list[bool] = []

    def set_meeting_output_required(self, required: bool) -> None:
        self.required.append(required)


def pump_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def store(tmp_path):
    settings = SettingsStore.for_namespace("SolinTests", f"hosted-meeting-{secrets.token_hex(4)}")
    store = HostedMeetingSettingsStore(settings)
    store.set_hall_name("Salão do Reino Central")
    store.set_quality(MeetingQuality.HD)
    store.set_web_port(free_tcp_port())
    store.set_hall_feed("none")
    yield store
    for key in settings.all_keys():
        settings.set_value(key, None)


def test_meeting_lifecycle(store, tmp_path):
    runtime = FakeRuntime()
    service = HostedMeetingService(store, tmp_path, scene_runtime=runtime)
    infos: list[dict] = []
    service.info_changed.connect(infos.append)
    assert service.start_meeting()
    assert pump_until(lambda: service.state is MeetingState.LIVE and service.info["output"] == "starting")
    info = service.info
    assert info["slug"].startswith("salao-do-reino-central-")
    assert info["invitationUrl"].endswith(f"/j/{info['slug']}")
    assert "#" in info["stageUrl"]
    assert info["galene"] == "running"

    # The program output was requested from the scene engine with a 720p, present-only token.
    settings = runtime.scene_engine.started[0]
    assert (settings.width, settings.height, settings.audio_only) == (1280, 720, False)
    assert settings.whip_url.endswith(f"/group/{info['slug']}/.whip")
    assert decode_claims(settings.token)["permissions"] == ["present", "message"]
    assert runtime.required == [True]

    # The commenter-audio player joined Galene.
    assert pump_until(lambda: service.info["commenterAudio"] == "running")

    # An attendee joins through the web server; the operator admits through the service.
    port = store.web_port()
    base = f"http://127.0.0.1:{port}"
    device, secret = secrets.token_hex(16), secrets.token_hex(24)

    async def join() -> str:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/api/{info['slug']}/join",
                                    json={"device": device, "secret": secret, "name": "Ana"}) as response:
                return (await response.json())["status"]

    assert asyncio.run(join()) == "lobby"
    assert pump_until(lambda: [a["device"] for a in service.snapshot.get("lobby", [])] == [device])
    service.admit(device)
    assert pump_until(lambda: [a["device"] for a in service.snapshot.get("attendees", [])] == [device])
    assert service.snapshot["operatorConnected"] is True

    service.end_meeting()
    assert pump_until(lambda: service.state is MeetingState.IDLE)
    assert runtime.scene_engine.stopped == 1
    assert runtime.required == [True, False]
    assert service.info["galene"] == "stopped"


def test_start_fails_cleanly_without_galene(store, tmp_path, monkeypatch):
    monkeypatch.setenv("SOLIN_GALENE", str(tmp_path / "missing-galene"))
    import solin.core.hosted_meeting.galene_server as galene_server

    monkeypatch.setattr(galene_server, "find_helper", lambda helper: None)
    service = HostedMeetingService(store, tmp_path, scene_runtime=FakeRuntime())
    service.start_meeting()
    assert pump_until(lambda: service.state is MeetingState.FAILED)
    assert "not installed" in service.info["error"]


def test_link_stays_the_same_across_meetings(store, tmp_path):
    service = HostedMeetingService(store, tmp_path, scene_runtime=FakeRuntime())
    before = service.info
    group_keys = set()
    for _ in range(2):
        assert service.start_meeting()
        assert pump_until(lambda: service.state is MeetingState.LIVE)
        live = service.info
        assert (live["invitationUrl"], live["stageUrl"]) == (before["invitationUrl"], before["stageUrl"])
        assert service._runtime.core.stage_key == live["stageUrl"].split("#")[1]
        group_keys.add(service._group_key)
        service.end_meeting()
        assert pump_until(lambda: service.state is MeetingState.IDLE, 30)
        assert service.info["invitationUrl"] == before["invitationUrl"]
    assert len(group_keys) == 2  # only the link is permanent: each meeting has its own key
