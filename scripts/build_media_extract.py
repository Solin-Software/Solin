"""Build and install the extraction helper against the installed OBS FFmpeg ABI."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = REPOSITORY_ROOT / "native" / "media_extract"
DEFAULT_BUILD_DIRECTORY = REPOSITORY_ROOT / "build" / "native" / "media-extract"
DEFAULT_CACHE_DIRECTORY = REPOSITORY_ROOT / "build" / "dependencies" / "media-extract"
SDK_VERSION = "2025-08-23"
COMPONENTS = ("avformat", "avcodec", "avutil", "swscale")
PLATFORMS = {"win32": "windows", "linux": "linux", "darwin": "macos"}
ARCHITECTURES = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}


@dataclass(frozen=True)
class SdkSpec:
    filename: str
    sha256: str


SDK_SPECS = {
    ("windows", "x86_64"): SdkSpec(
        f"windows-deps-{SDK_VERSION}-x64.zip",
        "8de229cff6f1981508c0eb646b35e644633a5855787b9f5d3b90ae2aeb87ffc1",
    ),
    ("macos", "x86_64"): SdkSpec(
        f"macos-deps-{SDK_VERSION}-x86_64.tar.xz",
        "a84e850ff7773ab45e3ce0f25bb34799f4df46f3bfce28c8341030a4e2a48505",
    ),
    ("macos", "arm64"): SdkSpec(
        f"macos-deps-{SDK_VERSION}-arm64.tar.xz",
        "09cbeb3cbf4fbb9e58e4cf4ca030a724fb1be41d2d7851c8b356f476e04544db",
    ),
}


class MediaExtractBuildError(RuntimeError):
    """The helper cannot be built or qualified against the selected runtime."""


def target() -> tuple[str, str]:
    selected = PLATFORMS.get(sys.platform), ARCHITECTURES.get(platform.machine().casefold())
    if selected not in {*SDK_SPECS, ("linux", "x86_64")}:
        raise MediaExtractBuildError(f"Unsupported extraction target: {selected}")
    return selected  # type: ignore[return-value]


def installed_runtime(target_platform: str, architecture: str) -> Path:
    spec = importlib.util.find_spec("pylibobs")
    if spec is None or spec.origin is None:
        raise MediaExtractBuildError("Install pylibobs before building the media extraction helper")
    root = Path(spec.origin).resolve().parent / "_libs" / target_platform / architecture
    if not root.is_dir():
        raise MediaExtractBuildError(f"Installed pylibobs runtime is missing: {root}")
    return root


def _download_sdk(spec: SdkSpec, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / spec.filename
    if archive.is_file():
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == spec.sha256:
                return archive
        raise MediaExtractBuildError(f"Cached OBS SDK failed SHA-256 verification: {archive}")
    url = f"https://github.com/obsproject/obs-deps/releases/download/{SDK_VERSION}/{spec.filename}"
    download = tempfile.NamedTemporaryFile(dir=cache, delete=False)
    temporary = Path(download.name)
    try:
        with download as stream:
            with urllib.request.urlopen(url, timeout=120) as response:
                shutil.copyfileobj(response, stream)
        with temporary.open("rb") as reader:
            digest = hashlib.file_digest(reader, "sha256").hexdigest()
        if digest != spec.sha256:
            raise MediaExtractBuildError(f"Downloaded OBS SDK failed SHA-256 verification: {url}")
        temporary.replace(archive)
    finally:
        download.close()
        temporary.unlink(missing_ok=True)
    return archive


def prepare_sdk(target_platform: str, architecture: str, cache: Path) -> Path:
    """Extract verified build inputs; their native runtimes are never installed."""
    if target_platform == "linux":
        result = subprocess.run(
            ["pkg-config", "--variable=includedir", "libavformat"],
            check=True,
            capture_output=True,
            text=True,
        )
        include = Path(result.stdout.strip())
        destination = cache.resolve() / "sdk-linux-x86_64"
        destination.mkdir(parents=True, exist_ok=True)
        for component in COMPONENTS:
            source = include / f"lib{component}"
            if not source.is_dir():
                raise MediaExtractBuildError(f"Install the matching lib{component}-dev package")
            shutil.copytree(source, destination / "include" / f"lib{component}", dirs_exist_ok=True)
        return destination
    spec = SDK_SPECS[target_platform, architecture]
    archive = _download_sdk(spec, cache)
    destination = cache.resolve() / f"sdk-{target_platform}-{architecture}"
    with tempfile.TemporaryDirectory(prefix=".sdk-", dir=cache) as directory:
        staging = Path(directory) / "sdk"
        staging.mkdir()
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    resolved = (staging / member.filename).resolve()
                    if not resolved.is_relative_to(staging.resolve()):
                        raise MediaExtractBuildError("OBS SDK archive contains an escaping path")
                bundle.extractall(staging)
        else:
            with tarfile.open(archive, "r:xz") as bundle:
                bundle.extractall(staging, filter="data")
        for component in COMPONENTS:
            header = (
                staging
                / "include"
                / f"lib{component}"
                / ("swscale.h" if component == "swscale" else f"{component}.h")
            )
            # libavutil's main installed header is avutil.h.
            library = (
                staging
                / "lib"
                / (f"{component}.lib" if target_platform == "windows" else f"lib{component}.dylib")
            )
            if not header.is_file() or not library.is_file():
                raise MediaExtractBuildError(
                    f"OBS SDK is missing {component} headers or link library"
                )
        if destination.is_symlink() or not destination.is_relative_to(cache.resolve()):
            raise MediaExtractBuildError("OBS SDK cache destination must be a private directory")
        if __package__:
            from .package_libobs_runtime import _publish_runtime
        else:
            from package_libobs_runtime import _publish_runtime
        _publish_runtime(staging, destination)
    return destination


def _run(command: Sequence[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)


def install_helper(
    executable: Path, runtime: Path, target_platform: str, architecture: str
) -> Path:
    """Qualify a sibling candidate and atomically replace only Solin's own executable."""
    if __package__:
        from .package_libobs_runtime import (
            _retry_windows_sharing,
            relocate_media_extract,
            stage_media_extract_dependencies,
            verify_media_extract,
        )
    else:
        from package_libobs_runtime import (
            _retry_windows_sharing,
            relocate_media_extract,
            stage_media_extract_dependencies,
            verify_media_extract,
        )

    name = "solin-media-extract.exe" if target_platform == "windows" else "solin-media-extract"
    destination = runtime / name
    if destination.is_symlink():
        raise MediaExtractBuildError("The installed extraction helper must not be a symbolic link")
    candidate = runtime / f".solin-media-extract-{uuid.uuid4().hex}{executable.suffix}"
    try:
        shutil.copy2(executable, candidate)
        candidate.chmod(candidate.stat().st_mode | 0o111)
        stage_media_extract_dependencies(runtime, target_platform)
        relocate_media_extract(runtime, candidate, target_platform, architecture)
        verify_media_extract(candidate, target_platform=target_platform)
        _retry_windows_sharing(lambda: os.replace(candidate, destination))
    finally:
        candidate.unlink(missing_ok=True)
    return destination


