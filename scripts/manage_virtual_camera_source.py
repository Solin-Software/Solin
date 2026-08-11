from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_CLSID = "{9B035447-3D53-4CD7-A6FC-7CE7862D8830}"
CLSID_KEY = rf"SOFTWARE\Classes\CLSID\{SOURCE_CLSID}"
INPROC_KEY = rf"{CLSID_KEY}\InprocServer32"
DISPLAY_NAME = "Solin Virtual Camera Media Source"
STAGED_DLL_NAME = "solin-virtual-camera-source"
DELETE_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4)


class VirtualCameraRegistrationError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or manage the development COM registration for the Solin camera source."
    )
    parser.add_argument("action", choices=("status", "register", "unregister"))
    parser.add_argument(
        "--configuration",
        choices=("Debug", "RelWithDebInfo", "Release"),
        default="Debug",
    )
    parser.add_argument("--dll", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace or remove a registration that points at another DLL.",
    )
    parser.add_argument(
        "--restart-frame-server",
        action="store_true",
        help=(
            "After registration, restart the Windows Frame Server so it loads "
            "the new DLL. This interrupts active camera consumers."
        ),
    )
    return parser


def _default_dll(configuration: str) -> Path:
    return (
        REPOSITORY_ROOT
        / "build"
        / "native"
        / "media-engine-gstreamer"
        / configuration
        / "solin-virtual-camera-source.dll"
    )


def _managed_staging_root() -> Path:
    winreg = _registry_module()
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion",
        access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
    ) as key:
        common_files, value_type = winreg.QueryValueEx(key, "CommonFilesDir")
    if value_type != winreg.REG_SZ or not isinstance(common_files, str):
        raise VirtualCameraRegistrationError(
            "The machine Common Files directory has an invalid registry value"
        )
    root = Path(common_files.strip())
    if not root.is_absolute():
        raise VirtualCameraRegistrationError("The machine Common Files directory must be absolute")
    return root / "Solin" / "VirtualCamera" / "Development"


def _source_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _is_managed_path(path: str | Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(_managed_staging_root().resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _stage_source(source: Path) -> Path:
    staging_root = _managed_staging_root()
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = staging_root / f".{STAGED_DLL_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        copied_digest = _source_digest(temporary)
        destination = staging_root / (f"{STAGED_DLL_NAME}-{copied_digest[:16]}.dll")
        if destination.is_file() and _source_digest(destination) == copied_digest:
            return destination
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _cleanup_staged_sources(*, keep: frozenset[Path] = frozenset()) -> None:
    staging_root = _managed_staging_root()
    if not staging_root.is_dir():
        return
    normalized_keep = {path.resolve(strict=False) for path in keep}
    pattern = f"{STAGED_DLL_NAME}-*.dll"
    for candidate in staging_root.glob(pattern):
        if candidate.resolve(strict=False) in normalized_keep:
            continue
        for delay in (*DELETE_RETRY_DELAYS_SECONDS, None):
            try:
                candidate.unlink(missing_ok=True)
                break
            except PermissionError:
                if delay is None:
                    print(
                        f"warning: obsolete staged camera source is still in use: {candidate}",
                        file=sys.stderr,
                    )
                    break
                time.sleep(delay)


def _registry_module():
    if sys.platform != "win32":
        raise VirtualCameraRegistrationError(
            "The Media Foundation source can only be registered on Windows"
        )
    import winreg

    return winreg


def _system_executable(*relative_parts: str) -> Path:
    system_root_text = os.environ.get("SystemRoot", "").strip()
    system_root = Path(system_root_text)
    if not system_root_text or not system_root.is_absolute():
        raise VirtualCameraRegistrationError("The Windows system directory is unavailable")
    executable = system_root.joinpath(*relative_parts)
    if not executable.is_file():
        raise VirtualCameraRegistrationError(
            f"The Windows system executable is unavailable: {executable.name}"
        )
    return executable


def _key_exists(path: str) -> bool:
    winreg = _registry_module()
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            path,
            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ):
            return True
    except FileNotFoundError:
        return False


def _read_registration() -> tuple[str | None, str | None] | None:
    winreg = _registry_module()
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            INPROC_KEY,
            access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            try:
                path, path_type = winreg.QueryValueEx(key, "")
            except FileNotFoundError:
                path, path_type = None, None
            try:
                threading_model, model_type = winreg.QueryValueEx(key, "ThreadingModel")
            except FileNotFoundError:
                threading_model, model_type = None, None
    except FileNotFoundError:
        return (None, None) if _key_exists(CLSID_KEY) else None
    if path is not None and path_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
        raise VirtualCameraRegistrationError("The registered source path has an invalid type")
    if threading_model is not None and model_type != winreg.REG_SZ:
        raise VirtualCameraRegistrationError("The registered threading model has an invalid type")
    return (
        os.path.expandvars(path) if path is not None else None,
        threading_model,
    )


def _delete_registration_keys() -> None:
    winreg = _registry_module()
    for key_path in (INPROC_KEY, CLSID_KEY):
        try:
            winreg.DeleteKeyEx(
                winreg.HKEY_LOCAL_MACHINE,
                key_path,
                access=winreg.KEY_WOW64_64KEY,
            )
        except FileNotFoundError:
            continue


def _write_registration(path: str, threading_model: str) -> None:
    winreg = _registry_module()
    access = winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, CLSID_KEY, access=access) as clsid_key:
        winreg.SetValueEx(clsid_key, "", 0, winreg.REG_SZ, DISPLAY_NAME)
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, INPROC_KEY, access=access) as inproc_key:
        winreg.SetValueEx(inproc_key, "", 0, winreg.REG_SZ, path)
        winreg.SetValueEx(inproc_key, "ThreadingModel", 0, winreg.REG_SZ, threading_model)


