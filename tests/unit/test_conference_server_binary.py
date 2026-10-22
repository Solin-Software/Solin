from __future__ import annotations

import pytest

from solin.core.conference.server_binary import (
    OVERRIDE_ENVIRONMENT,
    ServerUnavailableError,
    executable_name,
    platform_key,
    server_executable,
)


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("linux", "x86_64", "linux-amd64"),
        ("linux", "aarch64", "linux-arm64"),
        ("win32", "AMD64", "windows-amd64"),
        ("win32", "ARM64", "windows-arm64"),
    ],
)
def test_platform_key(system, machine, expected):
    assert platform_key(system, machine) == expected


@pytest.mark.parametrize(("system", "machine"), [("darwin", "arm64"), ("linux", "riscv64")])
def test_unsupported_platforms_are_reported(system, machine):
    with pytest.raises(ServerUnavailableError):
        platform_key(system, machine)


def test_executable_name():
    assert executable_name("win32") == "livekit-server.exe"
    assert executable_name("linux") == "livekit-server"


def test_override_wins(tmp_path):
    binary = tmp_path / "custom-livekit"
    binary.write_text("")
    assert server_executable(environ={OVERRIDE_ENVIRONMENT: str(binary)}) == binary


def test_override_must_exist(tmp_path):
    with pytest.raises(ServerUnavailableError):
        server_executable(environ={OVERRIDE_ENVIRONMENT: str(tmp_path / "missing")})


def test_compiled_build_uses_bin_next_to_the_app(tmp_path):
    app = tmp_path / "solin"
    app.write_text("")
    (tmp_path / "bin").mkdir()
    binary = tmp_path / "bin" / executable_name()
    binary.write_text("")
    assert server_executable(environ={}, compiled=True, app_executable=str(app)) == binary


def test_development_checkout_uses_fetched_build(tmp_path):
    binary = tmp_path / "build" / "livekit" / platform_key() / executable_name()
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    assert server_executable(environ={}, compiled=False, repo_root=tmp_path) == binary


def test_missing_binary_points_at_the_fetch_script(tmp_path):
    with pytest.raises(ServerUnavailableError, match="fetch_livekit_server"):
        server_executable(environ={}, compiled=False, repo_root=tmp_path)
