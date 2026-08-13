from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - the tool is intentionally Windows-only
    winreg = None  # type: ignore[assignment]


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FILTER_CLSID = "{08AFA2E5-0293-4E56-9FE1-2A79DAE8E28F}"
VIDEO_INPUT_CATEGORY = "{860BB310-5D01-11D0-BD3B-00A0C911CE86}"
FRIENDLY_NAME = "Solin Virtual Camera"
FILTER_FILENAME = "solin-virtual-camera.dll"
DEFAULT_FILTER_X64 = (
    REPOSITORY_ROOT
    / "build"
    / "native"
    / "media-engine-gstreamer"
    / "Release"
    / FILTER_FILENAME
)
DEFAULT_FILTER_X86 = (
    REPOSITORY_ROOT
    / "build"
    / "native"
    / "directshow-filter-x86"
    / "Release"
    / FILTER_FILENAME
)
PE_MACHINE_X86 = 0x014C
PE_MACHINE_X64 = 0x8664


class VirtualCameraRegistrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RegistrationState:
    architecture: str
    registered: bool
    valid: bool
    dll_path: str = ""
    error: str = ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install, remove, or inspect the per-user x86/x64 DirectShow "
            "virtual-camera filters."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--x64", type=Path, default=DEFAULT_FILTER_X64)
    install.add_argument("--x86", type=Path, default=DEFAULT_FILTER_X86)
    subparsers.add_parser("uninstall")
    subparsers.add_parser("status")
    return parser


def _require_windows() -> None:
    if sys.platform != "win32" or winreg is None:
        raise VirtualCameraRegistrationError(
            "DirectShow virtual-camera registration requires Windows x64"
        )
    if os.environ.get("PROCESSOR_ARCHITECTURE", "").casefold() not in {
        "amd64",
        "x86_64",
    } and os.environ.get("PROCESSOR_ARCHITEW6432", "").casefold() not in {
        "amd64",
        "x86_64",
    }:
        raise VirtualCameraRegistrationError(
            "DirectShow x86/x64 filters require Windows x64"
        )


def _managed_staging_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise VirtualCameraRegistrationError("LOCALAPPDATA is unavailable")
    return Path(local_app_data) / "Solin" / "VirtualCamera" / "versions"


def _pe_machine(path: Path) -> int:
    try:
        with path.open("rb") as binary:
            if binary.read(2) != b"MZ":
                return 0
            binary.seek(0x3C)
            pe_offset_bytes = binary.read(4)
            if len(pe_offset_bytes) != 4:
                return 0
            pe_offset = struct.unpack("<I", pe_offset_bytes)[0]
            binary.seek(pe_offset)
            if binary.read(4) != b"PE\0\0":
                return 0
            machine = binary.read(2)
            return struct.unpack("<H", machine)[0] if len(machine) == 2 else 0
    except OSError:
        return 0


def _validate_filter(path: Path, expected_machine: int, architecture: str) -> Path:
    resolved = path.resolve(strict=True)
    if resolved.name.casefold() != FILTER_FILENAME:
        raise VirtualCameraRegistrationError(
            f"Unexpected {architecture} filter filename: {resolved.name}"
        )
    if _pe_machine(resolved) != expected_machine:
        raise VirtualCameraRegistrationError(
            f"The {architecture} filter has the wrong PE architecture: {resolved}"
        )
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stage_filters(x64: Path, x86: Path) -> tuple[Path, Path]:
    root = _managed_staging_root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".staging-{uuid.uuid4().hex}"
    temporary.mkdir()
    try:
        staged_x64 = temporary / "x64" / FILTER_FILENAME
        staged_x86 = temporary / "x86" / FILTER_FILENAME
        staged_x64.parent.mkdir()
        staged_x86.parent.mkdir()
        shutil.copy2(x64, staged_x64)
        shutil.copy2(x86, staged_x86)
        x64_digest = _sha256(staged_x64)
        x86_digest = _sha256(staged_x86)
        version = hashlib.sha256(
            f"{x64_digest}:{x86_digest}".encode("ascii")
        ).hexdigest()[:20]
        destination = root / version
        if destination.exists():
            if (
                _sha256(destination / "x64" / FILTER_FILENAME) != x64_digest
                or _sha256(destination / "x86" / FILTER_FILENAME) != x86_digest
            ):
                raise VirtualCameraRegistrationError(
                    "A managed virtual-camera version has inconsistent contents"
                )
            shutil.rmtree(temporary)
        else:
            temporary.replace(destination)
        return (
            destination / "x64" / FILTER_FILENAME,
            destination / "x86" / FILTER_FILENAME,
        )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _registry_view(architecture: str) -> int:
    assert winreg is not None
    return winreg.KEY_WOW64_64KEY if architecture == "x64" else winreg.KEY_WOW64_32KEY


