from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.e2e._packaged_app import (
    assert_profile_user_state_survived,
    assert_process_survives_startup,
    directory_from_env,
    isolated_app_env,
    seed_profile_user_state,
)


pytestmark = pytest.mark.e2e

_APP_BUNDLE = "Solin.app"
_APP_EXE = Path("Contents") / "MacOS" / "main"
_PROFILE_ID = "macos_upgrade_profile"


def _require_macos() -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS app replacement smoke tests run only on macOS.")


def _app_executable(app_bundle: Path) -> Path:
    executable = app_bundle / _APP_EXE
    if not executable.is_file():
        pytest.fail(f"App bundle executable was not found: {executable}")
    if not os.access(executable, os.X_OK):
        pytest.fail(f"App bundle executable is not marked executable: {executable}")
    return executable


def _copy_app_bundle(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ditto", str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Could not copy app bundle from {source} to {destination}.\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )


def _macos_data_dir(env: dict[str, str]) -> Path:
    return (
        Path(env["HOME"])
        / "Library"
        / "Application Support"
        / "Solin"
        / "Solin"
    )


def _run_qsettings_script(env: dict[str, str], script: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"QSettings helper failed with {result.returncode}.\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )


def _seed_qsettings(env: dict[str, str]) -> None:
    _run_qsettings_script(
        env,
        f"""
from PySide6.QtCore import QSettings

profile_id = {_PROFILE_ID!r}
global_settings = QSettings("Solin", "GlobalApp")
global_settings.setValue("last_active_profile", profile_id)
global_settings.sync()

profile_settings = QSettings(f"Solin_{{profile_id}}", "App")
profile_settings.setValue("language", "pt_BR")
profile_settings.sync()
""",
    )


def _assert_qsettings_survived(env: dict[str, str]) -> None:
    _run_qsettings_script(
        env,
        f"""
from PySide6.QtCore import QSettings

profile_id = {_PROFILE_ID!r}
last_active = QSettings("Solin", "GlobalApp").value("last_active_profile")
language = QSettings(f"Solin_{{profile_id}}", "App").value("language")
assert last_active == profile_id, last_active
assert language == "pt_BR", language
""",
    )


def test_macos_app_replacement_preserves_user_state() -> None:
    _require_macos()
    old_app = directory_from_env(
        "SOLIN_OLD_MACOS_APP",
        purpose="macOS app replacement smoke tests",
    )
    new_app = directory_from_env(
        "SOLIN_NEW_MACOS_APP",
        purpose="macOS app replacement smoke tests",
    )
    _app_executable(old_app)
    _app_executable(new_app)

    with tempfile.TemporaryDirectory(prefix="sln-upgrade-") as temp_dir:
        temp_root = Path(temp_dir)
        env = isolated_app_env(temp_root)
        installed_app = temp_root / "Applications" / _APP_BUNDLE

        _copy_app_bundle(old_app, installed_app)
        sentinels = seed_profile_user_state(_macos_data_dir(env), _PROFILE_ID)
        _seed_qsettings(env)

        _copy_app_bundle(new_app, installed_app)
        assert_profile_user_state_survived(sentinels)
        _assert_qsettings_survived(env)
        assert_process_survives_startup(_app_executable(installed_app), env=env)
