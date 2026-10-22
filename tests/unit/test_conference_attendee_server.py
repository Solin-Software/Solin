from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from solin.core.conference.attendee_access import (
    AttendeeAccess,
    JoinRefusedError,
    StageHand,
    attendee_token,
    clean_name,
)
from solin.core.conference.attendee_server import AttendeeHttpApplication
from solin.core.conference.web_client import CLIENT_FILES

ACCESS = AttendeeAccess(meeting_id="m-1", hall_name="Central Hall", room="solin-meeting",
                        api_key="SOLk", api_secret="s" * 43, stage_secret="stage-secret-123")
DEVICE = "device-abc123"


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


@pytest.fixture
def dirs(tmp_path):
    pages, client = tmp_path / "pages", tmp_path / "client"
    pages.mkdir()
    client.mkdir()
    (pages / "join.html").write_text("<p>join</p>")
    (pages / "stage.html").write_text("<p>stage</p>")
    (pages / "join.js").write_text("// join")
    (pages / ".secret.js").write_text("// hidden")
    for name in CLIENT_FILES:
        (client / name).write_text("// lk")
    return pages, client


def _run(dirs, scenario, upstream="http://127.0.0.1:9"):
    pages, client = dirs
    application = AttendeeHttpApplication(pages_directory=pages, client_directory=client,
                                          livekit_http_url=upstream)

    async def main():
        async with TestClient(TestServer(application.app)) as http:
            await scenario(application, http)

    asyncio.run(main())


def test_tokens_are_lobby_only_and_refuse_removed_devices():
    claims = _claims(attendee_token(ACCESS, DEVICE, "  Ann\n  B. "))
    assert claims["sub"] == "att:" + DEVICE and claims["name"] == "Ann B."
    assert claims["video"]["hidden"] and not claims["video"]["canSubscribe"]
    assert not claims["video"]["canPublish"] and claims["exp"] - claims["nbf"] == 900
    removed = replace(ACCESS, removed=frozenset({"att:" + DEVICE}))
    with pytest.raises(JoinRefusedError, match="removed"):
        attendee_token(removed, DEVICE)
    for bad in ("short", "x" * 65, "has space!", None, 7):
        with pytest.raises(JoinRefusedError, match="invalid_device"):
            attendee_token(ACCESS, bad)
    assert clean_name("x" * 100) == "x" * 64 and clean_name(None) == ""


def test_pages_assets_and_vendor_files(dirs):
    async def scenario(application, http):
        assert (await http.get("/", allow_redirects=False)).headers["Location"] == "/join"
        page = await http.get("/join")
        assert await page.text() == "<p>join</p>"
        assert "default-src 'self'" in page.headers["Content-Security-Policy"]
        assert page.headers["Referrer-Policy"] == "no-referrer"
        assert page.headers["Cache-Control"] == "no-store"
        assert (await http.get("/assets/join.js")).status == 200
        assert (await http.get("/assets/.secret.js")).status == 404
        assert (await http.get("/assets/..%2Fjoin.html")).status == 404
        vendor = await http.get("/vendor/livekit-client.umd.js")
        assert vendor.status == 200 and vendor.headers["Content-Type"].startswith("text/javascript")
        assert (await http.get("/vendor/other.js")).status == 404

    _run(dirs, scenario)


def test_join_api_follows_the_published_access(dirs):
    async def scenario(application, http):
        assert await (await http.get("/api/meeting")).json() == {"open": False}
        closed = await http.post("/api/join", json={"deviceId": DEVICE, "name": "Ann"})
        assert closed.status == 409
        application.publish_access(ACCESS)
        meeting = await http.get("/api/meeting")
        assert await meeting.json() == {"open": True, "meetingId": "m-1", "hallName": "Central Hall",
                                        "e2ee": False}
        assert meeting.headers["Cache-Control"] == "no-store"
        joined = await http.post("/api/join", json={"deviceId": DEVICE, "name": "Ann"})
        body = await joined.json()
        assert joined.status == 200 and body["room"] == "solin-meeting"
        assert _claims(body["token"])["sub"] == "att:" + DEVICE
        assert (await http.post("/api/join", json={"deviceId": "bad"})).status == 400
        assert (await http.post("/api/join", data="deviceId=x")).status == 415
        assert (await http.post("/api/join", json=["list"])).status == 400
        big = await http.post("/api/join", data=b"{" + b" " * 3000 + b"}",
                              headers={"Content-Type": "application/json"})
        assert big.status == 413

    _run(dirs, scenario)


def test_join_attempts_are_rate_limited(dirs):
    async def scenario(application, http):
        application.publish_access(ACCESS)
        statuses = [(await http.post("/api/join", json={"deviceId": DEVICE})).status
                    for _ in range(21)]
        assert statuses[:20] == [200] * 20 and statuses[20] == 429

    _run(dirs, scenario)


def test_stage_needs_the_meeting_secret(dirs):
    async def scenario(application, http):
        assert (await http.get("/stage/stage-secret-123")).status == 404  # no meeting
        application.publish_access(ACCESS)
        application.publish_hands([StageHand("Ben", 1, True), StageHand("Cy", 2, False)], 9)
        assert (await http.get("/stage/wrong")).status == 404
        assert (await http.get("/api/stage/wrong/hands")).status == 404
        assert await (await http.get("/stage/stage-secret-123")).text() == "<p>stage</p>"
        hands = await (await http.get("/api/stage/stage-secret-123/hands")).json()
        assert hands == {"open": True, "attendance": 9, "hands": [
            {"name": "Ben", "position": 1, "speaking": True},
            {"name": "Cy", "position": 2, "speaking": False},
        ]}

    _run(dirs, scenario)


def test_rtc_requests_and_websockets_are_proxied(dirs):
    async def upstream_main():
        seen = {}

        async def validate(request):
            seen["validate"] = (request.path_qs, request.headers.get("Authorization"))
            return web.Response(text="ok", headers={"X-Upstream": "1"})

        async def rtc(request):
            seen["ws_query"] = request.query_string
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for message in socket:
                if message.type == WSMsgType.BINARY:
                    await socket.send_bytes(b"echo:" + message.data)
                elif message.type == WSMsgType.TEXT:
                    await socket.send_str("echo:" + message.data)
            return socket

        upstream = web.Application()
        upstream.router.add_get("/rtc/validate", validate)
        upstream.router.add_get("/rtc", rtc)
        server = TestServer(upstream)
        await server.start_server()
        return server, seen

    pages, client = dirs

    async def main():
        upstream, seen = await upstream_main()
        application = AttendeeHttpApplication(
            pages_directory=pages, client_directory=client,
            livekit_http_url=str(upstream.make_url("/")).rstrip("/"))
        async with TestClient(TestServer(application.app)) as http:
            reply = await http.get("/rtc/validate?access_token=t", headers={"Authorization": "Bearer t"})
            assert await reply.text() == "ok" and reply.headers["X-Upstream"] == "1"
            assert seen["validate"] == ("/rtc/validate?access_token=t", "Bearer t")
            socket = await http.ws_connect("/rtc?access_token=t&auto_subscribe=1")
            await socket.send_bytes(b"\x01\x02")
            assert (await socket.receive()).data == b"echo:\x01\x02"
            await socket.send_str("hi")
            assert (await socket.receive()).data == "echo:hi"
            await socket.close()
            assert seen["ws_query"] == "access_token=t&auto_subscribe=1"
        await upstream.close()

    asyncio.run(main())


def test_unreachable_signaling_is_a_bad_gateway(dirs):
    async def scenario(application, http):
        assert (await http.get("/rtc/validate")).status == 502

    _run(dirs, scenario)
