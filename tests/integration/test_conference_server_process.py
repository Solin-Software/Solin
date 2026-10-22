from __future__ import annotations

import socket
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from solin.core.conference.server_binary import ServerUnavailableError, server_executable
from solin.core.conference.server_config import ServerSettings, generate_credentials
from solin.core.conference.server_process import ConferenceServerService, ServerState
from tests._qt import wait_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake server uses a shebang script")

# A stand-in for livekit-server: reads the JSON config and behaves per FAKE_LIVEKIT_MODE.
FAKE_SERVER = textwrap.dedent(
    """\
    #!{python}
    import json, os, socket, sys, time
    config = json.load(open(sys.argv[sys.argv.index("--config") + 1]))
    mode = os.environ.get("FAKE_LIVEKIT_MODE", "ok")
    marker = os.environ.get("FAKE_LIVEKIT_MARKER")
    if marker:
        with open(marker, "a") as handle:
            handle.write("start\\n")
    if mode == "crash":
        print("fatal: bad config", flush=True)
        sys.exit(3)
    if mode == "hang":
        time.sleep(60)
        sys.exit(0)
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", config["port"]))
    server.listen()
    print("starting LiveKit server", flush=True)
    if mode == "crash_after_ready":
        time.sleep(0.5)
        sys.exit(2)
    while True:
        connection, _ = server.accept()
        connection.close()
    """
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _settings() -> ServerSettings:
    key, secret = generate_credentials()
    tcp = _free_port()
    return ServerSettings(
        api_key=key,
        api_secret=secret,
        http_port=_free_port(),
        rtc_tcp_port=tcp,
        rtc_udp_port=_free_port(),
    )


@pytest.fixture
def fake_binary(tmp_path) -> Path:
    path = tmp_path / "fake-livekit-server"
    path.write_text(FAKE_SERVER.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def make_service(tmp_path):
    services: list[ConferenceServerService] = []

    def make(resolver, **kwargs) -> ConferenceServerService:
        service = ConferenceServerService(
            tmp_path / "conference", executable_resolver=resolver, **kwargs
        )
        services.append(service)
        return service

    yield make
    for service in services:
        service.stop(wait=True)
        service.deleteLater()


def _states(service: ConferenceServerService) -> list[str]:
    seen: list[str] = []
    service.state_changed.connect(seen.append)
    return seen


def test_starts_and_reports_running(make_service, fake_binary, monkeypatch):
    monkeypatch.setenv("FAKE_LIVEKIT_MODE", "ok")
    service = make_service(lambda: fake_binary)
    seen = _states(service)
    service.start(_settings())
    wait_until(
        lambda: service.state is ServerState.RUNNING, description="server running", timeout_ms=5000
    )
    assert seen[:2] == ["starting", "running"]
    assert service.local_url.startswith("ws://127.0.0.1:")
    service.stop(wait=True)
    assert service.state is ServerState.STOPPED


def test_config_file_is_owner_only_and_holds_the_secret(make_service, fake_binary, monkeypatch):
    monkeypatch.setenv("FAKE_LIVEKIT_MODE", "ok")
    service = make_service(lambda: fake_binary)
    settings = _settings()
    service.start(settings)
    wait_until(
        lambda: service.state is ServerState.RUNNING, description="server running", timeout_ms=5000
    )
    mode = stat.S_IMODE(service.config_path.stat().st_mode)
    assert mode == 0o600
    assert settings.api_secret in service.config_path.read_text()


def test_restarts_after_a_crash(make_service, fake_binary, monkeypatch, tmp_path):
    marker = tmp_path / "starts.log"
    monkeypatch.setenv("FAKE_LIVEKIT_MODE", "crash_after_ready")
    monkeypatch.setenv("FAKE_LIVEKIT_MARKER", str(marker))
    service = make_service(lambda: fake_binary, max_restarts=10)
    seen = _states(service)
    service.start(_settings())
    wait_until(
        lambda: "restarting" in seen and seen.count("running") >= 2,
        description=f"a restart after the crash (states: {seen})",
        timeout_ms=10000,
    )
    assert marker.read_text().count("start") >= 2


def test_gives_up_after_the_restart_budget(make_service, fake_binary, monkeypatch):
    monkeypatch.setenv("FAKE_LIVEKIT_MODE", "crash")
    service = make_service(lambda: fake_binary, max_restarts=2)
    service._restart_delay_ms = 10  # keep the test fast; backoff doubles from here
    errors: list[str] = []
    service.error.connect(errors.append)
    service.start(_settings())
    service._restart_delay_ms = 10
    wait_until(
        lambda: service.state is ServerState.FAILED, description="failed state", timeout_ms=10000
    )
    assert errors and "keeps failing" in errors[-1]


def test_not_ready_in_time_counts_as_a_failure(make_service, fake_binary, monkeypatch):
    monkeypatch.setenv("FAKE_LIVEKIT_MODE", "hang")
    service = make_service(lambda: fake_binary, ready_timeout_ms=300, max_restarts=0)
    service.start(_settings())
    wait_until(
        lambda: service.state is ServerState.FAILED, description="failed state", timeout_ms=5000
    )


def test_missing_binary_fails_with_a_clear_error(make_service):
    def missing() -> Path:
        raise ServerUnavailableError("LiveKit server not found")

    service = make_service(missing)
    errors: list[str] = []
    service.error.connect(errors.append)
    service.start(_settings())
    assert service.state is ServerState.FAILED
    assert errors == ["LiveKit server not found"]


def test_invalid_settings_never_reach_the_process(make_service, fake_binary):
    service = make_service(lambda: fake_binary)
    with pytest.raises(ValueError):
        service.start(ServerSettings(api_key="SOLKEY", api_secret="short"))
    assert service.state is ServerState.STOPPED


def _real_binary() -> Path | None:
    try:
        return server_executable()
    except ServerUnavailableError:
        return None


@pytest.mark.skipif(_real_binary() is None, reason="run scripts/fetch_livekit_server.py first")
def test_real_livekit_server_starts(make_service):
    service = make_service(server_executable)
    service.start(_settings())
    wait_until(
        lambda: service.state is ServerState.RUNNING,
        description="real server running",
        timeout_ms=15000,
    )
