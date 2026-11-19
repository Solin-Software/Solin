"""The meeting server as Solin sees it: the API client, the server setting, owned codes, tunnel and TURN."""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from cryptography import x509

from solin.core.foundation.settings_store import SettingsStore
from solin.core.hosted_meeting.galene_config import GaleneLayout, TurnServer
from solin.core.hosted_meeting.hub_client import CodeState, HubClient, HubError, TunnelInfo, owner_of
from solin.core.hosted_meeting.links import LinksState
from solin.core.hosted_meeting.settings import (
    DEFAULT_SERVER,
    SERVER_OVERRIDE,
    HostedMeetingSettingsStore,
    HubServer,
    clean_server,
)
from solin.core.hosted_meeting.tunnel import CERT_OVERRIDE, OFFICIAL_TUNNEL_CERT, frpc_config, tunnel_certificate
from solin.core.meetings.schedule import MIDWEEK, WEEKEND, MeetingSchedule, MeetingSlot
from tests._http import ThreadingLoopbackHTTPServer

# Captured at import, before the autouse fixture makes every HubClient offline.
REAL_CALL = HubClient._call

TZ = timezone(timedelta(hours=-3))
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)
SCHEDULE = MeetingSchedule(midweek=MeetingSlot(MIDWEEK, 2, 19 * 60 + 30), weekend=MeetingSlot(WEEKEND, 6, 9 * 60))


