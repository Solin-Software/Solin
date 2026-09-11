"""Validate replacing an AppImage preserves profile data and starts on XCB."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from tests.e2e._packaged_app import (
    assert_process_survives_startup,
    assert_profile_user_state_survived,
    isolated_app_env,
    path_from_env,
    seed_profile_user_state,
)

pytestmark = pytest.mark.e2e


def test_appimage_replacement_preserves_user_state(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("AppImage replacement requires Linux.")
    old = path_from_env("SOLIN_OLD_APPIMAGE", purpose="AppImage upgrade")
    new = path_from_env("SOLIN_NEW_APPIMAGE", purpose="AppImage upgrade")
    env = isolated_app_env(tmp_path)
    env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    installed = tmp_path / "Solin.AppImage"
    shutil.copyfile(old, installed)
    installed.chmod(0o755)
    assert_process_survives_startup(installed, args=("--create-profile",), env=env)
    sentinels = seed_profile_user_state(Path(env["XDG_DATA_HOME"]) / "Solin" / "Solin", "upgrade")
    shutil.copyfile(new, installed)
    installed.chmod(0o755)
    assert_process_survives_startup(installed, args=("--create-profile",), env=env)
    assert_profile_user_state_survived(sentinels)
