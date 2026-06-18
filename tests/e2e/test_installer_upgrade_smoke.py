from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._packaged_app import (
    assert_process_survives_startup,
    isolated_app_env,
    path_from_env,
)


pytestmark = pytest.mark.e2e

_APP_EXE = "Solin.exe"
_PROFILE_ID = "upgrade_profile"
_REGISTRY_SUBKEYS = (
    r"Software\Solin",
    rf"Software\Solin_{_PROFILE_ID}",
)


def _require_windows_installer_opt_in() -> None:
    if os.name != "nt":
        pytest.skip("Windows installer upgrade smoke tests run only on Windows.")
    if os.environ.get("SOLIN_E2E_ALLOW_INSTALLER_MUTATION") != "1":
        pytest.skip(
            "Set SOLIN_E2E_ALLOW_INSTALLER_MUTATION=1 to run installer e2e tests."
        )


def _winreg() -> Any:
    import winreg

    return winreg


def _registry_key_exists(root: Any, subkey: str) -> bool:
    winreg = _winreg()
    try:
        with winreg.OpenKey(root, subkey):
            return True
    except FileNotFoundError:
        return False
    except PermissionError:
        return True


def _skip_if_real_solin_registry_exists() -> None:
    winreg = _winreg()
    for root_name, root in (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE)):
        if _registry_key_exists(root, r"Software\Solin"):
            pytest.skip(
                f"{root_name}\\Software\\Solin already exists; refusing to mutate "
                "a machine with an existing Solin installation."
            )


def _delete_registry_tree(root: Any, subkey: str) -> None:
    winreg = _winreg()
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            children: list[str] = []
            index = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, index))
                    index += 1
                except OSError:
                    break
    except FileNotFoundError:
        return

    for child in children:
        _delete_registry_tree(root, rf"{subkey}\{child}")
    try:
        winreg.DeleteKey(root, subkey)
    except FileNotFoundError:
        return


def _cleanup_test_registry() -> None:
    winreg = _winreg()
    for subkey in _REGISTRY_SUBKEYS:
        _delete_registry_tree(winreg.HKEY_CURRENT_USER, subkey)


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


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_user_state(temp_root: Path) -> dict[Path, str]:
    data_dir = temp_root / "AppData" / "Roaming" / "Solin" / "Solin"
    profile_dir = data_dir / "profiles" / _PROFILE_ID
    sentinels = {
        profile_dir / "images" / "upgrade-image.txt": "image survives upgrade",
        profile_dir / "embedded" / "upgrade-media.bin": "media survives upgrade",
    }

    _write_json(
        data_dir / "profiles.json",
        {
            "profiles": [
                {
                    "id": _PROFILE_ID,
                    "name": "Upgrade Profile",
                    "created_at": 1_700_000_000.0,
                }
            ]
        },
    )
    _write_json(
        profile_dir / "playlists.json",
        {
            "playlists": [
                {
                    "id": "upgrade-playlist",
                    "name": "Upgrade Playlist",
                    "items": [],
                }
            ]
        },
    )
    _write_json(
        profile_dir / "meeting_trees.json",
        {
            "version": 1,
            "trees": {
                "upgrade-week": {
                    "nodes": [],
                    "last_canonical_hash": "upgrade-sentinel",
                }
            },
        },
    )
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return sentinels


def _seed_qsettings() -> None:
    from PySide6.QtCore import QSettings

    global_settings = QSettings("Solin", "GlobalApp")
    global_settings.setValue("last_active_profile", _PROFILE_ID)
    global_settings.sync()

    profile_settings = QSettings(f"Solin_{_PROFILE_ID}", "App")
    profile_settings.setValue("language", "pt_BR")
    profile_settings.sync()


def _assert_user_state_survived(sentinels: dict[Path, str]) -> None:
    for path, expected in sentinels.items():
        assert path.read_text(encoding="utf-8") == expected

    profile_dir = next(iter(sentinels)).parents[1]
    playlists = json.loads((profile_dir / "playlists.json").read_text(encoding="utf-8"))
    meeting_trees = json.loads(
        (profile_dir / "meeting_trees.json").read_text(encoding="utf-8")
    )
    assert playlists["playlists"][0]["id"] == "upgrade-playlist"
    assert (
        meeting_trees["trees"]["upgrade-week"]["last_canonical_hash"]
        == "upgrade-sentinel"
    )

    from PySide6.QtCore import QSettings

    assert QSettings("Solin", "GlobalApp").value("last_active_profile") == _PROFILE_ID
    assert QSettings(f"Solin_{_PROFILE_ID}", "App").value("language") == "pt_BR"


def test_full_installer_upgrade_preserves_user_state() -> None:
    _require_windows_installer_opt_in()
    _skip_if_real_solin_registry_exists()
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
            sentinels = _seed_user_state(temp_root)
            _seed_qsettings()

            _run_installer(new_installer, install_dir, env)
            _assert_user_state_survived(sentinels)
            assert_process_survives_startup(install_dir / _APP_EXE, env=env)
        finally:
            _run_uninstaller(install_dir, env)
            _cleanup_test_registry()
            shutil.rmtree(install_dir, ignore_errors=True)
