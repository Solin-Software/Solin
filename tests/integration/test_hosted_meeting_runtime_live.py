"""The meeting runtime against a real Galene: password, lobby, tokens, moderation, proxy,
stage and moderator pages, all under the meeting's short code."""

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

SLUG = "k7m4p2"
PASSWORD = "482913"


@pytest.fixture
def meeting(tmp_path):
    server = GaleneServer(tmp_path / "galene")
    server.start()
    key = new_group_key()
    server.layout.write_group(SLUG, key)
    snapshots: list[dict] = []
    devices: list[list[dict]] = []
    runtime = MeetingRuntime(
        MeetingRuntimeConfig(slug=SLUG, title="Test Hall", group_key=key, galene_port=server.port,
                             web_port=free_tcp_port(), bind_host="127.0.0.1", password=PASSWORD,
                             starts_at="2026-11-18T19:30:00-03:00"),
        on_snapshot=snapshots.append,
        on_devices=devices.append,
    )
    runtime.start()
    runtime.snapshots = snapshots  # type: ignore[attr-defined]
    runtime.devices = devices  # type: ignore[attr-defined]
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
            async with session.get(f"{base}/{SLUG.upper()}") as page:
                results["page"] = page.status
                results["csp"] = page.headers.get("Content-Security-Policy", "")
                results["base"] = f'data-base="/{SLUG.upper()}"' in await page.text()
            async with session.get(f"{base}/{SLUG}/api/info") as info:
                results["info"] = await info.json()
            status, body = await _post(base, f"/{SLUG}/api/join", {"device": device, "secret": secret,
                                                                  "name": "Ana Souza", "password": "wrong", "count": 3})
            results["wrong_password"] = (status, body.get("reason"))
            status, body = await _post(base, f"/{SLUG}/api/join", {"device": device, "secret": secret,
                                                                  "name": "Ana Souza", "password": PASSWORD, "count": 3})
            results["join"] = body["status"]
            events = await session.ws_connect(f"{base}/{SLUG}/api/events")
            await events.send_str(json.dumps({"type": "auth", "device": device, "secret": secret}))
            assert json.loads((await events.receive()).data)["type"] == "hello"

            # The operator admits from Qt (another thread).
            await asyncio.get_running_loop().run_in_executor(None, lambda: meeting.command("admit", device).result(5))
            admitted = json.loads((await asyncio.wait_for(events.receive(), 5)).data)
            results["admitted"] = admitted["type"]

            # Join Galene through the hall's proxy, as the browser does.
            attendee = GaleneClient(f"ws://127.0.0.1:{meeting.config.web_port}/{SLUG}/ws", SLUG, admitted["token"])
            await attendee.connect()
            results["perms"] = attendee.permissions
            await attendee.user_action("setdata", attendee.id, {"device": device, "raisehand": True})
            await asyncio.sleep(0.5)
            results["hands"] = [h["name"] for h in meeting.core.snapshot()["hands"]]

            results["totals"] = (meeting.core.totals()["people"], meeting.core.totals()["devices"])

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
            status, body = await _post(base, f"/{SLUG}/api/token", {"device": device, "secret": secret})
            results["token"] = body["status"]

            meeting.command("remove", device).result(5)
            removed = json.loads((await asyncio.wait_for(events.receive(), 5)).data)
            results["removed"] = removed["type"]
            await asyncio.sleep(0.5)
            results["kicked"] = not attendee.connected
            status, body = await _post(base, f"/{SLUG}/api/token", {"device": device, "secret": secret})
            results["token_after_removal"] = (status, body["status"])
            await attendee.close()
            await events.close()
        return results

    results = asyncio.run(scenario())
    assert results["page"] == 200 and "default-src 'self'" in results["csp"] and results["base"]
    assert results["info"]["valid"] is True and results["info"]["startsAt"].startswith("2026-11-18T19:30")
    assert results["wrong_password"] == (403, "password")
    assert results["join"] == "lobby"
    assert results["admitted"] == "admitted"
    assert results["perms"] == ()  # observe: no self-unmute
    assert results["hands"] == ["Ana Souza"]
    assert results["totals"] == (3, 1)
    assert results["called"] == "called_on"
    assert "present" in results["perms_live"]
    assert results["done"] == "done"
    assert "present" not in results["perms_done"]
    assert results["token"] == "admitted"
    assert results["removed"] == "removed"
    assert results["kicked"]
    assert results["token_after_removal"] == (403, "refused")
    assert meeting.snapshots


