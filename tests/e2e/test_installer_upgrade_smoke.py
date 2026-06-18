from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.e2e._packaged_app import (
    assert_profile_user_state_survived,
    assert_process_survives_startup,
    cleanup_solin_test_registry,
    isolated_app_env,
    path_from_env,
    seed_profile_user_state,
    skip_if_solin_registry_exists,
)


pytestmark = pytest.mark.e2e

_APP_EXE = "Solin.exe"
_PROFILE_ID = "upgrade_profile"


def _require_windows_installer_opt_in() -> None:
    if os.name != "nt":
        pytest.skip("Windows installer upgrade smoke tests run only on Windows.")
    if os.environ.get("SOLIN_E2E_ALLOW_INSTALLER_MUTATION") != "1":
        pytest.skip(
            "Set SOLIN_E2E_ALLOW_INSTALLER_MUTATION=1 to run installer e2e tests."
        )


def _run_installer(installer: Path, install_dir: Path, env: dict[str, str]) -> None:
    timeout = float(os.environ.get("SOLIN_E2E_INSTALLER_TIMEOUT", "240"))
    result = subprocess.run(  # noqa: S603 - e2e runs user-supplied installer artifacts
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CURRENTUSER",
            f"/DIR={install_dir}",
        ],
        cwd=installer.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Installer failed with exit code {result.returncode}: {installer}\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    installed_exe = install_dir / _APP_EXE
    if not installed_exe.is_file():
        pytest.fail(f"Installer did not produce {installed_exe}")


def _run_uninstaller(install_dir: Path, env: dict[str, str]) -> None:
    uninstallers = sorted(install_dir.glob("unins*.exe"))
    if not uninstallers:
        return
    subprocess.run(  # noqa: S603 - e2e runs installer-generated uninstaller
        [
            str(uninstallers[0]),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
        ],
        cwd=install_dir,
        env=env,
        timeout=float(os.environ.get("SOLIN_E2E_INSTALLER_TIMEOUT", "240")),
        check=False,
    )


def _seed_qsettings() -> None:
    from PySide6.QtCore import QSettings

    global_settings = QSettings("Solin", "GlobalApp")
    global_settings.setValue("last_active_profile", _PROFILE_ID)
    global_settings.sync()

    profile_settings = QSettings(f"Solin_{_PROFILE_ID}", "App")
    profile_settings.setValue("language", "pt_BR")
    profile_settings.sync()


def _assert_qsettings_survived() -> None:
    from PySide6.QtCore import QSettings

    assert QSettings("Solin", "GlobalApp").value("last_active_profile") == _PROFILE_ID
    assert QSettings(f"Solin_{_PROFILE_ID}", "App").value("language") == "pt_BR"


def test_full_installer_upgrade_preserves_user_state() -> None:
    _require_windows_installer_opt_in()
    skip_if_solin_registry_exists(_PROFILE_ID)
    old_installer = path_from_env(
        "SOLIN_OLD_INSTALLER",
        purpose="full installer upgrade smoke tests",
    )
    new_installer = path_from_env(
        "SOLIN_NEW_INSTALLER",
        purpose="full installer upgrade smoke tests",
    )

    with tempfile.TemporaryDirectory(prefix="solin-upgrade-e2e-") as temp_dir:
        temp_root = Path(temp_dir)
        install_dir = temp_root / "Install" / "Solin"
        env = isolated_app_env(temp_root)

        try:
            _run_installer(old_installer, install_dir, env)
            data_dir = temp_root / "AppData" / "Roaming" / "Solin" / "Solin"
            sentinels = seed_profile_user_state(data_dir, _PROFILE_ID)
            _seed_qsettings()

            _run_installer(new_installer, install_dir, env)
            assert_profile_user_state_survived(sentinels)
            _assert_qsettings_survived()
            assert_process_survives_startup(install_dir / _APP_EXE, env=env)
        finally:
            _run_uninstaller(install_dir, env)
            cleanup_solin_test_registry(_PROFILE_ID)
            shutil.rmtree(install_dir, ignore_errors=True)
