"""Build the pinned OBS playback module against the installed pylibobs runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

if __package__:
    from .build_media_extract import (
        SDK_SPECS,
        _download_sdk,
        installed_runtime,
        prepare_sdk,
        target,
    )
    from .package_libobs_runtime import _publish_runtime, _relocate_macos, _retry_windows_sharing
else:
    from build_media_extract import SDK_SPECS, _download_sdk, installed_runtime, prepare_sdk, target
    from package_libobs_runtime import _publish_runtime, _relocate_macos, _retry_windows_sharing

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = ROOT / "native" / "media_playback"
OBS_VERSION = "32.1.2"
SOURCE_URL = f"https://codeload.github.com/obsproject/obs-studio/tar.gz/refs/tags/{OBS_VERSION}"
SOURCE_SHA256 = "b4a59410cddb46d0e31df1ee13b8ec66f30862d7e980c1a8c4e3b5d16fae6053"
MODULE_NAME = "solin-media-source"
SOURCE_ID = "solin_media_source"
MANIFEST_NAME = "media-playback-manifest.json"
SUFFIXES = {"windows": ".dll", "linux": ".so", "macos": ".dylib"}
_PLAYBACK = "shared/media-playback/media-playback"


class MediaPlaybackBuildError(RuntimeError):
    """Pinned sources, toolchain, or installed playback module failed validation."""


def patch_series() -> list[dict]:
    """Read the ordered downstream changes from their single manifest."""
    manifest = SOURCE_DIRECTORY / "patches" / "series.json"
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MediaPlaybackBuildError("Invalid playback patch series") from error
    if not isinstance(entries, list) or not entries:
        raise MediaPlaybackBuildError("Playback patch series must not be empty")
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key]
            for key in ("file", "purpose", "upstream")
        ):
            raise MediaPlaybackBuildError("Invalid playback patch description")
        name = entry["file"]
        if (
            PurePosixPath(name).name != name or "\\" in name
            or not name.endswith(".patch") or name in names
        ):
            raise MediaPlaybackBuildError("Invalid or duplicate playback patch path")
        prerequisites = entry.get("requires", [])
        if (
            not isinstance(prerequisites, list)
            or any(not isinstance(required, str) or required not in names for required in prerequisites)
        ):
            raise MediaPlaybackBuildError(f"Playback patch prerequisites are out of order: {name}")
        names.add(name)
    return entries


def apply_patches(sources: Path) -> None:
    """Apply explicit unified diffs, rejecting any upstream context mismatch."""
    git = shutil.which("git")
    if git is None:
        raise MediaPlaybackBuildError("Git is required to apply the audited playback patches")
    # A source cache lives inside the Solin checkout. Git-format diffs otherwise
    # inherit that repository's prefix and may silently skip every target file.
    environment = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        environment.pop(name, None)
    environment["GIT_CEILING_DIRECTORIES"] = str(sources.resolve().parent)
    for entry in patch_series():
        patch = SOURCE_DIRECTORY / "patches" / entry["file"]
        if not patch.is_file():
            raise MediaPlaybackBuildError(f"Missing playback patch: {entry['file']}")
        command = [git, "apply", "--no-index", "--whitespace=nowarn"]
        for flags in (["--check"], []):
            result = subprocess.run(
                [*command, *flags, str(patch.resolve())], cwd=sources,
                capture_output=True, text=True, check=False,
                env=environment,
            )
            if result.returncode:
                raise MediaPlaybackBuildError(
                    f"Playback patch {entry['file']} no longer applies: {result.stderr.strip()}"
                )


def download_sources(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"obs-studio-{OBS_VERSION}.tar.gz"
    if archive.is_symlink():
        raise MediaPlaybackBuildError("OBS source archive must not be a symbolic link")
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            temporary = Path(output.name)
            try:
                with urllib.request.urlopen(SOURCE_URL, timeout=120) as response:
                    shutil.copyfileobj(response, output)
            except BaseException:  # noqa: BLE001 - remove partial downloads even on cancellation
                output.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            _verify_archive(temporary)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    _verify_archive(archive)
    return archive


def _verify_archive(archive: Path) -> None:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != SOURCE_SHA256:
        raise MediaPlaybackBuildError(f"OBS source archive failed SHA-256 verification: {archive}")


def prepare_sources(cache: Path) -> Path:
    archive = download_sources(cache)
    destination = cache.resolve() / f"obs-{OBS_VERSION}"
    if destination.is_symlink():
        raise MediaPlaybackBuildError("OBS source destination must not be a symbolic link")
    with tempfile.TemporaryDirectory(prefix=".obs-", dir=cache) as directory:
        staging = Path(directory) / "sources"
        staging.mkdir()
        with tarfile.open(archive, "r:gz") as bundle:
            prefix = f"obs-studio-{OBS_VERSION}/"
            for member in bundle:
                if not member.name.startswith(prefix):
                    continue
                name = member.name.removeprefix(prefix)
                selected = (
                    (name.startswith("libobs/") and name.endswith(".h"))
                    or (name.startswith(f"{_PLAYBACK}/") and name.endswith((".c", ".h")))
                    or (name.startswith("deps/w32-pthreads/") and name.endswith(".h"))
                    or name
                    in {
                        "plugins/obs-ffmpeg/obs-ffmpeg-source.c",
                        "plugins/obs-ffmpeg/obs-ffmpeg-compat.h",
                        "plugins/obs-ffmpeg/obs-ffmpeg-formats.h",
                        "plugins/obs-ffmpeg/data/locale/en-US.ini",
                        "COPYING",
                    }
                )
                if not selected:
                    continue
                if not member.isfile() or ".." in PurePosixPath(name).parts:
                    raise MediaPlaybackBuildError("OBS archive contains an invalid source member")
                source = bundle.extractfile(member)
                if source is None:
                    raise MediaPlaybackBuildError(f"OBS archive is missing source bytes: {name}")
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                text = source.read().decode("utf-8")
                path.write_text(text, encoding="utf-8", newline="\n")
        for required in (
            "plugins/obs-ffmpeg/obs-ffmpeg-source.c",
            f"{_PLAYBACK}/media.c",
            f"{_PLAYBACK}/decode.c",
            f"{_PLAYBACK}/media-playback.c",
            f"{_PLAYBACK}/cache.c",
            "libobs/obs.h",
            "COPYING",
        ):
            if not (staging / required).is_file():
                raise MediaPlaybackBuildError(f"OBS archive is missing required source: {required}")
        apply_patches(staging)
        _publish_runtime(staging, destination)
    return destination


def prepare_playback_sdk(platform: str, architecture: str, cache: Path) -> Path:
    """Reuse SDK headers only after checking them against the verified archive."""
    sdk = cache.resolve() / f"sdk-{platform}-{architecture}"
    if platform != "linux" and sdk.is_dir() and not sdk.is_symlink():
        archive = _download_sdk(SDK_SPECS[platform, architecture], cache)
        prefixes = ("include/libav", "include/libswscale/", "include/simde/")
        checked = 0
        matches = True
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    if member.is_dir() or not member.filename.startswith(prefixes):
                        continue
                    path = sdk / member.filename
                    if (
                        not path.is_file()
                        or hashlib.sha256(path.read_bytes()).digest()
                        != hashlib.sha256(bundle.read(member)).digest()
                    ):
                        matches = False
                        break
                    checked += 1
        else:
            with tarfile.open(archive, "r:xz") as bundle:
                for member in bundle:
                    if not member.isfile() or not member.name.startswith(prefixes):
                        continue
                    source = bundle.extractfile(member)
                    path = sdk / member.name
                    if (
                        source is None
                        or not path.is_file()
                        or hashlib.sha256(path.read_bytes()).digest()
                        != hashlib.sha256(source.read()).digest()
                    ):
                        matches = False
                        break
                    checked += 1
        if matches and checked:
            return sdk
    return prepare_sdk(platform, architecture, cache)


_VERIFY_MODULE = """
import sys, tempfile
from pylibobs.context import OBSContext
from pylibobs.source import Source
from pylibobs._ffi import ffi, get_lib
with tempfile.TemporaryDirectory(prefix='solin-playback-qualification-') as config:
    with OBSContext(module_config_path=config) as context:
        context.set_audio(samples_per_sec=48000)
        library = get_lib()
        module = ffi.new('obs_module_t **')
        result = library.obs_open_module(module, sys.argv[1].encode(), sys.argv[2].encode())
        if result != 0 or not library.obs_init_module(module[0]):
            raise RuntimeError('Solin media module could not be initialized')
        source = Source.create('solin_media_source', 'solin-playback-qualification',
                               {'is_local_file': True, 'local_file': ''})
        if source.id != 'solin_media_source':
            raise RuntimeError('Solin media source was not registered')
        source.release()
