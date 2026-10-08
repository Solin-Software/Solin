from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

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
from tests.e2e._http_runtime import assert_packaged_http_runtime
from tests.e2e._windows_process import run_windows_process_tree


pytestmark = pytest.mark.e2e

_APP_EXE = "Solin.exe"
_PROFILE_ID = "upgrade_profile"
_CAMERA_CLSID = "{08AFA2E5-0293-4E56-9FE1-2A79DAE8E28F}"
_VIDEO_INPUT_CATEGORY = "{860BB310-5D01-11D0-BD3B-00A0C911CE86}"
_PE_MACHINES = {"x64": 0x8664, "x86": 0x014C}
_InstallScope = Literal["user", "machine"]


def _camera_registration_keys_exist(architecture: str, scope: _InstallScope) -> bool:
    import winreg

    view = winreg.KEY_WOW64_64KEY if architecture == "x64" else winreg.KEY_WOW64_32KEY
    hive = winreg.HKEY_LOCAL_MACHINE if scope == "machine" else winreg.HKEY_CURRENT_USER
    for subkey in (
        f"Software\\Classes\\CLSID\\{_CAMERA_CLSID}",
        f"Software\\Classes\\CLSID\\{_VIDEO_INPUT_CATEGORY}\\Instance\\{_CAMERA_CLSID}",
    ):
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view):
                return True
        except FileNotFoundError:
            pass
    return False


def _camera_registration(
    architecture: str, scope: _InstallScope
) -> tuple[Path, str, str, bytes] | None:
    import winreg

    view = winreg.KEY_WOW64_64KEY if architecture == "x64" else winreg.KEY_WOW64_32KEY
    class_path = f"Software\\Classes\\CLSID\\{_CAMERA_CLSID}\\InprocServer32"
    category_path = f"Software\\Classes\\CLSID\\{_VIDEO_INPUT_CATEGORY}\\Instance\\{_CAMERA_CLSID}"
    hive = winreg.HKEY_LOCAL_MACHINE if scope == "machine" else winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(hive, class_path, 0, winreg.KEY_READ | view) as key:
            dll_path = Path(winreg.QueryValueEx(key, "")[0])
            threading_model = str(winreg.QueryValueEx(key, "ThreadingModel")[0])
        with winreg.OpenKey(hive, category_path, 0, winreg.KEY_READ | view) as key:
            friendly_name = str(winreg.QueryValueEx(key, "FriendlyName")[0])
            filter_data = winreg.QueryValueEx(key, "FilterData")[0]
    except FileNotFoundError:
        return None
    return dll_path, threading_model, friendly_name, filter_data


def _pe_machine(path: Path) -> int:
    with path.open("rb") as binary:
        if binary.read(2) != b"MZ":
            return 0
        binary.seek(0x3C)
        offset = binary.read(4)
        if len(offset) != 4:
            return 0
        binary.seek(struct.unpack("<I", offset)[0])
        if binary.read(4) != b"PE\0\0":
            return 0
        machine = binary.read(2)
        return struct.unpack("<H", machine)[0] if len(machine) == 2 else 0


