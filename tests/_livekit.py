"""A real livekit-server for live conference tests (skipped when the binary is absent)."""

from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

import pytest

from solin.core.conference.server_binary import ServerUnavailableError, server_executable
from solin.core.conference.server_config import (
    ServerRole, ServerSettings, generate_credentials, render_config,
)


def _binary() -> Path | None:
    try:
        return server_executable()
    except ServerUnavailableError:
        return None


requires_livekit_server = pytest.mark.skipif(
    _binary() is None, reason="run scripts/fetch_livekit_server.py first"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def livekit_server(tmp_path):
    """Yields ``(ws_url, api_key, api_secret)`` of a fresh loopback server."""
    key, secret = generate_credentials()
    http, tcp, udp = _free_port(), _free_port(), _free_port()
    settings = ServerSettings(
        api_key=key, api_secret=secret, role=ServerRole.HALL, http_port=http,
        rtc_tcp_port=tcp, rtc_udp_port=udp, bind_addresses=("127.0.0.1",), node_ip="127.0.0.1",
    )
    config = tmp_path / "livekit.json"
    config.write_text(render_config(settings), encoding="utf-8")
    process = subprocess.Popen([str(_binary()), "--config", str(config)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", http), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("livekit-server did not start")
    yield f"ws://127.0.0.1:{http}", key, secret
    process.terminate()
    process.wait(10)