async def _staff(session, base, hello):
    ws = await session.ws_connect(f"{base}/{SLUG}/api/staff")
    await ws.send_str(json.dumps({"type": "hello", **hello}))
    return ws


async def _next(ws, kind=None):
    while True:
        message = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
        if kind is None or message["type"] == kind:
            return message


def test_stage_and_moderator_pages(meeting):
    """Name + password → the operator approves → views; a moderator admits from the browser."""

    base = f"http://127.0.0.1:{meeting.config.web_port}"
    stage_device, stage_secret = secrets.token_hex(16), secrets.token_hex(24)
    mod_device, mod_secret = secrets.token_hex(16), secrets.token_hex(24)
    attendee, attendee_secret = secrets.token_hex(16), secrets.token_hex(24)

    async def scenario() -> dict:
        results: dict = {}
        async with aiohttp.ClientSession() as session:
            for path in ("s", "m"):
                async with session.get(f"{base}/{SLUG}/{path}") as page:
                    results[f"page_{path}"] = page.status
            stage = await _staff(session, base, {"role": "stage", "device": stage_device, "secret": stage_secret})
            results["stage_hello"] = (await _next(stage))["type"]
            await stage.send_str(json.dumps({"type": "login", "name": "Tribuna", "password": "nope",
                                             "keyKind": "secret", "key": stage_secret}))
            results["stage_bad"] = (await _next(stage)).get("reason")
            await stage.send_str(json.dumps({"type": "login", "name": "Tribuna", "password": PASSWORD,
                                             "keyKind": "secret", "key": stage_secret}))
            results["stage_pending"] = (await _next(stage))["type"]
            await asyncio.sleep(0.2)
            results["requests"] = [r["role"] for r in meeting.core.snapshot()["staffRequests"]]
            meeting.command("approve_staff", stage_device).result(5)
            results["stage_ready"] = (await _next(stage))["type"]
            results["stage_view"] = sorted((await _next(stage, "view")).keys())

            mod = await _staff(session, base, {"role": "moderator", "device": mod_device, "secret": mod_secret})
            await _next(mod, "login")
            await mod.send_str(json.dumps({"type": "login", "name": "Irmão Lima", "password": PASSWORD,
                                           "keyKind": "secret", "key": mod_secret}))
            await _next(mod, "pending")
            meeting.command("approve_staff", mod_device).result(5)
            await _next(mod, "ready")
            await _next(mod, "view")

            # An attendee waits; the remote moderator admits them.
            await _post(base, f"/{SLUG}/api/join", {"device": attendee, "secret": attendee_secret, "name": "Bia",
                                                     "password": PASSWORD, "count": 0})
            view = await _next(mod, "view")
            while not view["lobby"]:
                view = await _next(mod, "view")
            results["mod_lobby"] = [(r["name"], r["count"]) for r in view["lobby"]]
            await mod.send_str(json.dumps({"type": "command", "name": "admit", "args": [attendee]}))
            await mod.send_str(json.dumps({"type": "command", "name": "end", "args": []}))  # not allowed
            await asyncio.sleep(0.5)
            results["admitted"] = meeting.core.attendees[attendee].state.value
            results["ended"] = meeting.core.ended
            results["staff"] = sorted(s["role"] for s in meeting.core.snapshot()["staff"])
            results["persisted"] = sorted(d["role"] for d in meeting.devices[-1])

            # Remembered: a second visit skips the password and approval.
            await mod.close()
            again = await _staff(session, base, {"role": "moderator", "device": mod_device, "secret": mod_secret})
            results["again"] = (await _next(again))["type"]

            # Removed by the operator: the page is told and closed.
            meeting.command("remove_device", stage_device).result(5)
            results["stage_removed"] = (await _next(stage, "removed"))["type"]
            await again.close()
            await stage.close()
        return results

    results = asyncio.run(scenario())
    assert results["page_s"] == 200 and results["page_m"] == 200
    assert results["stage_hello"] == "login" and results["stage_bad"] == "password"
    assert results["stage_pending"] == "pending" and results["requests"] == ["stage"]
    assert results["stage_ready"] == "ready"
    assert {"hands", "live", "participants", "staff"} <= set(results["stage_view"])
    assert results["mod_lobby"] == [("Bia", 0)]
    assert results["admitted"] == "admitted" and results["ended"] is False
    assert results["staff"] == ["moderator", "stage"]
    assert results["persisted"] == ["moderator", "stage"]
    assert results["again"] == "ready"
    assert results["stage_removed"] == "removed"