def _assert_camera_registered(env: dict[str, str], scope: _InstallScope) -> None:
    for architecture, machine in _PE_MACHINES.items():
        registration = _camera_registration(architecture, scope)
        assert registration is not None, f"missing {architecture} camera registration"
        dll_path, threading_model, friendly_name, filter_data = registration
        assert dll_path.is_absolute() and dll_path.is_file()
        assert _pe_machine(dll_path) == machine
        assert threading_model.casefold() == "both"
        assert friendly_name == "Solin Virtual Camera"
        assert isinstance(filter_data, bytes) and filter_data
        expected_root = Path(
            os.environ["ProgramFiles"] if scope == "machine" else os.environ["LOCALAPPDATA"]
        ).resolve()
        assert dll_path.resolve().is_relative_to(expected_root)
        harness = path_from_env(
            f"SOLIN_DIRECTSHOW_HARNESS_{architecture.upper()}",
            purpose=f"{architecture} installed DirectShow graph verification",
        )
        verification = subprocess.run(  # noqa: S603 - trusted build harness
            [str(harness), str(dll_path)],
            cwd=harness.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if verification.returncode != 0:
            pytest.fail(
                f"{architecture} installed DirectShow filter is not operational\n"
                f"stdout:\n{verification.stdout}\n\nstderr:\n{verification.stderr}"
            )


def _assert_camera_absent(scope: _InstallScope) -> None:
    for architecture in _PE_MACHINES:
        assert not _camera_registration_keys_exist(architecture, scope), (
            f"stale {architecture} DirectShow registration remained after uninstall"
        )


def _camera_registration_paths(scope: _InstallScope) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for architecture in _PE_MACHINES:
        registration = _camera_registration(architecture, scope)
        assert registration is not None
        paths[architecture] = registration[0]
    return paths


def _assert_camera_pair_identity(paths: dict[str, Path]) -> None:
    x64_digest = hashlib.sha256(paths["x64"].read_bytes()).hexdigest()
    x86_digest = hashlib.sha256(paths["x86"].read_bytes()).hexdigest()
    pair_id = hashlib.sha256(f"{x64_digest}:{x86_digest}".encode("ascii")).hexdigest()[:20]
    for architecture, path in paths.items():
        assert path.parent.name == architecture
        assert path.parent.parent.name == pair_id


def _assert_reinstall_with_loaded_camera(
    installer: Path, install_dir: Path, env: dict[str, str], scope: _InstallScope
) -> None:
    import ctypes
    from ctypes import wintypes

    paths = _camera_registration_paths(scope)
    _assert_camera_pair_identity(paths)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryW.argtypes = [wintypes.LPCWSTR]
    kernel32.LoadLibraryW.restype = wintypes.HMODULE
    kernel32.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel32.FreeLibrary.restype = wintypes.BOOL
    handle = kernel32.LoadLibraryW(str(paths["x64"]))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Repairing the same build must reuse the pair without replacing a DLL
        # already loaded by a consumer.
        _run_installer(installer, install_dir, env, scope)
        assert _camera_registration_paths(scope) == paths
        _assert_camera_pair_identity(paths)
        _run_uninstaller(install_dir, env)
        _assert_camera_absent(scope)
        assert paths["x64"].is_file(), "a loaded DLL must survive file cleanup"
        # Windows retains the loaded file, but the camera is unregistered. That
        # residue must allow an immediate reinstall before closing the consumer.
        _run_installer(installer, install_dir, env, scope)
        assert _camera_registration_paths(scope) == paths
        _assert_camera_pair_identity(paths)
        _assert_camera_registered(env, scope)
        assert_packaged_http_runtime(install_dir / _APP_EXE, env=env)
    finally:
        if not kernel32.FreeLibrary(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def _require_windows_installer_opt_in() -> None:
    if os.name != "nt":
        pytest.skip("Windows installer upgrade smoke tests run only on Windows.")
    if os.environ.get("SOLIN_E2E_ALLOW_INSTALLER_MUTATION") != "1":
        pytest.skip("Set SOLIN_E2E_ALLOW_INSTALLER_MUTATION=1 to run installer e2e tests.")


def _run_installer(
    installer: Path,
    install_dir: Path,
    env: dict[str, str],
    scope: _InstallScope,
) -> None:
    result = _run_logged_installer_process(
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/ALLUSERS" if scope == "machine" else "/CURRENTUSER",
            f"/DIR={install_dir}",
        ],
        cwd=installer.parent,
        env=env,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Installer failed with exit code {result.returncode}: {installer}\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    installed_exe = install_dir / _APP_EXE
    if not installed_exe.is_file():
        pytest.fail(f"Installer did not produce {installed_exe}")


def _run_rollback_injection(
    installer: Path,
    install_dir: Path,
    env: dict[str, str],
    scope: _InstallScope,
) -> None:
    result = _run_logged_installer_process(
        [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/ALLUSERS" if scope == "machine" else "/CURRENTUSER",
            f"/DIR={install_dir}",
        ],
        cwd=installer.parent,
        env=env,
    )
    if result.returncode == 0:
        pytest.fail("The x86 registration failure-injection installer succeeded")


def _run_uninstaller(install_dir: Path, env: dict[str, str]) -> None:
    uninstallers = sorted(install_dir.glob("unins*.exe"))
    if not uninstallers:
        pytest.fail(f"Installer did not produce an uninstaller in {install_dir}")
    result = _run_logged_installer_process(
        [
            str(uninstallers[0]),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
        ],
        cwd=uninstallers[0].parent.parent,
        env=env,
    )
    if result.returncode != 0:
        pytest.fail(
            f"Uninstaller failed with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )


def _run_logged_installer_process(
    args: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    # Keep diagnostics outside {app}: uninstall can remove the application tree.
    # The job waits for Inno's second phase as well as the original launcher.
    with tempfile.TemporaryDirectory(prefix="solin-installer-log-") as directory:
        log = Path(directory) / "setup.log"
        command = [*args, f"/LOG={log}"]
        try:
            result = run_windows_process_tree(
                command,
                cwd=cwd,
                env=env,
                timeout=float(os.environ.get("SOLIN_E2E_INSTALLER_TIMEOUT", "240")),
            )
        except subprocess.TimeoutExpired as error:
            error.add_note(
                log.read_text(encoding="utf-8-sig", errors="replace")
                if log.exists()
                else "Inno did not create a log."
            )
            raise
        if log.exists():
            result.stdout += "\nInno log:\n" + log.read_text(encoding="utf-8-sig", errors="replace")
        return result


def _cleanup_installation(
    install_dir: Path,
    env: dict[str, str],
    scope: _InstallScope,
    camera_paths: dict[str, Path],
) -> None:
    primary_error = sys.exception()
    try:
        # A failed reinstall may follow a completed uninstall. Do not replace
        # that failure with a misleading "missing uninstaller" cleanup error.
        if any(install_dir.glob("unins*.exe")):
            _run_uninstaller(install_dir, env)
        _assert_camera_absent(scope)
        for path in camera_paths.values():
            assert not path.exists(), "unloaded camera DLL survived uninstall"
    except (Exception, pytest.fail.Exception) as cleanup_error:  # noqa: BLE001 - retain primary failure
        if primary_error is None:
            raise
        primary_error.add_note(f"Installer cleanup also failed: {cleanup_error}")


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
    rollback_installer = path_from_env(
        "SOLIN_ROLLBACK_INSTALLER",
        purpose="DirectShow registration rollback smoke tests",
    )
    if any(_camera_registration_keys_exist(architecture, "user") for architecture in _PE_MACHINES):
        pytest.skip("The installer smoke requires both DirectShow registry views to be clean.")

    with tempfile.TemporaryDirectory(prefix="solin-upgrade-e2e-") as temp_dir:
        temp_root = Path(temp_dir)
        install_dir = temp_root / "Install" / "Solin"
        env = isolated_app_env(temp_root)
        installed = False
        registration_paths: dict[str, Path] = {}

        try:
            _run_installer(old_installer, install_dir, env, "user")
            installed = True
            data_dir = temp_root / "AppData" / "Roaming" / "Solin" / "Solin"
            sentinels = seed_profile_user_state(data_dir, _PROFILE_ID)
            _seed_qsettings()

            # A previous development build can include an optional extension
            # absent from the release payload. Even an unloaded .pyd makes
            # Python see backports.zstd as a namespace, breaking urllib3 imports.
            obsolete_extension = install_dir / "backports" / "zstd" / "_obsolete.pyd"
            obsolete_extension.parent.mkdir(parents=True, exist_ok=True)
            obsolete_extension.write_bytes(b"previous build extension")
            unrelated_file = install_dir / "user-kept.txt"
            unrelated_file.write_text("user-owned file", encoding="utf-8")

            _run_installer(new_installer, install_dir, env, "user")
            assert not obsolete_extension.exists(), "obsolete Python extension survived upgrade"
            assert unrelated_file.read_text(encoding="utf-8") == "user-owned file"
            assert_packaged_http_runtime(install_dir / _APP_EXE, env=env)
            _assert_camera_registered(env, "user")
            registration_paths = _camera_registration_paths("user")
            _run_rollback_injection(rollback_installer, install_dir, env, "user")
            assert _camera_registration_paths("user") == registration_paths
            _assert_camera_registered(env, "user")
            assert_profile_user_state_survived(sentinels)
            _assert_qsettings_survived()
            assert_process_survives_startup(install_dir / _APP_EXE, env=env)
            _assert_reinstall_with_loaded_camera(new_installer, install_dir, env, "user")
        finally:
            if installed:
                _cleanup_installation(install_dir, env, "user", registration_paths)
            cleanup_solin_test_registry(_PROFILE_ID)
            shutil.rmtree(install_dir, ignore_errors=True)


def test_full_installer_machine_scope_registers_camera_for_all_users() -> None:
    _require_windows_installer_opt_in()
    skip_if_solin_registry_exists(_PROFILE_ID)
    import ctypes

    if ctypes.windll.shell32.IsUserAnAdmin() == 0:
        pytest.skip("The machine-scope installer smoke requires an elevated runner.")
    new_installer = path_from_env(
        "SOLIN_NEW_INSTALLER",
        purpose="machine-scope full installer smoke tests",
    )
    rollback_installer = path_from_env(
        "SOLIN_ROLLBACK_INSTALLER",
        purpose="machine-scope DirectShow registration rollback smoke tests",
    )
    if any(
        _camera_registration_keys_exist(architecture, "machine") for architecture in _PE_MACHINES
    ):
        pytest.skip("The installer smoke requires clean machine DirectShow views.")
    prior_user_registration = {
        architecture: _camera_registration(architecture, "user") for architecture in _PE_MACHINES
    }

    with tempfile.TemporaryDirectory(prefix="solin-machine-e2e-") as temp_dir:
        temp_root = Path(temp_dir)
        install_dir = temp_root / "Install" / "Solin"
        env = isolated_app_env(temp_root)
        installed = False
        registration_paths: dict[str, Path] = {}
        try:
            _run_installer(new_installer, install_dir, env, "machine")
            installed = True
            assert_packaged_http_runtime(install_dir / _APP_EXE, env=env)
            _assert_camera_registered(env, "machine")
            registration_paths = _camera_registration_paths("machine")
            _run_rollback_injection(rollback_installer, install_dir, env, "machine")
            assert _camera_registration_paths("machine") == registration_paths
            _assert_camera_registered(env, "machine")
            _assert_reinstall_with_loaded_camera(new_installer, install_dir, env, "machine")
            assert {
                architecture: _camera_registration(architecture, "user")
                for architecture in _PE_MACHINES
            } == prior_user_registration
        finally:
            if installed:
                _cleanup_installation(install_dir, env, "machine", registration_paths)
            assert {
                architecture: _camera_registration(architecture, "user")
                for architecture in _PE_MACHINES
            } == prior_user_registration
            shutil.rmtree(install_dir, ignore_errors=True)