print('Solin media source qualified')
"""


def verify_module(binary: Path, data: Path) -> None:
    environment = os.environ.copy()
    for name in ("LIBOBS_PATH", "OBS_DATA_PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"):
        environment.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-B", "-c", _VERIFY_MODULE, str(binary.resolve()), str(data.resolve())],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode != 0 or "Solin media source qualified" not in result.stdout:
        raise MediaPlaybackBuildError(
            f"Playback module qualification failed ({result.returncode}):\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
        )


def install_module(binary: Path, sources: Path, runtime: Path, platform: str) -> Path:
    plugins = runtime / "obs-plugins"
    plugins.mkdir(exist_ok=True)
    destination = plugins / f"{MODULE_NAME}{SUFFIXES[platform]}"
    data = runtime / "data" / "obs-plugins" / MODULE_NAME
    if destination.is_symlink() or data.is_symlink():
        raise MediaPlaybackBuildError("Owned module destinations must not be symbolic links")
    with tempfile.TemporaryDirectory(prefix=".solin-media-", dir=runtime) as directory:
        staging = Path(directory)
        candidate = staging / destination.name
        shutil.copy2(binary, candidate)
        candidate_data = staging / "data"
        (candidate_data / "locale").mkdir(parents=True)
        shutil.copy2(
            sources / "plugins/obs-ffmpeg/data/locale/en-US.ini",
            candidate_data / "locale/en-US.ini",
        )
        shutil.copy2(sources / "COPYING", candidate_data / "COPYING")
        if platform == "macos":
            _relocate_macos(runtime, binaries=[candidate])
        verify_module(candidate, candidate_data)
        _publish_runtime(candidate_data, data)
        candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        installed_hash = (
            hashlib.sha256(destination.read_bytes()).hexdigest() if destination.is_file() else None
        )
        # Windows keeps loaded modules locked. An identical qualified artifact
        # needs no replacement and can be rebuilt while the application is open.
        if installed_hash != candidate_hash:
            _retry_windows_sharing(lambda: os.replace(candidate, destination))
        receipt = {
            "schema": 1,
            "source_id": SOURCE_ID,
            "obs_version": OBS_VERSION,
            "source_sha256": SOURCE_SHA256,
            "plugin": {
                "path": destination.relative_to(runtime).as_posix(),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            },
            "patches": [
                {
                    **entry,
                    "sha256": hashlib.sha256(
                        (SOURCE_DIRECTORY / "patches" / entry["file"]).read_bytes()
                    ).hexdigest(),
                }
                for entry in patch_series()
            ],
        }
        manifest = staging / MANIFEST_NAME
        manifest.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        _retry_windows_sharing(lambda: os.replace(manifest, runtime / MANIFEST_NAME))
    return destination


def build_media_playback(
    *, build_directory: Path, cache_directory: Path, configuration: str = "Release", jobs: int = 0
) -> Path:
    if jobs < 0:
        raise MediaPlaybackBuildError("--jobs must be greater than or equal to zero")
    platform, architecture = target()
    runtime = installed_runtime(platform, architecture)
    sources = prepare_sources(cache_directory.resolve())
    sdk = prepare_playback_sdk(platform, architecture, cache_directory.resolve() / "dependencies")
    cmake = shutil.which("cmake")
    if cmake is None:
        raise MediaPlaybackBuildError("CMake 3.25 or newer and a native C toolchain are required")
    build_directory = build_directory.resolve()
    command = [
        cmake,
        "-S",
        str(SOURCE_DIRECTORY),
        "-B",
        str(build_directory),
        f"-DSOLIN_OBS_ROOT={sources}",
        f"-DSOLIN_OBS_RUNTIME={runtime}",
        f"-DSOLIN_FFMPEG_ROOT={sdk}",
    ]
    if platform == "windows":
        command.extend(["-A", "x64"])
    else:
        command.append(f"-DCMAKE_BUILD_TYPE={configuration}")
    if platform == "macos":
        command.extend(
            [f"-DCMAKE_OSX_ARCHITECTURES={architecture}", "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0"]
        )
    _run(command)
    command = [cmake, "--build", str(build_directory), "--config", configuration, "--parallel"]
    if jobs:
        command.append(str(jobs))
    _run(command)
    ctest = str(Path(cmake).with_name("ctest.exe" if platform == "windows" else "ctest"))
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
    filename = f"{MODULE_NAME}{SUFFIXES[platform]}"
    binary = next(
        (
            p
            for p in (build_directory / configuration / filename, build_directory / filename)
            if p.is_file()
        ),
        None,
    )
    if binary is None:
        raise MediaPlaybackBuildError("CMake did not produce the playback module")
    return install_module(binary, sources, runtime, platform)


def _run(command: list[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/native/media-playback")
    parser.add_argument(
        "--cache-dir", type=Path, default=ROOT / "build/dependencies/media-playback"
    )
    parser.add_argument(
        "--configuration", choices=("Release", "RelWithDebInfo", "Debug"), default="Release"
    )
    parser.add_argument("--jobs", type=int, default=0)
    options = parser.parse_args(arguments)
    try:
        result = build_media_playback(
            build_directory=options.build_dir,
            cache_directory=options.cache_dir,
            configuration=options.configuration,
            jobs=options.jobs,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Solin media playback module installed and qualified: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