def test_old_codes_say_the_link_is_no_longer_valid(meeting):
    base = f"http://127.0.0.1:{meeting.config.web_port}"

    async def scenario() -> tuple:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/b2c3d4") as page:
                page_status = page.status
            async with session.get(f"{base}/b2c3d4/api/info") as info:
                body = await info.json()
            async with session.post(f"{base}/b2c3d4/api/join", json={}) as join:
                join_status = join.status
            async with session.get(f"{base}/not-a-code") as bad:
                bad_status = bad.status
        return page_status, body, join_status, bad_status

    assert asyncio.run(scenario()) == (200, {"valid": False}, 404, 404)


def test_wrong_passwords_are_rate_limited(meeting):
    base = f"http://127.0.0.1:{meeting.config.web_port}"

    async def scenario() -> list[int]:
        statuses = []
        for _ in range(12):
            status, _body = await _post(base, f"/{SLUG}/api/join", {
                "device": secrets.token_hex(16), "secret": secrets.token_hex(24), "name": "X",
                "password": "000000", "count": 1})
            statuses.append(status)
        return statuses

    statuses = asyncio.run(scenario())
    assert statuses[:10] == [403] * 10 and statuses[10:] == [429, 429]


def test_the_client_address_behind_the_hub_is_the_forwarded_one():
    class Request:
        def __init__(self, remote, forwarded=""):
            self.remote = remote
            self.headers = {"X-Forwarded-For": forwarded} if forwarded else {}

    assert MeetingRuntime.client_address(Request("127.0.0.1", "203.0.113.9, 10.0.0.1")) == "203.0.113.9"
    assert MeetingRuntime.client_address(Request("192.168.1.20", "203.0.113.9")) == "192.168.1.20"  # not spoofable
    assert MeetingRuntime.client_address(Request("192.168.1.20")) == "192.168.1.20"


def test_events_require_a_known_device(meeting):
    base = f"http://127.0.0.1:{meeting.config.web_port}"

    async def scenario() -> int | None:
        async with aiohttp.ClientSession() as session:
            events = await session.ws_connect(f"{base}/{SLUG}/api/events")
            await events.send_str(json.dumps({"type": "auth", "device": "nope", "secret": "x" * 32}))
            await events.receive()
            return events.close_code

    assert asyncio.run(scenario()) == 4401


def test_assets_are_served_and_traversal_refused(meeting):
    base = f"http://127.0.0.1:{meeting.config.web_port}"

    async def scenario() -> tuple[int, int, int]:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/{SLUG}/assets/app.js") as ok:
                a = ok.status
            async with session.get(f"{base}/{SLUG}/assets/..%2Fapp.js") as bad:
                b = bad.status
            async with session.get(f"{base}/{SLUG}/assets/index.html") as html:
                c = html.status
        return a, b, c

    assert asyncio.run(scenario()) == (200, 404, 404)