def _query_value(path: str, name: str, architecture: str):
    assert winreg is not None
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        path,
        0,
        winreg.KEY_READ | _registry_view(architecture),
    ) as key:
        return winreg.QueryValueEx(key, name)[0]


def _registration_state(architecture: str) -> RegistrationState:
    class_path = f"Software\\Classes\\CLSID\\{FILTER_CLSID}\\InprocServer32"
    category_path = (
        "Software\\Classes\\CLSID\\"
        f"{VIDEO_INPUT_CATEGORY}\\Instance\\{FILTER_CLSID}"
    )
    try:
        dll_path = str(_query_value(class_path, "", architecture))
    except OSError:
        return RegistrationState(architecture, False, False, error="missing")
    try:
        threading_model = str(_query_value(class_path, "ThreadingModel", architecture))
        friendly_name = str(_query_value(category_path, "FriendlyName", architecture))
        class_id = str(_query_value(category_path, "CLSID", architecture))
        filter_data = _query_value(category_path, "FilterData", architecture)
        expected_machine = PE_MACHINE_X64 if architecture == "x64" else PE_MACHINE_X86
        valid = (
            threading_model.casefold() == "both"
            and friendly_name == FRIENDLY_NAME
            and class_id.casefold() == FILTER_CLSID.casefold()
            and isinstance(filter_data, bytes)
            and bool(filter_data)
            and Path(dll_path).is_absolute()
            and _pe_machine(Path(dll_path)) == expected_machine
        )
        return RegistrationState(
            architecture,
            True,
            valid,
            dll_path=dll_path,
            error="" if valid else "invalid",
        )
    except (OSError, ValueError):
        return RegistrationState(
            architecture, True, False, dll_path=dll_path, error="invalid"
        )


def status() -> tuple[RegistrationState, RegistrationState]:
    _require_windows()
    return _registration_state("x64"), _registration_state("x86")


def _regsvr32(architecture: str) -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    directory = "System32" if architecture == "x64" else "SysWOW64"
    executable = system_root / directory / "regsvr32.exe"
    if not executable.is_file():
        raise VirtualCameraRegistrationError(f"regsvr32 is unavailable: {executable}")
    return executable


def _run_registration(architecture: str, path: Path, *, unregister: bool) -> None:
    command = [str(_regsvr32(architecture)), "/s"]
    if unregister:
        command.append("/u")
    command.append(str(path))
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        operation = "unregistration" if unregister else "registration"
        raise VirtualCameraRegistrationError(
            f"{architecture} DirectShow {operation} failed with exit code "
            f"{result.returncode}"
        )


def _restore_registration(architecture: str, previous: RegistrationState) -> None:
    if previous.registered and previous.dll_path and Path(previous.dll_path).is_file():
        _run_registration(architecture, Path(previous.dll_path), unregister=False)
    else:
        _delete_registration_keys(architecture)


