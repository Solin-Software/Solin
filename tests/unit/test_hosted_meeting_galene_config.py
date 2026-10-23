from __future__ import annotations

import json

import pytest

from solin.core.hosted_meeting.galene_config import GaleneLayout, TurnServer
from solin.core.hosted_meeting.tokens import group_jwk


def test_group_is_token_only_without_recording(tmp_path):
    layout = GaleneLayout(tmp_path)
    layout.prepare()
    key = b"k" * 32
    path = layout.write_group("hall-a-0001", key, display_name="Kingdom Hall")
    group = json.loads(path.read_text())
    assert group["codecs"] == ["h264", "opus"]
    assert group["allow-recording"] is False
    assert group["authKeys"] == [group_jwk(key)]
    assert "users" not in group and "wildcard-user" not in group
    assert group["displayName"] == "Kingdom Hall"
    assert (tmp_path / "static" / "index.html").is_file()


def test_invalid_group_names_are_rejected(tmp_path):
    layout = GaleneLayout(tmp_path)
    for bad in ("../x", "A", "x", "has space"):
        with pytest.raises(ValueError):
            layout.write_group(bad, b"k" * 32)


def test_ice_servers_use_time_limited_credentials(tmp_path):
    layout = GaleneLayout(tmp_path)
    turn = TurnServer(urls=("turn:hub.example:3478?transport=udp", "turn:hub.example:3478?transport=tcp"),
                      secret="s3cret")
    layout.write_ice_servers(turn)
    servers = json.loads((tmp_path / "data" / "ice-servers.json").read_text())
    assert servers[0] == {"urls": ["stun:hub.example:3478"]}
    assert servers[1]["credentialType"] == "hmac-sha1"
    assert servers[1]["credential"] == "s3cret"
    layout.write_ice_servers(None)
    assert not (tmp_path / "data" / "ice-servers.json").exists()


def test_server_config_lists_origins(tmp_path):
    layout = GaleneLayout(tmp_path)
    layout.write_server_config(["https://b", "https://a", ""])
    assert json.loads((tmp_path / "data" / "config.json").read_text()) == {"allowOrigin": ["https://a", "https://b"]}
    layout.write_server_config([])
    assert json.loads((tmp_path / "data" / "config.json").read_text()) == {}
