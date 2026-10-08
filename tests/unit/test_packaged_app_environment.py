from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from tests.e2e._packaged_app import isolated_app_env


@pytest.fixture
def environment_root():
    # Keep Unix sockets within sun_path on both Linux and macOS runners.
    directory = "/tmp" if os.name == "posix" else None
    with tempfile.TemporaryDirectory(prefix="sln-", dir=directory) as root:
        yield Path(root)


@pytest.mark.parametrize("inherited", [False, True])
def test_windows_preserves_installer_owned_temporary_environment(
    environment_root, monkeypatch, inherited,
):
    monkeypatch.setattr(sys, "platform", "win32")
    for key in ("TMPDIR", "TMP", "TEMP"):
        if inherited:
            monkeypatch.setenv(key, str(environment_root / "SystemTemp"))
        else:
            monkeypatch.delenv(key, raising=False)

    env = isolated_app_env(environment_root)

    temporary_values = {key: env.get(key) for key in ("TMPDIR", "TMP", "TEMP")}
    inherited_values = {key: os.environ.get(key) for key in ("TMPDIR", "TMP", "TEMP")}
    assert temporary_values == inherited_values
    assert not (environment_root / "Temp").exists()
    assert env["APPDATA"] == str(environment_root / "AppData" / "Roaming")
    assert env["LOCALAPPDATA"] == str(environment_root / "AppData" / "Local")
    assert env["QT_QPA_PLATFORM"] == "windows"
    assert env["SOLIN_IPC_SERVER_NAME"] != isolated_app_env(environment_root)["SOLIN_IPC_SERVER_NAME"]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_unix_smokes_isolate_socket_and_extraction_directories(
    environment_root, monkeypatch, platform,
):
    monkeypatch.setattr(sys, "platform", platform)
    env = isolated_app_env(environment_root)
    for key in ("TMPDIR", "TMP", "TEMP"):
        assert env[key] == str(environment_root / "Temp")
        assert Path(env[key]).is_dir()
    assert env["HOME"] == str(environment_root / "Home")
    assert env["XDG_DATA_HOME"] == str(environment_root / "Data")