class _Server(BaseHTTPRequestHandler):
    """A stand-in for the meeting server's API: answers are set per path."""

    answers: dict[str, tuple[int, dict]] = {}
    received: list = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        type(self).received.append((self.command, self.path, body))
        status, data = type(self).answers.get(self.path, (404, {"error": "not found"}))
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = _answer

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def client(monkeypatch):
    _Server.answers, _Server.received = {}, []
    server = ThreadingLoopbackHTTPServer(_Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setattr(HubClient, "_call", REAL_CALL)
    # The real server is HTTPS; point the client at this plain-HTTP stand-in.
    monkeypatch.setattr(HubServer, "public_origin", property(lambda self: f"http://127.0.0.1:{port}"))
    yield HubClient(HubServer("meet.example.test"), timeout=3)
    server.shutdown()
    server.server_close()


def test_create_code_sends_only_the_secrets_hash(client):
    _Server.answers["/api/codes"] = (201, {"code": "k7m4p2"})
    assert client.create_code("my-secret", "2026-11-18T19:30:00-03:00", title="Central", kind="midweek") == "k7m4p2"
    method, path, body = _Server.received[0]
    assert (method, path) == ("POST", "/api/codes")
    assert body == {"owner": owner_of("my-secret"), "startsAt": "2026-11-18T19:30:00-03:00",
                    "title": "Central", "kind": "midweek"}
    assert "my-secret" not in json.dumps(body)


def test_sync_codes_and_rejections(client):
    _Server.answers["/api/codes/sync"] = (200, {"rejected": ["b2c3d4"]})
    states = [CodeState("k7m4p2", "s1", "2026-11-18T19:30:00-03:00", True, title="Central", kind="weekend"),
              CodeState("b2c3d4", "s2", "", False)]
    assert client.sync_codes(states) == {"b2c3d4"}
    assert _Server.received[0][2] == {"codes": [
        {"code": "k7m4p2", "secret": "s1", "startsAt": "2026-11-18T19:30:00-03:00", "active": True,
         "title": "Central", "kind": "weekend"},
        {"code": "b2c3d4", "secret": "s2", "startsAt": "", "active": False, "title": "", "kind": ""}]}


def test_tunnel_and_turn(client):
    _Server.answers["/api/hub"] = (200, {"frpPort": 7000, "frpToken": "tok"})
    _Server.answers["/api/turn"] = (200, {"urls": ["turn:meet.example.test:3478?transport=udp"],
                                          "username": "1800000000:k7m4p2", "credential": "c", "ttl": 43200})
    assert client.tunnel_info() == TunnelInfo(7000, "tok")
    turn = client.turn_credentials("k7m4p2", "s1")
    assert turn is not None and turn.username == "1800000000:k7m4p2"
    assert _Server.received[-1][2] == {"code": "k7m4p2", "secret": "s1"}
    _Server.answers["/api/turn"] = (404, {"error": "this hub has no TURN relay"})
    assert client.turn_credentials("k7m4p2", "s1") is None


def test_errors_are_readable(client, monkeypatch):
    _Server.answers["/api/codes"] = (429, {"error": "too many new codes from this address; try again later"})
    with pytest.raises(HubError, match="429: too many new codes"):
        client.create_code("s")
    monkeypatch.setattr(HubServer, "public_origin", property(lambda self: "http://127.0.0.1:9"))
    with pytest.raises(HubError, match="cannot be reached"):
        client.create_code("s")


@pytest.fixture
def store():
    settings = SettingsStore.for_namespace("SolinTests", f"hosted-meeting-server-{secrets.token_hex(4)}")
    yield HostedMeetingSettingsStore(settings)
    for key in settings.all_keys():
        settings.set_value(key, None)


def test_the_server_is_fixed_but_development_can_override_it(store, monkeypatch):
    monkeypatch.delenv(SERVER_OVERRIDE, raising=False)
    assert store.server() == DEFAULT_SERVER == "meet.solin.run"
    assert store.hub() == HubServer("meet.solin.run")
    monkeypatch.setenv(SERVER_OVERRIDE, " https://Hub.Local:8443/ ")
    assert store.server() == "hub.local:8443"
    monkeypatch.setenv(SERVER_OVERRIDE, "")
    assert store.server() == "" and store.hub() is None    # this network only
    monkeypatch.setenv(SERVER_OVERRIDE, "not a server")
    assert store.hub() is None
    assert clean_server("meet.solin.run/k7m4p2") == "meet.solin.run"


def test_issued_codes_keep_their_secret_and_passwords():
    state = LinksState().with_permanent().top_up(SCHEDULE, NOW, with_links=True)
    assert state.permanent is not None and not state.issued(state.permanent.code)
    local = state.meetings[1].code
    issued = state.issue(local, "q2q2q2", "secret-1")
    assert issued.meetings[1].code == "q2q2q2" and issued.meetings[1].password == state.meetings[1].password
    assert issued.issued("q2q2q2") and not issued.issued(local)
    assert LinksState.from_json(issued.to_json()).secrets == {"q2q2q2": "secret-1"}
    assert not issued.forget_secrets({"q2q2q2"}).issued("q2q2q2")
    # A code that is gone (and not retired) loses its secret on the next top-up.
    gone = issued.remove_meeting(issued.meetings[1].id, NOW)
    assert gone.issued("q2q2q2")       # retired: the server must hear it is no longer valid
    later = gone.top_up(SCHEDULE, NOW + timedelta(days=500), with_links=True)
    assert not later.issued("q2q2q2")


def test_frpc_config_names_the_tunnel_after_the_code():
    config = frpc_config(HubServer("meet.example.test:8443"), TunnelInfo(7000, 'to"k'), "k7m4p2", "sec", 8780,
                         Path("/run/hub-tunnel.crt"))
    assert 'serverAddr = "meet.example.test"' in config and "serverPort = 7000" in config
    assert "transport.tls.enable = true" in config and 'transport.tls.serverName = "meet.example.test"' in config
    assert 'transport.tls.trustedCaFile = "/run/hub-tunnel.crt"' in config
    assert 'auth.token = "to\\"k"' in config
    assert 'metadatas.code = "k7m4p2"' in config and 'metadatas.secret = "sec"' in config
    assert 'name = "k7m4p2"' in config and "localPort = 8780" in config


def test_the_official_server_certificate_is_pinned(monkeypatch, tmp_path):
    official = x509.load_pem_x509_certificate(tunnel_certificate(HubServer(DEFAULT_SERVER)).encode())
    names = official.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert names.get_values_for_type(x509.DNSName) == [DEFAULT_SERVER]
    assert official.not_valid_after_utc.year >= 2045
    # Another server (development only) is checked only against a certificate it is given.
    monkeypatch.delenv(CERT_OVERRIDE, raising=False)
    assert tunnel_certificate(HubServer("hub.local:8443")) is None
    pem = tmp_path / "hub.crt"
    pem.write_text("PEM", encoding="utf-8")
    monkeypatch.setenv(CERT_OVERRIDE, str(pem))
    assert tunnel_certificate(HubServer("hub.local:8443")) == "PEM"
    assert tunnel_certificate(HubServer(DEFAULT_SERVER)) == OFFICIAL_TUNNEL_CERT    # never overridden


def test_turn_credentials_from_the_server_are_handed_out_as_they_are(tmp_path):
    layout = GaleneLayout(tmp_path)
    layout.write_ice_servers(TurnServer(urls=("turn:h:3478?transport=udp",), secret="cred", username="1800:k7m4p2",
                                        credential_type=""))
    servers = json.loads((tmp_path / "data" / "ice-servers.json").read_text())
    assert servers[-1] == {"urls": ["turn:h:3478?transport=udp"], "username": "1800:k7m4p2", "credential": "cred"}
