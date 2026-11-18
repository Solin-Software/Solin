"""The hall's meeting codes on the hub: what Solin sends, how it reads the answer, redrawing clashes."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from solin.core.hosted_meeting.hub_codes import CodesPayload, codes_payload, register_codes
from solin.core.hosted_meeting.links import LinkMode, LinksState
from solin.core.hosted_meeting.settings import HubConnection
from solin.core.meetings.schedule import MIDWEEK, WEEKEND, MeetingSchedule, MeetingSlot

TZ = timezone(timedelta(hours=-3))
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)   # Monday
SCHEDULE = MeetingSchedule(midweek=MeetingSlot(MIDWEEK, 2, 19 * 60 + 30), weekend=MeetingSlot(WEEKEND, 6, 9 * 60))


def test_same_link_mode_sends_the_permanent_code_with_the_next_start():
    state = LinksState().top_up(SCHEDULE, NOW, with_links=False).with_permanent()
    assert state.permanent is not None
    payload = codes_payload(state, LinkMode.SAME, NOW)
    assert payload.active == {state.permanent.code: "2026-11-18T19:30:00-03:00"}
    # The next meeting cancelled: the countdown points at the one after.
    cancelled = state.set_cancelled(state.upcoming(NOW)[0].id, True)
    assert codes_payload(cancelled, LinkMode.SAME, NOW).active[state.permanent.code] == "2026-11-22T09:00:00-03:00"


def test_per_meeting_mode_sends_every_meeting_and_retires_the_rest():
    state = LinksState().with_permanent().top_up(SCHEDULE, NOW, with_links=True)
    state = state.renew(state.meetings[0].id, NOW)
    retired_code = next(iter(state.retired))
    payload = codes_payload(state, LinkMode.PER_MEETING, NOW, running="zzzzzz")
    assert set(payload.active) == {m.code for m in state.meetings} | {"zzzzzz"}
    assert payload.active[state.meetings[1].code] == state.meetings[1].starts_at.isoformat()
    assert payload.active["zzzzzz"] == ""
    assert state.permanent is not None and state.permanent.code in payload.retired
    assert retired_code in payload.retired


def test_redraw_replaces_only_the_clashing_codes():
    state = LinksState().with_permanent().top_up(SCHEDULE, NOW, with_links=True)
    assert state.permanent is not None
    clash = {state.permanent.code, state.meetings[2].code}
    redrawn = state.redraw(clash)
    assert redrawn.permanent is not None and redrawn.permanent.code not in clash
    assert redrawn.permanent.password == state.permanent.password
    assert redrawn.meetings[2].code not in clash and redrawn.meetings[2].password == state.meetings[2].password
    assert [m.code for i, m in enumerate(redrawn.meetings) if i != 2] == \
           [m.code for i, m in enumerate(state.meetings) if i != 2]
    assert not clash & set(redrawn.retired)
    assert len(redrawn.codes_in_use()) == len(state.codes_in_use())


class _Hub(BaseHTTPRequestHandler):
    answer: tuple[int, dict] = (200, {"ok": True})
    received: list = []

    def do_PUT(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append((self.path, self.headers["Authorization"], body))
        status, data = type(self).answer
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def hub_server(monkeypatch):
    _Hub.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    # The hub is HTTPS in real life; point the client at this plain-HTTP stand-in.
    monkeypatch.setattr(HubConnection, "public_origin", property(lambda self: f"http://127.0.0.1:{port}"))
    yield HubConnection(domain="meet.example.test", frp_addr="meet.example.test", frp_port=7000,
                        frp_token="t", hall="hall-x", hall_key="secret-key")
    server.shutdown()
    server.server_close()


def test_register_codes_sends_the_list_with_the_hall_key(hub_server):
    _Hub.answer = (200, {"ok": True})
    payload = CodesPayload(active={"k7m4p2": "2026-11-18T19:30:00-03:00"}, retired=("b2c3d4",))
    assert register_codes(hub_server, payload).ok
    path, auth, body = _Hub.received[0]
    assert (path, auth) == ("/api/halls/hall-x/codes", "Bearer secret-key")
    assert body == {"codes": [{"code": "k7m4p2", "startsAt": "2026-11-18T19:30:00-03:00"}], "retired": ["b2c3d4"]}


def test_register_codes_reads_the_hubs_refusals(hub_server):
    _Hub.answer = (409, {"taken": ["k7m4p2"]})
    result = register_codes(hub_server, CodesPayload(active={"k7m4p2": ""}))
    assert not result.ok and result.taken == ("k7m4p2",)
    _Hub.answer = (401, {"error": "unknown hall or wrong hall key"})
    assert "key" in register_codes(hub_server, CodesPayload()).error
    _Hub.answer = (500, {})
    assert register_codes(hub_server, CodesPayload()).error == "the hub answered 500"


def test_register_codes_when_the_hub_is_unreachable(monkeypatch):
    monkeypatch.setattr(HubConnection, "public_origin", property(lambda self: "http://127.0.0.1:9"))
    hub = HubConnection(domain="x.test", frp_addr="x.test", frp_port=7000, frp_token="t", hall="hall-x", hall_key="k")
    result = register_codes(hub, CodesPayload(), timeout=2)
    assert not result.ok and "cannot be reached" in result.error