def register_source(dll: Path, *, force: bool) -> Path:
    source = dll.resolve()
    if not source.is_file():
        raise VirtualCameraRegistrationError(f"Camera source DLL is missing: {source}")
    current = _read_registration()
    if (
        current is not None
        and (current[0] is None or not _is_managed_path(current[0]))
        and not force
    ):
        raise VirtualCameraRegistrationError(
            "Another or incomplete camera source registration exists; pass --force to replace it"
        )
    staged = _stage_source(source)
    try:
        _write_registration(str(staged), "Both")
    except OSError as error:
        try:
            if current is not None and current[0] is not None and current[1] is not None:
                _write_registration(current[0], current[1])
            else:
                _delete_registration_keys()
        except OSError:
            pass
        if current is None or current[0] is None or Path(current[0]) != staged:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        if isinstance(error, PermissionError):
            raise VirtualCameraRegistrationError(
                "Machine registration requires an elevated terminal"
            ) from error
        raise
    _cleanup_staged_sources(keep=frozenset({staged}))
    return staged


def _remove_persistent_camera(source: Path) -> None:
    registrar = _system_executable("System32", "regsvr32.exe")
    try:
        removal = subprocess.run(
            [str(registrar), "/s", "/u", str(source)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VirtualCameraRegistrationError(
            "Could not run the native virtual-camera cleanup"
        ) from error
    if removal.returncode != 0:
        raise VirtualCameraRegistrationError(
            "The COM source could not remove the persistent virtual camera"
        )


def _restart_frame_server() -> None:
    powershell = _system_executable(
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "$service = Get-Service -Name 'FrameServer'; "
        "Restart-Service -InputObject $service -Force; "
        "$service.WaitForStatus("
        "[System.ServiceProcess.ServiceControllerStatus]::Running, "
        "[TimeSpan]::FromSeconds(20))"
    )
    try:
        restart = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=25,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VirtualCameraRegistrationError(
            "The camera source was registered, but Frame Server could not be restarted"
        ) from error
    if restart.returncode != 0:
        detail = " ".join((restart.stderr or restart.stdout).splitlines())[:500]
        suffix = f": {detail}" if detail else ""
        raise VirtualCameraRegistrationError(
            f"The camera source was registered, but Frame Server could not be restarted{suffix}"
        )


def unregister_source(dll: Path, *, force: bool) -> bool:
    current = _read_registration()
    if current is None:
        _cleanup_staged_sources()
        return False
    if current[0] is None and not force:
        raise VirtualCameraRegistrationError(
            "The source registration is incomplete; pass --force to remove it"
        )
    if current[0] is not None and not _is_managed_path(current[0]) and not force:
        raise VirtualCameraRegistrationError(
            "The active registration points at another DLL; pass --force to remove it"
        )
    _remove_persistent_camera(dll.resolve())
    try:
        _delete_registration_keys()
    except PermissionError as error:
        raise VirtualCameraRegistrationError(
            "Machine registration requires an elevated terminal"
        ) from error
    _cleanup_staged_sources()
    return True


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    dll = options.dll or _default_dll(options.configuration)
    try:
        if options.restart_frame_server and options.action != "register":
            raise VirtualCameraRegistrationError(
                "--restart-frame-server is valid only with the register action"
            )
        if options.action == "status":
            current = _read_registration()
            if current is None:
                print("Solin virtual-camera source is not registered")
                return 1
            print(f"Registered source: {current[0] or '<missing>'}")
            print(f"Threading model: {current[1] or '<missing>'}")
            return (
                0
                if current[0] is not None
                and _is_managed_path(current[0])
                and Path(current[0]).is_file()
                and current[1] == "Both"
                else 2
            )
        if options.action == "register":
            staged = register_source(dll, force=options.force)
            print(f"Registered Solin virtual-camera source: {staged}")
            if options.restart_frame_server:
                _restart_frame_server()
                print("Restarted Windows Frame Server")
            return 0
        removed = unregister_source(dll, force=options.force)
        print(
            "Unregistered Solin virtual-camera source"
            if removed
            else "Solin virtual-camera source was not registered"
        )
        return 0
    except (OSError, VirtualCameraRegistrationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