def build_media_extract(
    *,
    build_directory: Path,
    cache_directory: Path,
    configuration: str,
    jobs: int,
) -> Path:
    target_platform, architecture = target()
    if jobs < 0:
        raise MediaExtractBuildError("--jobs must be greater than or equal to zero")
    build_directory = build_directory.resolve()
    cache_directory = cache_directory.resolve()
    runtime = installed_runtime(target_platform, architecture)
    sdk = prepare_sdk(target_platform, architecture, cache_directory)
    if not (SOURCE_DIRECTORY / "CMakeLists.txt").is_file():
        raise MediaExtractBuildError(f"Extraction helper source is missing: {SOURCE_DIRECTORY}")
    cmake = shutil.which("cmake")
    if cmake is None:
        raise MediaExtractBuildError("CMake and a native C++ toolchain are required")
    configure = [cmake, "-S", str(SOURCE_DIRECTORY), "-B", str(build_directory.resolve())]
    if target_platform == "windows":
        configure.extend(["-A", "x64"])
    else:
        configure.append(f"-DCMAKE_BUILD_TYPE={configuration}")
    configure.append(f"-DSOLIN_FFMPEG_ROOT={sdk}")
    if target_platform == "macos":
        configure.extend(
            [
                f"-DCMAKE_OSX_ARCHITECTURES={architecture}",
                "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0",
                "-DSOLIN_FFMPEG_RUNTIME_SUBDIR=Frameworks",
            ]
        )
    _run(configure)
    command = [cmake, "--build", str(build_directory), "--config", configuration, "--parallel"]
    if jobs:
        command.append(str(jobs))
    _run(command)
    _run(
        [
            str(Path(cmake).with_name("ctest.exe" if target_platform == "windows" else "ctest")),
            "--test-dir",
            str(build_directory),
            "--build-config",
            configuration,
            "--output-on-failure",
        ]
    )
    name = "solin-media-extract.exe" if target_platform == "windows" else "solin-media-extract"
    candidates = [build_directory / configuration / name, build_directory / name]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise MediaExtractBuildError("CMake did not produce the extraction helper")
    return install_helper(executable, runtime, target_platform, architecture)


def main(arguments: Sequence[str] | None = None) -> int:
    if __package__:
        from .package_libobs_runtime import LibobsPackagingError
    else:
        from package_libobs_runtime import LibobsPackagingError
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIRECTORY)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIRECTORY)
    parser.add_argument(
        "--configuration", choices=("Release", "RelWithDebInfo", "Debug"), default="Release"
    )
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument(
        "--prepare-sdk", action="store_true", help="Download verified SDK inputs without building"
    )
    options = parser.parse_args(arguments)
    try:
        if options.prepare_sdk:
            target_platform, architecture = target()
            print(
                f"OBS FFmpeg SDK: {prepare_sdk(target_platform, architecture, options.cache_dir)}"
            )
            return 0
        executable = build_media_extract(
            build_directory=options.build_dir,
            cache_directory=options.cache_dir,
            configuration=options.configuration,
            jobs=options.jobs,
        )
    except (
        MediaExtractBuildError,
        LibobsPackagingError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Media extraction helper installed and qualified: {executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
