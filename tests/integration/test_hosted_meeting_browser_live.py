"""The attendee page in a real browser, with a real Galene and the hall's bridge.

Two headless Chromium pages (fake camera and microphone) join through the meeting web
server: Ana, made a part-holder, turns her camera on; Bruno only watches. The hall's
``bridge receive --video-ports`` must put Ana's camera in video slot 1, while Bruno's
page gets her voice as audio only, never her camera, and keeps the main video for the
program.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time

import aiohttp
import pytest

from solin.core.hosted_meeting import participant_video
from solin.core.hosted_meeting.binaries import Helper, find_helper
from solin.core.hosted_meeting.galene_server import GaleneServer
from solin.core.hosted_meeting.processes import free_tcp_port
from solin.core.hosted_meeting.runtime import MeetingRuntime, MeetingRuntimeConfig
from solin.core.hosted_meeting.tokens import OBSERVE, new_group_key, sign_token

CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(find_helper(Helper.GALENE) is None or find_helper(Helper.BRIDGE) is None,
                       reason="Galene and solin-galene-bridge binaries not available"),
    pytest.mark.skipif(CHROMIUM is None, reason="Chromium not available"),
    # `receive` opens a playback device, which hosted CI runners lack.
    pytest.mark.skipif(bool(os.environ.get("CI")) and not os.environ.get("SOLIN_TEST_NATIVE_SIDECAR"),
                       reason="needs an audio device"),
]

SLUG = "b7rw5r"
PASSWORD = "246810"


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


class Page:
    """One Chromium tab driven over the DevTools protocol."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws
        self._ids = itertools.count(1)

    async def evaluate(self, expression: str):
        message_id = next(self._ids)
        await self._ws.send_str(json.dumps({"id": message_id, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True}}))
        while True:
            reply = json.loads((await self._ws.receive(timeout=15)).data)
            if reply.get("id") == message_id:
                result = reply.get("result", {})
                if "exceptionDetails" in result:
                    raise AssertionError(f"{expression}: {result['exceptionDetails']}")
                return result.get("result", {}).get("value")

    async def wait(self, expression: str, timeout: float = 15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await self.evaluate(expression)
            if value:
                return value
            await asyncio.sleep(0.2)
        raise AssertionError(f"timed out waiting for {expression}")


@pytest.fixture
def chromium():
    # Snap Chromium can only use a profile under its own common directory.
    base = Path.home() / "snap" / "chromium" / "common" if (CHROMIUM or "").startswith("/snap/") else None
    profile = (base or Path("/tmp")) / f"solin-meeting-test-{secrets.token_hex(4)}"
    profile.mkdir(parents=True)
    process = subprocess.Popen(  # noqa: S603 - test browser
        [CHROMIUM, "--headless=new", f"--user-data-dir={profile}", "--remote-debugging-port=0",
         "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
         "--autoplay-policy=no-user-gesture-required", "--no-first-run", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    port: list[int] = []

    def scan() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            match = re.search(r"DevTools listening on ws://[^:]+:(\d+)/", line)
            if match and not port:
                port.append(int(match.group(1)))

    threading.Thread(target=scan, daemon=True).start()
    assert wait_for(lambda: port, 20), "Chromium did not start its DevTools endpoint"
    yield port[0]
    # Snap Chromium runs confined and cannot be signalled from here: ask it to quit.
    asyncio.run(_close_browser(port[0]))
    try:
        process.wait(10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except PermissionError:
            pass
    shutil.rmtree(profile, ignore_errors=True)


async def _close_browser(port: int) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/json/version") as response:
                endpoint = (await response.json(content_type=None))["webSocketDebuggerUrl"]
            ws = await session.ws_connect(endpoint)
            await ws.send_str(json.dumps({"id": 1, "method": "Browser.close"}))
            await ws.close()
    except (aiohttp.ClientError, KeyError, OSError):
        pass


@pytest.fixture
def meeting(tmp_path):
    server = GaleneServer(tmp_path / "galene")
    server.start()
    key = new_group_key()
    server.layout.write_group(SLUG, key)
    snapshots: list[dict] = []
    runtime = MeetingRuntime(
        MeetingRuntimeConfig(slug=SLUG, title="Browser Hall", group_key=key, galene_port=server.port,
                             web_port=free_tcp_port(), bind_host="127.0.0.1", password=PASSWORD),
        on_snapshot=snapshots.append,
    )
    runtime.start()
    assert wait_for(lambda: runtime.operator_connected), "the operator client never joined Galene"
    runtime.snapshots = snapshots  # type: ignore[attr-defined]
    runtime.group_key = key  # type: ignore[attr-defined]
    runtime.galene_port = server.port  # type: ignore[attr-defined]
    yield runtime
    runtime.stop()
    server.stop()


@pytest.fixture
def slot_listener():
    """Stands in for libobs on video slot 1: a UDP socket on a free even port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for _ in range(50):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1] & ~1
        probe.close()
        try:
            sock.bind(("127.0.0.1", port))
            break
        except OSError:
            continue
    sock.settimeout(0.5)
    yield sock
    sock.close()


def test_part_holder_camera_reaches_the_hall_and_not_the_attendees(meeting, chromium, slot_listener):
    slot_port = slot_listener.getsockname()[1]
    audience = f"https://solin.invalid/group/{SLUG}/"
    receiver = subprocess.Popen(  # noqa: S603 - test helper
        [str(find_helper(Helper.BRIDGE)), "receive", "--ws", f"ws://127.0.0.1:{meeting.galene_port}/ws",
         "--group", SLUG, "--token", sign_token(meeting.group_key, username="Solin hall", audience=audience,
                                                permissions=OBSERVE, ttl_seconds=600),
         "--ignore-user", "Solin program", "--video-ports", f"{slot_port},{slot_port + 2}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    bridge_events: list[dict] = []

    def pump() -> None:
        assert receiver.stdout is not None
        for line in receiver.stdout:
            if line.startswith("{"):
                bridge_events.append(json.loads(line))

    threading.Thread(target=pump, daemon=True).start()
    base = f"http://127.0.0.1:{meeting.config.web_port}"

    def lobby_device(name: str) -> str | None:
        snapshot = meeting.snapshots[-1] if meeting.snapshots else {}
        return next((a["device"] for a in snapshot.get("lobby", []) if a["name"] == name), None)

    async def scenario() -> dict:
        report: dict = {}
        async with aiohttp.ClientSession() as session:
            async def open_page() -> tuple[Page, aiohttp.ClientWebSocketResponse]:
                async with session.put(f"http://127.0.0.1:{chromium}/json/new?{base}/{SLUG}") as response:
                    target = await response.json(content_type=None)
                ws = await session.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0)
                return Page(ws), ws

            async def join(page: Page, name: str) -> str:
                await page.wait("document.readyState === 'complete' && (document.getElementById('view-name') || {hidden: true}).hidden === false")
                await page.evaluate(f"document.getElementById('name').value = {json.dumps(name)};"
                                    f"document.getElementById('password').value = {json.dumps(PASSWORD)};"
                                    "document.querySelector('[data-count=\"2\"]').click();"
                                    "document.getElementById('name-form').requestSubmit(); true")
                loop = asyncio.get_running_loop()
                device = await loop.run_in_executor(None, lambda: wait_for(lambda: lobby_device(name), 15))
                assert device, f"{name} never reached the lobby"
                await loop.run_in_executor(None, lambda: meeting.command("admit", device).result(5))
                await page.wait("sc !== null && sc.socket && sc.socket.readyState === 1 && !!sc.id")
                return device

            ana, ana_ws = await open_page()
            bruno, bruno_ws = await open_page()
            ana_device = await join(ana, "Ana")
            await join(bruno, "Bruno")

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: meeting.command("set_part_holder", ana_device, True).result(5))
            await ana.wait("present && partHolder && (document.getElementById('part-cam') || {hidden: true}).hidden === false")
            await ana.evaluate("startMic().then(() => true)")
            await ana.evaluate("document.getElementById('part-cam').click(); true")
            await ana.wait("upCam !== null && upMic !== null")
            report["ana_camera"] = await ana.evaluate(
                "(() => { const s = camStream.getVideoTracks()[0].getSettings(); return [s.width, s.height]; })()")
            # Ana sees her own camera in a corner, and not after turning it off.
            await ana.wait("(v => !v.hidden && v.srcObject === camStream && v.videoWidth > 0)(document.getElementById('self-view'))")
            await ana.evaluate("document.getElementById('part-cam').click(); true")
            report["ana_self_view_after_off"] = await ana.evaluate(
                "(v => [v.hidden, v.srcObject])(document.getElementById('self-view'))")
            await ana.evaluate("document.getElementById('part-cam').click(); true")
            await ana.wait("upCam !== null")

            # Bruno: Ana's voice as a hidden <audio>; the main video untouched (no program here).
            await bruno.wait("voices.size === 1")
            report["bruno_audio"] = await bruno.evaluate(
                "[...voices.values()].map(a => ({hidden: a.hidden, audio: a.srcObject.getAudioTracks().length,"
                " video: a.srcObject.getVideoTracks().length}))")
            report["bruno_program_src"] = await bruno.evaluate("document.getElementById('program').srcObject === null")
            await asyncio.sleep(2.0)
            report["bruno_down_labels"] = await bruno.evaluate(
                "Object.values(sc.down).map(s => ({label: s.label, user: s.username,"
                " kinds: s.stream ? s.stream.getTracks().map(t => t.kind) : []}))")
            for ws in (ana_ws, bruno_ws):
                await ws.close()
        return report

    try:
        report = asyncio.run(scenario())
        packets = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(packets) < 20:
            try:
                packets.append(slot_listener.recv(2048))
            except TimeoutError:
                continue
    finally:
        receiver.terminate()
        receiver.wait(5)

    video_events = [e for e in bridge_events if e.get("event") == "video"]
    assert video_events and video_events[0]["slot"] == 1 and video_events[0]["user"] == "Ana", bridge_events
    assert len(packets) >= 20, f"only {len(packets)} RTP packets reached slot 1"
    assert all(p[1] & 0x7F == 96 for p in packets)  # rewritten to the slot's payload type
    assert report["ana_camera"] == [640, 360], report  # part-holder cameras go up at 360p
    assert report["ana_self_view_after_off"] == [True, None], report
    assert report["bruno_audio"] == [{"hidden": True, "audio": 1, "video": 0}], report
    assert report["bruno_program_src"] is True, report
    assert [d["label"] for d in report["bruno_down_labels"]] == ["camera"], report  # Ana's mic only
    assert participant_video.FFMPEG_OPTIONS  # the slot contract the bridge writes to


def test_first_screen_and_moderator_device_key_in_a_real_browser(meeting, chromium):
    """The attendee's first screen (wrong password, count), and a moderator whose browser
    keeps a non-extractable WebCrypto key: approved once, recognised again after a reload."""

    base = f"http://127.0.0.1:{meeting.config.web_port}"

    def snapshot() -> dict:
        return meeting.core.snapshot()

    async def scenario() -> dict:
        report: dict = {}
        async with aiohttp.ClientSession() as session:
            async def open_page(path: str) -> tuple[Page, aiohttp.ClientWebSocketResponse]:
                async with session.put(f"http://127.0.0.1:{chromium}/json/new?{base}/{path}") as response:
                    target = await response.json(content_type=None)
                ws = await session.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0)
                return Page(ws), ws

            loop = asyncio.get_running_loop()
            attendee, attendee_ws = await open_page(SLUG.upper())
            await attendee.wait("(document.getElementById('view-name') || {hidden: true}).hidden === false")
            await attendee.evaluate("document.getElementById('name').value = 'Família Souza';"
                                    "document.getElementById('password').value = '999999';"
                                    "document.getElementById('name-form').requestSubmit(); true")
            report["needs_count"] = await attendee.wait(
                "(document.getElementById('form-error') || {hidden: true}).hidden === false && document.getElementById('form-error').textContent")
            await attendee.evaluate("document.querySelector('[data-count=more]').click();"
                                    "const f = document.getElementById('count-more'); f.value = '7';"
                                    "f.dispatchEvent(new Event('input')); document.getElementById('name-form').requestSubmit(); true")
            await attendee.wait("document.getElementById('form-error').textContent === T.wrongPassword")
            await attendee.evaluate(f"document.getElementById('password').value = {json.dumps(PASSWORD)};"
                                    "document.getElementById('name-form').requestSubmit(); true")
            assert await loop.run_in_executor(None, lambda: wait_for(lambda: snapshot()["lobby"], 15))
            report["lobby"] = [(r["name"], r["count"]) for r in snapshot()["lobby"]]
            # A reload returns to the lobby without the form (the password was already checked).
            await attendee.evaluate("location.reload(); true")
            await asyncio.sleep(1.0)
            await attendee.wait("(document.getElementById('view-lobby') || {hidden: true}).hidden === false")

            moderator, moderator_ws = await open_page(f"{SLUG}/m")
            await moderator.wait("(document.getElementById('view-login') || {hidden: true}).hidden === false")
            report["secure"] = await moderator.evaluate("window.isSecureContext && !!crypto.subtle")
            await moderator.evaluate(f"document.getElementById('name').value = 'Irmão Lima';"
                                     f"document.getElementById('password').value = {json.dumps(PASSWORD)};"
                                     "document.getElementById('login-form').requestSubmit(); true")
            await moderator.wait("(document.getElementById('view-pending') || {hidden: true}).hidden === false")
            requests = await loop.run_in_executor(None, lambda: wait_for(lambda: snapshot()["staffRequests"], 10))
            assert requests
            device = snapshot()["staffRequests"][0]["device"]
            await loop.run_in_executor(None, lambda: meeting.command("approve_staff", device).result(5))
            await moderator.wait("(document.getElementById('view-console') || {hidden: true}).hidden === false"
                                 " && document.getElementById('lobby').children.length === 1")
            report["key_kind"] = meeting.core.devices.get(device, time.time()).key_kind.value
            report["mod_lobby"] = await moderator.evaluate("document.getElementById('lobby').textContent")
            # The moderator admits the family from the browser.
            await moderator.evaluate("document.querySelector('#lobby button.primary').click(); true")
            await attendee.wait("(document.getElementById('view-meeting') || {hidden: true}).hidden === false")

            # Reload: the stored key signs a fresh challenge; no login, no approval.
            await moderator.evaluate("location.reload(); true")
            await asyncio.sleep(1.0)
            await moderator.wait("(document.getElementById('view-console') || {hidden: true}).hidden === false")
            report["login_after_reload"] = await moderator.evaluate(
                "(document.getElementById('view-login') || {hidden: true}).hidden === false")
            report["totals"] = await moderator.evaluate("document.getElementById('totals').textContent")
            for ws in (attendee_ws, moderator_ws):
                await ws.close()
        return report

    report = asyncio.run(scenario())
    assert report["needs_count"], report
    assert report["lobby"] == [("Família Souza", 7)], report
    assert report["secure"] is True and report["key_kind"] == "ecdsa", report
    assert "Família Souza" in report["mod_lobby"], report
    assert report["login_after_reload"] is False, report
    assert "7" in report["totals"], report
