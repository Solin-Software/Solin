from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest


_SOLIN_REGISTRY_ROOT = r"Software\Solin"
_SOLIN_INSTALLER_REGISTRY_KEY = rf"{_SOLIN_REGISTRY_ROOT}\Solin"


def path_from_env(name: str, *, purpose: str) -> Path:
    raw_path = os.environ.get(name, "").strip()
    if not raw_path:
        pytest.skip(f"Set {name} to run {purpose}.")
    path = Path(raw_path)
    if not path.is_file():
        pytest.fail(f"{name} does not point to a file: {path}")
    return path


def directory_from_env(name: str, *, purpose: str) -> Path:
    raw_path = os.environ.get(name, "").strip()
    if not raw_path:
        pytest.skip(f"Set {name} to run {purpose}.")
    path = Path(raw_path)
    if not path.is_dir():
        pytest.fail(f"{name} does not point to a directory: {path}")
    return path


def isolated_app_env(temp_root: Path) -> dict[str, str]:
    home_dir = temp_root / "Home"
    xdg_config_dir = temp_root / "Config"
    xdg_data_dir = temp_root / "Data"
    xdg_cache_dir = temp_root / "Cache"
    temporary_dir = temp_root / "Temp"
    for path in (
        temp_root / "AppData" / "Roaming",
        temp_root / "AppData" / "Local",
        home_dir,
        xdg_config_dir,
        xdg_data_dir,
        xdg_cache_dir,
        temporary_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = {"win32": "windows", "darwin": "cocoa"}.get(sys.platform, "xcb")
    env["APPDATA"] = str(temp_root / "AppData" / "Roaming")
    env["LOCALAPPDATA"] = str(temp_root / "AppData" / "Local")
    env["HOME"] = str(home_dir)
    env["XDG_CONFIG_HOME"] = str(xdg_config_dir)
    env["XDG_DATA_HOME"] = str(xdg_data_dir)
    env["XDG_CACHE_HOME"] = str(xdg_cache_dir)
    # Qt's Unix local sockets and AppImage extraction use the temporary directory.
    # Keep it shared within one upgrade pair and private between separate tests.
    env["TMPDIR"] = str(temporary_dir)
    env["TMP"] = str(temporary_dir)
    env["TEMP"] = str(temporary_dir)
    env["SOLIN_IPC_SERVER_NAME"] = f"sln-{uuid.uuid4().hex[:12]}"
    env.setdefault(
        "QT_LOGGING_RULES",
        "qt.qpa.mime=false",
    )
    return env


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_profile_user_state(data_dir: Path, profile_id: str) -> dict[Path, str]:
    profile_dir = data_dir / "profiles" / profile_id
    sentinels = {
        profile_dir / "images" / "upgrade-image.txt": "image survives upgrade",
        profile_dir / "embedded" / "upgrade-media.bin": "media survives upgrade",
    }

    _write_json(
        data_dir / "profiles.json",
        {
            "profiles": [
                {
                    "id": profile_id,
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


def assert_profile_user_state_survived(sentinels: dict[Path, str]) -> None:
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


def _solin_registry_guard_targets(
    winreg: Any, profile_ids: tuple[str, ...]
) -> tuple[tuple[str, Any, str], ...]:
    targets: list[tuple[str, Any, str]] = [
        ("HKCU", winreg.HKEY_CURRENT_USER, _SOLIN_REGISTRY_ROOT),
    ]
    targets.extend(
        ("HKCU", winreg.HKEY_CURRENT_USER, f"Software\\Solin_{profile_id}")
        for profile_id in profile_ids
    )
    # Machine-scope installer metadata lives one level below the vendor
    # container. Inno Setup can legitimately leave an empty Software\Solin
    # parent after uninstall, so only the actual install marker is a conflict.
    targets.append(
        ("HKLM", winreg.HKEY_LOCAL_MACHINE, _SOLIN_INSTALLER_REGISTRY_KEY)
    )
    return tuple(targets)


def skip_if_solin_registry_exists(*profile_ids: str) -> None:
    if os.name != "nt":
        return
    winreg = _winreg()
    for root_name, root, subkey in _solin_registry_guard_targets(winreg, profile_ids):
        if _registry_key_exists(root, subkey):
            pytest.skip(
                f"{root_name}\\{subkey} already exists; refusing to mutate "
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


def cleanup_solin_test_registry(*profile_ids: str) -> None:
    if os.name != "nt":
        return
    winreg = _winreg()
    subkeys = [_SOLIN_REGISTRY_ROOT]
    subkeys.extend(f"Software\\Solin_{profile_id}" for profile_id in profile_ids)
    for subkey in subkeys:
        _delete_registry_tree(winreg.HKEY_CURRENT_USER, subkey)


def assert_process_survives_startup(
    exe_path: Path,
    *,
    args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
) -> None:
    startup_seconds = float(os.environ.get("SOLIN_E2E_STARTUP_SECONDS", "8"))
    shutdown_timeout = float(os.environ.get("SOLIN_E2E_SHUTDOWN_TIMEOUT", "10"))

    process = subprocess.Popen(  # noqa: S603 - e2e runs a user-supplied app artifact
        [str(exe_path), *args],
        cwd=exe_path.parent,
        env=env,
        start_new_session=os.name == "posix",
    )
    try:
        deadline = time.monotonic() + startup_seconds
        while time.monotonic() < deadline:
            exit_code = process.poll()
            if exit_code is not None:
                pytest.fail(f"Packaged app exited during startup with {exit_code}.")
            time.sleep(0.2)
    finally:
        if os.name == "posix":
            # AppImage extract-and-run forks AppRun and waits in the runtime.
            # Terminating only that launcher leaves the application (and its
            # single-instance socket) alive across replacement tests.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=shutdown_timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
            pytest.fail("Packaged app did not stop after the smoke test.")
        finally:
            if os.name == "posix":
                # A launcher may exit before its descendants, including on a
                # startup failure. Always dispose of its owned group so no
                # child survives the helper, even if it ignores SIGTERM.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
