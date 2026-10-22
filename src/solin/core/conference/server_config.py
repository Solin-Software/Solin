"""Configuration for the bundled LiveKit server, in either of its two roles.

A hall runs the server for its own meeting; any Solin installation can also opt in as a
relay (edge node) that hosts meetings for other halls and offers TURN for attendees whose
networks need it. Both roles use the same binary and this one configuration model.

The configuration is written as JSON, which LiveKit reads as YAML.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from enum import StrEnum

_PROTOCOLS = frozenset({"udp", "tcp", "tls"})
_LOG_LEVELS = frozenset({"debug", "info", "warn", "error"})


class ServerRole(StrEnum):
    HALL = "hall"
    RELAY = "relay"


@dataclass(frozen=True, slots=True)
class TurnServer:
    """An external TURN server handed to clients, with credentials derived from `secret`."""

    host: str
    port: int
    protocol: str
    secret: str
    ttl_seconds: int = 4 * 60 * 60


@dataclass(frozen=True, slots=True)
class EmbeddedTurn:
    """LiveKit's built-in TURN server (used by relays; no separate coturn process)."""

    udp_port: int = 3478
    relay_port_start: int = 30000
    relay_port_end: int = 30999
    domain: str = ""


@dataclass(frozen=True, slots=True)
class ServerSettings:
    api_key: str
    api_secret: str
    role: ServerRole = ServerRole.HALL
    http_port: int = 7880
    rtc_tcp_port: int = 7881
    rtc_udp_port: int = 7882
    # Signaling listens on these; media sockets use every interface (IPv4 and IPv6).
    bind_addresses: tuple[str, ...] = ("0.0.0.0",)
    use_external_ip: bool = False
    node_ip: str = ""
    turn_servers: tuple[TurnServer, ...] = ()
    embedded_turn: EmbeddedTurn | None = None
    log_level: str = "info"
    extra: dict[str, object] = field(default_factory=dict)


def generate_credentials() -> tuple[str, str]:
    """A fresh API key/secret pair; Solin mints all participant tokens with it."""
    return f"SOL{secrets.token_hex(6).upper()}", secrets.token_urlsafe(32)


def _check_port(name: str, value: int) -> None:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f"{name} must be a port number, got {value!r}")


def validate(settings: ServerSettings) -> None:
    if not settings.api_key or not settings.api_secret:
        raise ValueError("API key and secret are required")
    if len(settings.api_secret) < 32:
        raise ValueError("API secret must be at least 32 characters")
    ports = {
        "http_port": settings.http_port,
        "rtc_tcp_port": settings.rtc_tcp_port,
        "rtc_udp_port": settings.rtc_udp_port,
    }
    for name, port in ports.items():
        _check_port(name, port)
    tcp_ports = [settings.http_port, settings.rtc_tcp_port]
    if len(set(tcp_ports)) != len(tcp_ports):
        raise ValueError("HTTP and RTC TCP ports must differ")
    if settings.log_level not in _LOG_LEVELS:
        raise ValueError(f"Unknown log level {settings.log_level!r}")
    for turn in settings.turn_servers:
        _check_port("turn port", turn.port)
        if not turn.host or turn.protocol not in _PROTOCOLS or not turn.secret:
            raise ValueError(f"Invalid TURN server {turn.host!r}")
    embedded = settings.embedded_turn
    if embedded is not None:
        _check_port("embedded TURN UDP port", embedded.udp_port)
        _check_port("embedded TURN relay range start", embedded.relay_port_start)
        _check_port("embedded TURN relay range end", embedded.relay_port_end)
        if embedded.relay_port_start > embedded.relay_port_end:
            raise ValueError("Embedded TURN relay range is inverted")
        if embedded.udp_port in (settings.rtc_udp_port,):
            raise ValueError("Embedded TURN must not share the RTC UDP port")


def render_config(settings: ServerSettings) -> str:
    """Render the LiveKit server configuration document."""
    validate(settings)
    rtc: dict[str, object] = {
        "tcp_port": settings.rtc_tcp_port,
        "udp_port": settings.rtc_udp_port,
        "use_external_ip": settings.use_external_ip,
    }
    if settings.node_ip:
        rtc["node_ip"] = settings.node_ip
    if settings.turn_servers:
        rtc["turn_servers"] = [
            {
                "host": turn.host,
                "port": turn.port,
                "protocol": turn.protocol,
                "secret": turn.secret,
                "ttl": turn.ttl_seconds,
            }
            for turn in settings.turn_servers
        ]
    document: dict[str, object] = {
        "port": settings.http_port,
        "bind_addresses": list(settings.bind_addresses),
        "rtc": rtc,
        "keys": {settings.api_key: settings.api_secret},
        "logging": {"level": settings.log_level, "json": False},
    }
    if settings.embedded_turn is not None:
        turn = settings.embedded_turn
        section: dict[str, object] = {
            "enabled": True,
            "udp_port": turn.udp_port,
            "relay_range_start": turn.relay_port_start,
            "relay_range_end": turn.relay_port_end,
        }
        if turn.domain:
            section["domain"] = turn.domain
        document["turn"] = section
    document.update(settings.extra)
    return json.dumps(document, indent=2, sort_keys=False) + "\n"
