from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from . import _windows_pe
else:
    import _windows_pe

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VIRTUAL_CAMERA_FILTER_X64 = (
    REPOSITORY_ROOT
    / "build"
    / "native"
    / "virtual-camera-x64"
    / "Release"
    / "solin-virtual-camera.dll"
)
DEFAULT_VIRTUAL_CAMERA_FILTER_X86 = (
    REPOSITORY_ROOT
    / "build"
    / "native"
    / "virtual-camera-x86"
    / "Release"
    / "solin-virtual-camera.dll"
)
DEFAULT_APPLICATION_DIRECTORY = REPOSITORY_ROOT / "build" / "main.dist"
LICENSE_FILENAMES = ("directshow-baseclasses.LICENSE", "libyuv.LICENSE")
ALLOWED_FILTER_IMPORTS = {
    "advapi32.dll",
    "bcrypt.dll",
    "gdi32.dll",
    "kernel32.dll",
    "ole32.dll",
    "oleaut32.dll",
    "user32.dll",
}
WINDOWS_RENAME_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4)
PE_MACHINE_X86 = 0x014C
PE_MACHINE_X64 = 0x8664


class VirtualCameraPackagingError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage the x64/x86 DirectShow virtual-camera filters."
    )
    parser.add_argument(
        "--virtual-camera-filter-x64", type=Path, default=DEFAULT_VIRTUAL_CAMERA_FILTER_X64
    )
    parser.add_argument(
        "--virtual-camera-filter-x86", type=Path, default=DEFAULT_VIRTUAL_CAMERA_FILTER_X86
    )
    parser.add_argument("--application-dir", type=Path, default=DEFAULT_APPLICATION_DIRECTORY)
    parser.add_argument("--skip-verification", action="store_true")
    return parser


def _validate_filter(path: Path, architecture: str) -> None:
    if not path.is_file():
        raise VirtualCameraPackagingError(
            f"Virtual-camera {architecture} filter is missing: {path}"
        )
    expected_machine = PE_MACHINE_X64 if architecture == "x64" else PE_MACHINE_X86
    if _windows_pe.pe_machine(path) != expected_machine:
        raise VirtualCameraPackagingError(
            f"Virtual-camera {architecture} filter has the wrong PE architecture: {path}"
        )
    _validate_filter_imports(path, architecture)


def _validate_filter_imports(path: Path, architecture: str) -> None:
    imports = _windows_pe.pe_imports(path)
    if not imports:
        raise VirtualCameraPackagingError(
            f"The {architecture} virtual-camera filter PE import table could not be audited"
        )
    unexpected = sorted(
        imported for imported in imports if imported.casefold() not in ALLOWED_FILTER_IMPORTS
    )
    if unexpected:
        raise VirtualCameraPackagingError(
            f"The {architecture} virtual-camera filter imports dependencies outside "
            f"the production allowlist: {', '.join(unexpected)}"
        )


def _replace_with_retry(source: Path, destination: Path) -> None:
    for delay in (*WINDOWS_RENAME_RETRY_DELAYS_SECONDS, None):
        try:
            source.replace(destination)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def _remove_backup_with_retry(path: Path) -> None:
    for delay in (*WINDOWS_RENAME_RETRY_DELAYS_SECONDS, None):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            if delay is None:
                print(
                    f"warning: obsolete virtual-camera backup could not be removed: {path}",
                    file=sys.stderr,
                )
                return
            time.sleep(delay)


def _replace_staging_directory(staging: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}-{uuid.uuid4().hex}.backup")
    had_destination = destination.exists()
    if had_destination:
        _replace_with_retry(destination, backup)
    try:
        _replace_with_retry(staging, destination)
    except Exception as replacement_error:  # noqa: BLE001 - transactional rollback boundary
        if had_destination and backup.exists():
            try:
                _replace_with_retry(backup, destination)
            except Exception as rollback_error:  # noqa: BLE001 - rollback failure boundary
                raise VirtualCameraPackagingError(
                    "Virtual-camera replacement failed and the previous payload "
                    "could not be restored"
                ) from rollback_error
        raise replacement_error
    if backup.exists():
        _remove_backup_with_retry(backup)


def _payload_manifest(root: Path) -> dict[str, object]:
    filters = {}
    for architecture in ("x64", "x86"):
        path = root / architecture / "solin-virtual-camera.dll"
        _validate_filter(path, architecture)
        filters[architecture] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "machine": _windows_pe.pe_machine(path),
            "imports": sorted(name.casefold() for name in _windows_pe.pe_imports(path)),
        }
    licenses = {}
    for filename in LICENSE_FILENAMES:
        path = root / "share" / "licenses" / "solin-virtual-camera" / filename
        licenses[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"schema_version": 1, "filters": filters, "licenses": licenses}


def _verify_staged_camera(root: Path) -> None:
    manifest = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
    if manifest != _payload_manifest(root):
        raise VirtualCameraPackagingError(
            "Staged virtual-camera manifest does not match the payload"
        )
    expected = {
        "runtime-manifest.json",
        "x64/solin-virtual-camera.dll",
        "x86/solin-virtual-camera.dll",
        *(f"share/licenses/solin-virtual-camera/{name}" for name in LICENSE_FILENAMES),
    }
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != expected:
        raise VirtualCameraPackagingError("Staged virtual-camera payload contains unexpected files")


def package_virtual_camera(
    *,
    virtual_camera_filter_x64: Path,
    virtual_camera_filter_x86: Path,
    application_dir: Path,
    verify: bool,
) -> Path:
    if sys.platform != "win32":
        raise VirtualCameraPackagingError("Windows packaging must run on Windows")
    application_dir = application_dir.resolve()
    if not application_dir.is_dir() or not (application_dir / "Solin.exe").is_file():
        raise VirtualCameraPackagingError(
            f"Nuitka application directory is incomplete: {application_dir}"
        )
    filters = {
        "x64": virtual_camera_filter_x64.resolve(),
        "x86": virtual_camera_filter_x86.resolve(),
    }
    for architecture, path in filters.items():
        _validate_filter(path, architecture)
    native_directory = application_dir / "native"
    native_directory.mkdir(parents=True, exist_ok=True)
    destination = native_directory / "virtual-camera"
    staging = Path(tempfile.mkdtemp(prefix="virtual-camera-staging-", dir=native_directory))
    try:
        for architecture, path in filters.items():
            (staging / architecture).mkdir()
            shutil.copy2(path, staging / architecture / "solin-virtual-camera.dll")
        license_directory = staging / "share" / "licenses" / "solin-virtual-camera"
        license_directory.mkdir(parents=True)
        for filename in LICENSE_FILENAMES:
            shutil.copy2(
                REPOSITORY_ROOT / "native" / "virtual_camera" / "third_party" / filename,
                license_directory / filename,
            )
        (staging / "runtime-manifest.json").write_text(
            json.dumps(_payload_manifest(staging), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if verify:
            _verify_staged_camera(staging)
        _replace_staging_directory(staging, destination)
    except Exception:  # noqa: BLE001 - incomplete staging cleanup boundary
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        destination = package_virtual_camera(
            virtual_camera_filter_x64=options.virtual_camera_filter_x64,
            virtual_camera_filter_x86=options.virtual_camera_filter_x86,
            application_dir=options.application_dir,
            verify=not options.skip_verification,
        )
    except (VirtualCameraPackagingError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Packaged virtual camera: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
