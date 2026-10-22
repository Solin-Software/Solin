from __future__ import annotations

import json

import pytest

from solin.core.conference.server_config import (
    EmbeddedTurn,
    ServerRole,
    ServerSettings,
    TurnServer,
    generate_credentials,
    render_config,
)


def _settings(**overrides) -> ServerSettings:
    key, secret = generate_credentials()
    return ServerSettings(api_key=key, api_secret=secret, **overrides)


def test_credentials_are_fresh_and_strong():
    pairs = {generate_credentials() for _ in range(20)}
    assert len(pairs) == 20
    for key, secret in pairs:
        assert key.startswith("SOL") and len(key) == 15
        assert len(secret) >= 32


def test_hall_config_document():
    settings = _settings(node_ip="203.0.113.7")
    document = json.loads(render_config(settings))
    assert document["port"] == 7880
    assert document["bind_addresses"] == ["0.0.0.0"]
    assert document["rtc"] == {
        "tcp_port": 7881,
        "udp_port": 7882,
        "use_external_ip": False,
        "node_ip": "203.0.113.7",
    }
    assert document["keys"] == {settings.api_key: settings.api_secret}
    assert "turn" not in document


def test_external_turn_servers_use_shared_secret_credentials():
    turn = TurnServer(host="hub.example.org", port=3478, protocol="udp", secret="s" * 40)
    document = json.loads(render_config(_settings(turn_servers=(turn,))))
    assert document["rtc"]["turn_servers"] == [
        {
            "host": "hub.example.org",
            "port": 3478,
            "protocol": "udp",
            "secret": "s" * 40,
            "ttl": 14400,
        }
    ]


def test_relay_role_enables_embedded_udp_turn():
    settings = _settings(
        role=ServerRole.RELAY,
        embedded_turn=EmbeddedTurn(udp_port=3478, relay_port_start=30000, relay_port_end=30099),
    )
    document = json.loads(render_config(settings))
    assert document["turn"] == {
        "enabled": True,
        "udp_port": 3478,
        "relay_range_start": 30000,
        "relay_range_end": 30099,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"http_port": 0},
        {"rtc_udp_port": 70000},
        {"http_port": 7881},  # collides with the RTC TCP port
        {"log_level": "verbose"},
        {"turn_servers": (TurnServer(host="", port=3478, protocol="udp", secret="x"),)},
        {"turn_servers": (TurnServer(host="h", port=3478, protocol="quic", secret="x"),)},
        {"embedded_turn": EmbeddedTurn(relay_port_start=31000, relay_port_end=30000)},
        {"embedded_turn": EmbeddedTurn(udp_port=7882)},  # collides with the RTC UDP port
    ],
)
def test_invalid_settings_are_rejected(overrides):
    with pytest.raises(ValueError):
        render_config(_settings(**overrides))


def test_short_secret_is_rejected():
    with pytest.raises(ValueError):
        render_config(ServerSettings(api_key="SOLKEY", api_secret="short"))
