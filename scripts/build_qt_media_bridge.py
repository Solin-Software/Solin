from __future__ import annotations

import argparse
import importlib.metadata
import platform
import shutil
import struct
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = REPOSITORY_ROOT / "native" / "qt_media_bridge"
DEFAULT_BUILD_DIRECTORY = REPOSITORY_ROOT / "build" / "native" / "qt-media-bridge"
DEFAULT_SDK_ROOT = (
    REPOSITORY_ROOT / "build" / "dependencies" / "qt-bridge-sdk" / "6.11.1" / "msvc2022_64"
)
QT_BINDING_VERSION = "6.11.1"
PE_MACHINE_X64 = 0x8664


class QtMediaBridgeBuildError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure and build the pinned Qt/D3D11 media bridge."
    )
    parser.add_argument(
        "--configuration",
        choices=("Debug", "RelWithDebInfo", "Release"),
        default="Release",
    )
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIRECTORY)
    parser.add_argument("--sdk-root", type=Path, default=DEFAULT_SDK_ROOT)
    parser.add_argument(
        "--skip-sdk-bootstrap",
        action="store_true",
        help="Fail instead of installing the pinned Qt SDK when it is absent.",
    )
    parser.add_argument("--jobs", type=int, default=0)
    return parser


def _require_command(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise QtMediaBridgeBuildError(f"Required command is unavailable: {name}")
    return executable


def _run(command: Sequence[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, check=False)
    if result.returncode != 0:
        raise QtMediaBridgeBuildError(
            f"Command failed with exit code {result.returncode}: {command[0]}"
        )


def _complete_sdk(root: Path) -> bool:
    return all(
        path.is_file()
        for path in (
            root / "include" / "QtCore" / "qglobal.h",
            root / "include" / "QtGui" / QT_BINDING_VERSION / "QtGui" / "private" / "qrhi_p.h",
            root
            / "include"
            / "QtMultimedia"
            / QT_BINDING_VERSION
            / "QtMultimedia"
            / "private"
            / "qvideoframe_p.h",
            root / "lib" / "Qt6Core.lib",
            root / "lib" / "Qt6Gui.lib",
            root / "lib" / "Qt6Multimedia.lib",
        )
    )


def _bootstrap_sdk(root: Path) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise QtMediaBridgeBuildError("PowerShell is required to install the pinned Qt SDK")
    _run(
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPOSITORY_ROOT / "scripts" / "install_qt_bridge_sdk_windows.ps1"),
            "-InstallDirectory",
            str(root),
        ]
    )


def _binding_package_root() -> Path:
    for distribution in ("PySide6", "shiboken6"):
        installed = importlib.metadata.version(distribution)
        if installed != QT_BINDING_VERSION:
            raise QtMediaBridgeBuildError(
                f"{distribution} {QT_BINDING_VERSION} is required; found {installed}"
            )
    try:
        import shiboken6
    except ImportError as error:
        raise QtMediaBridgeBuildError("The matching shiboken6 package is unavailable") from error
    package_root = Path(shiboken6.__file__).resolve().parent.parent
    config = package_root / "shiboken6" / "lib" / "cmake" / "Shiboken6"
    if not config.is_dir():
        raise QtMediaBridgeBuildError(
            f"The shiboken6 development package is incomplete: {package_root}"
        )
    return package_root


def _pe_machine(path: Path) -> int:
    try:
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
    except OSError:
        return 0


def build_qt_media_bridge(
    *,
    configuration: str,
    build_directory: Path,
    sdk_root: Path,
    bootstrap_sdk: bool,
    jobs: int,
) -> Path:
    if sys.platform != "win32" or platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise QtMediaBridgeBuildError("The Qt media bridge currently requires Windows x64")
    if jobs < 0:
        raise QtMediaBridgeBuildError("--jobs must be greater than or equal to zero")

    cmake = _require_command("cmake")
    build_directory = build_directory.resolve()
    sdk_root = sdk_root.resolve()
    if not _complete_sdk(sdk_root):
        if not bootstrap_sdk:
            raise QtMediaBridgeBuildError(f"Pinned Qt bridge SDK is incomplete: {sdk_root}")
        _bootstrap_sdk(sdk_root)
    if not _complete_sdk(sdk_root):
        raise QtMediaBridgeBuildError(f"Pinned Qt bridge SDK is incomplete: {sdk_root}")

    package_root = _binding_package_root()
    _run(
        [
            cmake,
            "-S",
            str(SOURCE_DIRECTORY),
            "-B",
            str(build_directory),
            "-A",
            "x64",
            f"-DPython3_EXECUTABLE={sys.executable}",
            f"-DSOLIN_QT_BRIDGE_SDK_ROOT={sdk_root}",
            f"-DSOLIN_SHIBOKEN_PACKAGE_ROOT={package_root}",
        ]
    )
    build_command = [
        cmake,
        "--build",
        str(build_directory),
        "--config",
        configuration,
        "--parallel",
    ]
    if jobs > 0:
        build_command.append(str(jobs))
    _run(build_command)

    candidates = tuple(
        path
        for path in (build_directory / configuration).glob("solin_qt_media_bridge*.pyd")
        if path.is_file()
    )
    if len(candidates) != 1:
        raise QtMediaBridgeBuildError(
            f"Expected one Qt media bridge extension, found {len(candidates)}"
        )
    extension = candidates[0]
    if _pe_machine(extension) != PE_MACHINE_X64:
        raise QtMediaBridgeBuildError(f"Qt media bridge has the wrong PE architecture: {extension}")
    return extension


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        extension = build_qt_media_bridge(
            configuration=options.configuration,
            build_directory=options.build_dir,
            sdk_root=options.sdk_root,
            bootstrap_sdk=not options.skip_sdk_bootstrap,
            jobs=options.jobs,
        )
    except (QtMediaBridgeBuildError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Qt media bridge ready: {extension}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
