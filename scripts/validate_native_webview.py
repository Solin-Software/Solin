from __future__ import annotations

import argparse
import ctypes
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from native_webview_widget.abi import (
    ABI_VERSION,
    MACOS_LIBRARY_NAMES,
    WINDOWS_LIBRARY_NAME,
    native_library_candidates,
    required_exports_for_system,
)

PACKAGE_DIR = SRC_ROOT / "native_webview_widget"
MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


def _read_magic(path: Path, size: int = 4) -> bytes:
    return path.read_bytes()[:size]


def validate_python_package(package_dir: Path) -> list[str]:
    errors: list[str] = []
    for filename in ("__init__.py", "_backend.py", "widget.py", "abi.py"):
        if not (package_dir / filename).is_file():
            errors.append(f"Missing Python package file: {package_dir / filename}")
    return errors


def validate_binary_files(package_dir: Path) -> list[str]:
    errors: list[str] = []

    windows_dll = package_dir / WINDOWS_LIBRARY_NAME
    if not windows_dll.is_file():
        errors.append(f"Missing Windows native webview binary: {windows_dll}")
    elif _read_magic(windows_dll, 2) != b"MZ":
        errors.append(f"Windows native webview binary is not a PE file: {windows_dll}")

    macos_candidates = [package_dir / name for name in MACOS_LIBRARY_NAMES]
    if not any(path.is_file() for path in macos_candidates):
        errors.append(
            "Missing macOS native webview binary. Expected one of: "
            + ", ".join(str(path) for path in macos_candidates)
        )
    for candidate in macos_candidates:
        if candidate.is_file() and _read_magic(candidate) not in MACHO_MAGICS:
            errors.append(f"macOS native webview binary is not a Mach-O file: {candidate}")

    return errors


def validate_current_platform_exports(package_dir: Path, system: str | None = None) -> list[str]:
    current_system = system or platform.system()
    candidates = native_library_candidates(current_system, package_dir)
    if not candidates:
        return []

    library_path = next((path for path in candidates if path.is_file()), None)
    if library_path is None:
        return [
            "Missing current-platform native webview binary. Expected one of: "
            + ", ".join(str(path) for path in candidates)
        ]

    try:
        library = ctypes.CDLL(str(library_path))
    except OSError as exc:
        return [f"Could not load current-platform native webview binary {library_path}: {exc}"]

    required_exports = required_exports_for_system(current_system)
    missing_exports = [name for name in required_exports if not hasattr(library, name)]
    if missing_exports:
        return [
            f"{library_path} is missing native webview ABI export(s): "
            + ", ".join(missing_exports)
        ]

    return []


def validate_native_webview_package(
    package_dir: Path = PACKAGE_DIR,
    *,
    check_current_platform_exports: bool = True,
) -> list[str]:
    errors = [
        *validate_python_package(package_dir),
        *validate_binary_files(package_dir),
    ]
    if check_current_platform_exports:
        errors.extend(validate_current_platform_exports(package_dir))
    return errors


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate native_webview_widget package files and ABI exports."
    )
    parser.add_argument("--package-dir", type=Path, default=PACKAGE_DIR)
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="Do not load the current-platform native library to verify exports.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    package_dir = args.package_dir.resolve()
    errors = validate_native_webview_package(
        package_dir,
        check_current_platform_exports=not args.skip_load,
    )

    if errors:
        for error in errors:
            print(error)
        return 1

    print(
        "native_webview_widget ABI "
        f"v{ABI_VERSION} validated with "
        f"{len(required_exports_for_system(platform.system()))} required export(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
