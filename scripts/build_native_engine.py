from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = REPOSITORY_ROOT / "native" / "media_engine"
DEFAULT_BUILD_DIRECTORY = (
    REPOSITORY_ROOT / "build" / "native" / "media-engine-gstreamer"
)
DEFAULT_GSTREAMER_ROOT = (
    REPOSITORY_ROOT / "build" / "dependencies" / "gstreamer" / "msvc_x86_64"
)


class NativeEngineBuildError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure, build, and test the Solin native media engine."
    )
    parser.add_argument(
        "--configuration",
        choices=("Debug", "RelWithDebInfo", "Release"),
        default="Release",
    )
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIRECTORY)
    parser.add_argument("--gstreamer-root", type=Path, default=DEFAULT_GSTREAMER_ROOT)
    parser.add_argument(
        "--skip-gstreamer-bootstrap",
        action="store_true",
        help="Fail instead of installing the pinned development runtime when it is absent.",
    )
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--jobs", type=int, default=0)
    return parser


def _require_command(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise NativeEngineBuildError(f"Required command is unavailable: {name}")
    return executable


def _complete_gstreamer_development_root(root: Path) -> bool:
    return all(
        path.is_file()
        for path in (
            root / "bin" / "gst-launch-1.0.exe",
            root / "bin" / "pkg-config.exe",
            root / "include" / "gstreamer-1.0" / "gst" / "gst.h",
            root / "lib" / "pkgconfig" / "gstreamer-1.0.pc",
        )
    )


def _run(command: Sequence[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, check=False)
    if result.returncode != 0:
        raise NativeEngineBuildError(
            f"Command failed with exit code {result.returncode}: {command[0]}"
        )


def _bootstrap_gstreamer(root: Path) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise NativeEngineBuildError("PowerShell is required to install pinned GStreamer")
    _run(
        [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPOSITORY_ROOT / "scripts" / "install_gstreamer_windows.ps1"),
            "-InstallType",
            "devel",
            "-InstallDirectory",
            str(root),
        ]
    )


def build_native_engine(
    *,
    configuration: str,
    build_directory: Path,
    gstreamer_root: Path,
    bootstrap_gstreamer: bool,
    run_tests: bool,
    jobs: int,
) -> Path:
    if sys.platform != "win32" or platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise NativeEngineBuildError(
            "The current native scene renderer build requires Windows x64"
        )
    if jobs < 0:
        raise NativeEngineBuildError("--jobs must be greater than or equal to zero")

    cmake = _require_command("cmake")
    build_directory = build_directory.resolve()
    gstreamer_root = gstreamer_root.resolve()
    if not _complete_gstreamer_development_root(gstreamer_root):
        if not bootstrap_gstreamer:
            raise NativeEngineBuildError(
                f"Pinned GStreamer development runtime is incomplete: {gstreamer_root}"
            )
        _bootstrap_gstreamer(gstreamer_root)
    if not _complete_gstreamer_development_root(gstreamer_root):
        raise NativeEngineBuildError(
            f"Pinned GStreamer development runtime is incomplete: {gstreamer_root}"
        )

    _run(
        [
            cmake,
            "-S",
            str(SOURCE_DIRECTORY),
            "-B",
            str(build_directory),
            "-G",
            "Visual Studio 17 2022",
            "-A",
            "x64",
            "-DSOLIN_MEDIA_ENGINE_ENABLE_GSTREAMER=ON",
            f"-DSOLIN_GSTREAMER_ROOT={gstreamer_root}",
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
    if run_tests:
        ctest = _require_command("ctest")
        _run(
            [
                ctest,
                "--test-dir",
                str(build_directory),
                "--build-config",
                configuration,
                "--output-on-failure",
            ]
        )

    executable = build_directory / configuration / "solin-media-engine.exe"
    if not executable.is_file():
        raise NativeEngineBuildError(f"Native engine executable was not produced: {executable}")
    virtual_camera_source = (
        build_directory / configuration / "solin-virtual-camera-source.dll"
    )
    if not virtual_camera_source.is_file():
        raise NativeEngineBuildError(
            "Native virtual-camera source was not produced: "
            f"{virtual_camera_source}"
        )
    return executable


def main(arguments: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    try:
        executable = build_native_engine(
            configuration=options.configuration,
            build_directory=options.build_dir,
            gstreamer_root=options.gstreamer_root,
            bootstrap_gstreamer=not options.skip_gstreamer_bootstrap,
            run_tests=not options.skip_tests,
            jobs=options.jobs,
        )
    except NativeEngineBuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Native media engine ready: {executable}")
    print(
        "Native virtual-camera source ready: "
        f"{executable.with_name('solin-virtual-camera-source.dll')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
