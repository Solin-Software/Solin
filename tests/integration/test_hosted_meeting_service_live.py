"""HostedMeetingService with a real Galene and bridge, and a fake scene engine."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from concurrent.futures import Future
from types import SimpleNamespace

import aiohttp
import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from solin.core.foundation.settings_store import SettingsStore
from solin.core.hosted_meeting.binaries import Helper, find_helper
from solin.core.hosted_meeting.hub_client import TunnelInfo, TurnCredentials
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
    store.set_server("")   # this network only: the tests do not reach a meeting server
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
    assert len(info["slug"]) == 6 and len(info["password"]) == 6
    assert info["invitationUrl"].endswith(f"/{info['slug']}")
    assert info["stageUrl"].endswith(f"/{info['slug']}/s") and info["moderatorUrl"].endswith(f"/{info['slug']}/m")
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
            async with session.post(f"{base}/{info['slug']}/api/join",
                                    json={"device": device, "secret": secret, "name": "Ana",
                                          "password": info["password"], "count": 2}) as response:
                return (await response.json())["status"]

    assert asyncio.run(join()) == "lobby"
    assert pump_until(lambda: [a["device"] for a in service.snapshot.get("lobby", [])] == [device])
    service.admit(device)
    assert pump_until(lambda: [a["device"] for a in service.snapshot.get("attendees", [])] == [device])
    assert service.snapshot["operatorConnected"] is True
    assert service.snapshot["people"] == 2 and service.attendance_text() == "2"
    # Solin restarting mid-meeting would let Ana straight back in.
    assert pump_until(lambda: (tmp_path / "hosted_meeting" / f"admitted-{info['slug']}.json").exists())

    service.end_meeting()
    assert pump_until(lambda: service.state is MeetingState.IDLE)
    assert runtime.scene_engine.stopped == 1
    assert runtime.required == [True, False]
    assert service.info["galene"] == "stopped"
    assert not list((tmp_path / "hosted_meeting").glob("admitted-*.json"))  # ended: nobody is let back


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
        assert (live["invitationUrl"], live["stageUrl"], live["password"]) == (
            before["invitationUrl"], before["stageUrl"], before["password"])
        assert service._runtime.core.password == live["password"]
        group_keys.add(service._group_key)
        service.end_meeting()
        assert pump_until(lambda: service.state is MeetingState.IDLE, 30)
        assert service.info["invitationUrl"] == before["invitationUrl"]
    assert len(group_keys) == 2  # only the link is permanent: each meeting has its own key


class FakeMeetingServer:
    """The meeting server's API as the service uses it; the tunnel itself points nowhere."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    def __call__(self, server):
        return self

    def create_code(self, secret, starts_at=""):
        code = ("q2q2q" + "23456789bcdfghjkmnpqrstvwxyz"[len(self.created)])
        self.created.append((code, secret))
        return code

    def sync_codes(self, codes):
        return set()

    def tunnel_info(self):
        return TunnelInfo(frp_port=7000, frp_token="frp-token")

    def turn_credentials(self, code, secret):
        return TurnCredentials(urls=("turn:hub.invalid:3478?transport=udp",), username=f"1900000000:{code}",
                               credential="cred")


@pytest.mark.skipif(find_helper(Helper.FRPC) is None, reason="frpc not available")
def test_a_meeting_on_the_server_uses_its_code_secret_for_tunnel_and_turn(store, tmp_path):
    server = FakeMeetingServer()
    store.set_server("hub.invalid")
    service = HostedMeetingService(store, tmp_path, scene_runtime=FakeRuntime(), hub_client=server)
    assert pump_until(lambda: service.info["linkScope"] == "hub")
    assert service.start_meeting()
    assert pump_until(lambda: service.state is MeetingState.LIVE)
    code, secret = server.created[0]
    info = service.info
    assert info["slug"] == code and info["invitationUrl"] == f"https://hub.invalid/{code}"
    frpc = (tmp_path / "hosted_meeting" / "frpc" / "frpc.toml").read_text()
    assert f'metadatas.code = "{code}"' in frpc and f'metadatas.secret = "{secret}"' in frpc
    assert f'name = "{code}"' in frpc and 'serverAddr = "hub.invalid"' in frpc
    ice = json.loads((tmp_path / "hosted_meeting" / "galene" / "data" / "ice-servers.json").read_text())
    assert ice[-1]["username"] == f"1900000000:{code}" and "credentialType" not in ice[-1]
    service.end_meeting()
    assert pump_until(lambda: service.state is MeetingState.IDLE, 30)
