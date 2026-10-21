from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from . import _windows_pe
else:
    import _windows_pe

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = REPOSITORY_ROOT / "native" / "virtual_camera"
DEFAULT_BUILD_X64_DIRECTORY = REPOSITORY_ROOT / "build" / "native" / "virtual-camera-x64"
DEFAULT_BUILD_X86_DIRECTORY = REPOSITORY_ROOT / "build" / "native" / "virtual-camera-x86"


class VirtualCameraBuildError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and test the x64/x86 DirectShow virtual-camera filters."
    )
    parser.add_argument(
        "--configuration", choices=("Debug", "RelWithDebInfo", "Release"), default="Release"
    )
    parser.add_argument("--build-x64-dir", type=Path, default=DEFAULT_BUILD_X64_DIRECTORY)
    parser.add_argument("--build-x86-dir", type=Path, default=DEFAULT_BUILD_X86_DIRECTORY)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument(
        "--qualify-production", action="store_true",
        help="Also test the production DLL using the current user's broker endpoint.",
    )
    parser.add_argument(
        "--exclude-test-label", help="CTest label regular expression to exclude.",
    )
    parser.add_argument("--jobs", type=int, default=0)
    return parser


def _require_command(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise VirtualCameraBuildError(f"Required command is unavailable: {name}")
    return executable


def _run(command: Sequence[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, check=False)
    if result.returncode != 0:
        raise VirtualCameraBuildError(
            f"Command failed with exit code {result.returncode}: {command[0]}"
        )


def build_virtual_camera(
    *,
    configuration: str,
    build_x64_directory: Path,
    build_x86_directory: Path,
    run_tests: bool,
    jobs: int,
    qualify_production: bool = False,
    exclude_test_label: str | None = None,
) -> tuple[Path, Path]:
    if sys.platform != "win32" or platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise VirtualCameraBuildError("DirectShow virtual-camera builds require Windows x64")
    if jobs < 0:
        raise VirtualCameraBuildError("--jobs must be greater than or equal to zero")
    if not run_tests and (qualify_production or exclude_test_label):
        raise VirtualCameraBuildError("Test qualification options require tests to be enabled")
    directories = (build_x64_directory.resolve(), build_x86_directory.resolve())
    if directories[0] == directories[1]:
        raise VirtualCameraBuildError("x64 and x86 builds require separate directories")
    cmake = _require_command("cmake")
    ctest = _require_command("ctest") if run_tests else None
    filters = []
    for directory, architecture, machine in zip(
        directories, ("x64", "Win32"), (0x8664, 0x014C), strict=True
    ):
        _run(
            [
                cmake,
                "-S",
                str(SOURCE_DIRECTORY),
                "-B",
                str(directory),
                "-A",
                architecture,
                f"-DSOLIN_VIRTUAL_CAMERA_BUILD_TESTS={'ON' if run_tests else 'OFF'}",
                f"-DSOLIN_VIRTUAL_CAMERA_QUALIFY_PRODUCTION={'ON' if qualify_production else 'OFF'}",
            ]
        )
        command = [cmake, "--build", str(directory), "--config", configuration, "--parallel"]
        if jobs:
            command.append(str(jobs))
        _run(command)
        if ctest is not None:
            _run(
                [
                    ctest,
                    "--test-dir",
                    str(directory),
                    "--build-config",
                    configuration,
                    "--output-on-failure",
                    *(["--label-exclude", exclude_test_label] if exclude_test_label else []),
                ]
            )
        dll = directory / configuration / "solin-virtual-camera.dll"
        if not dll.is_file() or _windows_pe.pe_machine(dll) != machine:
            raise VirtualCameraBuildError(
                f"Virtual-camera {architecture} filter is missing or has the wrong architecture: {dll}"
            )
        filters.append(dll)
    return filters[0], filters[1]


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        x64, x86 = build_virtual_camera(
            configuration=options.configuration,
            build_x64_directory=options.build_x64_dir,
            build_x86_directory=options.build_x86_dir,
            run_tests=not options.skip_tests,
            jobs=options.jobs,
            qualify_production=options.qualify_production,
            exclude_test_label=options.exclude_test_label,
        )
    except (VirtualCameraBuildError, OSError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"DirectShow filters ready: x64={x64}; x86={x86}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
