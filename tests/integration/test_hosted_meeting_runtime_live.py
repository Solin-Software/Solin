"""The meeting runtime against a real Galene: lobby, tokens, moderation, proxy, stage."""

from __future__ import annotations

import asyncio
import json
import secrets
import time

import aiohttp
import pytest

from solin.core.hosted_meeting.binaries import Helper, find_helper
from solin.core.hosted_meeting.galene_client import GaleneClient
from solin.core.hosted_meeting.galene_server import GaleneServer
from solin.core.hosted_meeting.processes import free_tcp_port
from solin.core.hosted_meeting.runtime import MeetingRuntime, MeetingRuntimeConfig
from solin.core.hosted_meeting.tokens import new_group_key

pytestmark = pytest.mark.skipif(find_helper(Helper.GALENE) is None, reason="Galene binary not available")

SLUG = "test-hall-0001"


@pytest.fixture
def meeting(tmp_path):
    server = GaleneServer(tmp_path / "galene")
    server.start()
    key = new_group_key()
    server.layout.write_group(SLUG, key)
    snapshots: list[dict] = []
    runtime = MeetingRuntime(
        MeetingRuntimeConfig(slug=SLUG, title="Test Hall", group_key=key, galene_port=server.port,
                             web_port=free_tcp_port(), bind_host="127.0.0.1"),
        on_snapshot=snapshots.append,
    )
    runtime.start()
    runtime.snapshots = snapshots  # type: ignore[attr-defined]
    deadline = time.monotonic() + 10
    while not runtime.operator_connected and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runtime.operator_connected, "the operator client never joined Galene"
    yield runtime
    runtime.stop()
    server.stop()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


async def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base}{path}", json=body) as response:
            return response.status, await response.json()


def test_attendee_journey(meeting):
    base = f"http://127.0.0.1:{meeting.config.web_port}"
    device, secret = secrets.token_hex(16), secrets.token_hex(24)

    async def scenario() -> dict:
        results: dict = {}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/j/{SLUG}") as page:
                results["page"] = page.status
                results["csp"] = page.headers.get("Content-Security-Policy", "")
            status, body = await _post(base, f"/api/{SLUG}/join", {"device": device, "secret": secret, "name": "Ana Souza"})
            results["join"] = body["status"]
            events = await session.ws_connect(f"{base}/api/{SLUG}/events")
            await events.send_str(json.dumps({"type": "auth", "device": device, "secret": secret}))
            assert json.loads((await events.receive()).data)["type"] == "hello"

            # The operator admits from Qt (another thread).
            await asyncio.get_running_loop().run_in_executor(None, lambda: meeting.command("admit", device).result(5))
            admitted = json.loads((await asyncio.wait_for(events.receive(), 5)).data)
            results["admitted"] = admitted["type"]

            # Join Galene through the hall's proxy, as the browser does.
            attendee = GaleneClient(f"ws://127.0.0.1:{meeting.config.web_port}/ws", SLUG, admitted["token"])
            await attendee.connect()
            results["perms"] = attendee.permissions
            await attendee.user_action("setdata", attendee.id, {"device": device, "raisehand": True})
            await asyncio.sleep(0.5)
            results["hands"] = [h["name"] for h in meeting.core.snapshot()["hands"]]

            stage = await session.ws_connect(f"{base}/api/{SLUG}/stage")
            await stage.send_str(json.dumps({"type": "auth", "key": meeting.core.stage_key}))
            results["stage"] = json.loads((await stage.receive()).data)["hands"]

            meeting.command("call_on", device).result(5)
            called = json.loads((await asyncio.wait_for(events.receive(), 5)).data)
            results["called"] = called["type"]
            await asyncio.sleep(0.5)
            results["perms_live"] = attendee.permissions
            await events.send_str(json.dumps({"type": "done"}))
            results["done"] = json.loads((await asyncio.wait_for(events.receive(), 5)).data)["type"]
            await asyncio.sleep(0.5)
            results["perms_done"] = attendee.permissions

            # A fresh token for reconnecting.
            status, body = await _post(base, f"/api/{SLUG}/token", {"device": device, "secret": secret})
            results["token"] = body["status"]

            meeting.command("remove", device).result(5)
            removed = json.loads((await asyncio.wait_for(events.receive(), 5)).data)
            results["removed"] = removed["type"]
            await asyncio.sleep(0.5)
            results["kicked"] = not attendee.connected
            status, body = await _post(base, f"/api/{SLUG}/token", {"device": device, "secret": secret})
            results["token_after_removal"] = (status, body["status"])
            await attendee.close()
            await events.close()
            await stage.close()
        return results

    results = asyncio.run(scenario())
    assert results["page"] == 200 and "default-src 'self'" in results["csp"]
    assert results["join"] == "lobby"
    assert results["admitted"] == "admitted"
    assert results["perms"] == ()  # observe: no self-unmute
    assert results["hands"] == ["Ana Souza"]
    assert results["stage"] == ["Ana Souza"]
    assert results["called"] == "called_on"
    assert "present" in results["perms_live"]
    assert results["done"] == "done"
    assert "present" not in results["perms_done"]
    assert results["token"] == "admitted"
    assert results["removed"] == "removed"
    assert results["kicked"]
    assert results["token_after_removal"] == (403, "refused")
    assert meeting.snapshots and meeting.snapshots[-1]["attendance"] == 1


def test_stage_requires_key(meeting):
    base = f"http://127.0.0.1:{meeting.config.web_port}"

    async def scenario() -> int | None:
        async with aiohttp.ClientSession() as session:
            stage = await session.ws_connect(f"{base}/api/{SLUG}/stage")
            await stage.send_str(json.dumps({"type": "auth", "key": "wrong"}))
            await stage.receive()
            return stage.close_code

    assert asyncio.run(scenario()) == 4401


def test_events_require_a_known_device(meeting):
    base = f"http://127.0.0.1:{meeting.config.web_port}"

    async def scenario() -> int | None:
        async with aiohttp.ClientSession() as session:
            events = await session.ws_connect(f"{base}/api/{SLUG}/events")
            await events.send_str(json.dumps({"type": "auth", "device": "nope", "secret": "x" * 32}))
            await events.receive()
            return events.close_code

    assert asyncio.run(scenario()) == 4401


def test_assets_are_served_and_traversal_refused(meeting):
    base = f"http://127.0.0.1:{meeting.config.web_port}"

    async def scenario() -> tuple[int, int, int]:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/assets/app.js") as ok:
                a = ok.status
            async with session.get(f"{base}/assets/..%2Fapp.js") as bad:
                b = bad.status
            async with session.get(f"{base}/assets/index.html") as html:
                c = html.status
        return a, b, c

    assert asyncio.run(scenario()) == (200, 404, 404)