def install_filters(x64: Path, x86: Path) -> tuple[Path, Path]:
    _require_windows()
    source_x64 = _validate_filter(x64, PE_MACHINE_X64, "x64")
    source_x86 = _validate_filter(x86, PE_MACHINE_X86, "x86")
    previous_x64, previous_x86 = status()
    staged_x64, staged_x86 = _stage_filters(source_x64, source_x86)
    try:
        _run_registration("x64", staged_x64, unregister=False)
        if not _registration_state("x64").valid:
            raise VirtualCameraRegistrationError("x64 registration verification failed")
        _run_registration("x86", staged_x86, unregister=False)
        current_x64, current_x86 = status()
        if not current_x64.valid or not current_x86.valid:
            raise VirtualCameraRegistrationError(
                "dual-architecture registration verification failed"
            )
    except Exception as registration_error:
        rollback_errors: list[str] = []
        for architecture, previous in (
            ("x86", previous_x86),
            ("x64", previous_x64),
        ):
            try:
                _restore_registration(architecture, previous)
            except Exception as error:  # noqa: BLE001 - transactional rollback boundary
                rollback_errors.append(f"{architecture}: {error}")
        if rollback_errors:
            raise VirtualCameraRegistrationError(
                "DirectShow registration failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from registration_error
        raise
    _cleanup_staged_versions()
    return staged_x64, staged_x86


def _delete_tree(path: str, architecture: str) -> None:
    assert winreg is not None
    view = _registry_view(architecture)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | view
        ) as key:
            children: list[str] = []
            index = 0
            while True:
                try:
                    children.append(winreg.EnumKey(key, index))
                    index += 1
                except OSError:
                    break
        for child in children:
            _delete_tree(f"{path}\\{child}", architecture)
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, path, view, 0)
    except FileNotFoundError:
        return


def _delete_registration_keys(architecture: str) -> None:
    _delete_tree(
        "Software\\Classes\\CLSID\\"
        f"{VIDEO_INPUT_CATEGORY}\\Instance\\{FILTER_CLSID}",
        architecture,
    )
    _delete_tree(
        f"Software\\Classes\\CLSID\\{FILTER_CLSID}", architecture
    )


def uninstall_filters() -> None:
    _require_windows()
    states = status()
    errors: list[str] = []
    for state in reversed(states):
        path = Path(state.dll_path) if state.dll_path else None
        try:
            if path is not None and path.is_file():
                _run_registration(state.architecture, path, unregister=True)
        except Exception as error:  # noqa: BLE001 - best-effort dual cleanup
            errors.append(f"{state.architecture} COM: {error}")
        try:
            _delete_registration_keys(state.architecture)
        except Exception as error:  # noqa: BLE001 - best-effort dual cleanup
            errors.append(f"{state.architecture} registry: {error}")
    _cleanup_staged_versions()
    if errors:
        raise VirtualCameraRegistrationError(
            "DirectShow unregistration was incomplete: " + "; ".join(errors)
        )


def _cleanup_staged_versions() -> None:
    root = _managed_staging_root()
    if not root.is_dir():
        return
    active = {
        str(Path(state.dll_path).resolve()).casefold()
        for state in (_registration_state("x64"), _registration_state("x86"))
        if state.dll_path
    }
    for candidate in root.iterdir():
        if not candidate.is_dir():
            continue
        if candidate.name.startswith(".staging-"):
            try:
                shutil.rmtree(candidate)
            except OSError:
                pass
            continue
        candidate_paths = {
            str((candidate / architecture / FILTER_FILENAME).resolve()).casefold()
            for architecture in ("x64", "x86")
        }
        if candidate_paths & active:
            continue
        try:
            shutil.rmtree(candidate)
        except OSError:
            # An older DLL may still be loaded by a consumer. It remains immutable
            # and will be retried by the next install/uninstall operation.
            continue
    try:
        root.rmdir()
    except OSError:
        pass


def main(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        if options.command == "install":
            installed_x64, installed_x86 = install_filters(options.x64, options.x86)
            print(f"x64: {installed_x64}")
            print(f"x86: {installed_x86}")
        elif options.command == "uninstall":
            uninstall_filters()
            print("Solin DirectShow virtual camera removed for the current user.")
        else:
            states = status()
            print(json.dumps([asdict(state) for state in states], indent=2))
            return 0 if all(state.valid for state in states) else 1
    except (OSError, subprocess.SubprocessError, VirtualCameraRegistrationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
