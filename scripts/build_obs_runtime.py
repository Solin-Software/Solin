"""Build and qualify the ABI-matched Windows OBS core with monitoring corrections."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

if __package__:
    from . import obs_sources
    from .build_media_extract import installed_runtime, prepare_sdk, target
    from .package_libobs_runtime import (
        _publish_runtime,
        _retry_windows_sharing,
        module_binary_path,
        module_data_path,
    )
else:
    import obs_sources
    from build_media_extract import installed_runtime, prepare_sdk, target
    from package_libobs_runtime import (
        _publish_runtime,
        _retry_windows_sharing,
        module_binary_path,
        module_data_path,
    )

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = ROOT / "native" / "obs_runtime"
MANIFEST_NAME = "obs-runtime-manifest.json"
_REQUIRED_MODULES = {"obs-ffmpeg": "ffmpeg_source", "win-wasapi": "wasapi_output_capture"}


class ObsRuntimeBuildError(RuntimeError):
    """The corrected core could not be built or qualified."""


def prepare_sources(cache: Path) -> Path:
    archive = obs_sources.download_sources(cache)
    destination = cache.resolve() / f"obs-core-{obs_sources.OBS_VERSION}"
    if destination.is_symlink():
        raise ObsRuntimeBuildError("OBS core destination must not be a symbolic link")
    with tempfile.TemporaryDirectory(prefix=".obs-core-", dir=cache) as directory:
        staging = Path(directory)
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(staging, filter="data")
        sources = staging / f"obs-studio-{obs_sources.OBS_VERSION}"
        for name in ("CMakeLists.txt", "libobs/obs.c", "COPYING"):
            if not (sources / name).is_file():
                raise ObsRuntimeBuildError(f"OBS archive is missing required core source: {name}")
        obs_sources.apply_patches(sources, SOURCE_DIRECTORY / "patches")
        _publish_runtime(sources, destination)
    return destination


def _exports(linker: Path, binary: Path) -> set[str]:
    result = subprocess.run(
        [str(linker), "/dump", "/exports", str(binary)],
        check=True,
        capture_output=True,
        text=True,
    )
    names = set(re.findall(r"^\s+\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(\S+)", result.stdout, re.M))
    if not names:
        raise ObsRuntimeBuildError(f"The OBS core has no inspectable exports: {binary}")
    return names


def verify_exports(linker: Path, binary: Path, installed: Path) -> None:
    missing = _exports(linker, installed) - _exports(linker, binary)
    if missing:
        raise ObsRuntimeBuildError(
            f"The corrected OBS core removes runtime exports: {sorted(missing)}"
        )


_VERIFY_RUNTIME = """
import json, os, sys, tempfile
from pathlib import Path
candidate, runtime, expected_version, module_paths = sys.argv[1:]
dll_search = os.add_dll_directory(runtime)
from pylibobs.context import OBSContext
from pylibobs._ffi import ffi, get_lib
library = get_lib()
actual_version = library.obs_get_version()
if actual_version != int(expected_version):
    raise RuntimeError(
        f'The corrected OBS core does not match the pinned runtime ABI: '
        f'expected {int(expected_version):#x}, got {actual_version:#x}')
required_modules = json.loads(module_paths)
with tempfile.TemporaryDirectory(prefix='solin-core-qualification-') as config:
    with OBSContext(module_config_path=config) as context:
        context.set_audio(samples_per_sec=48000)
        # The candidate core is outside the installed package; resolve required
        # modules from the selected runtime rather than package discovery.
        for binary_path, data_path, _ in required_modules:
            binary, data = Path(binary_path), Path(data_path)
            if not binary.is_file():
                raise RuntimeError(f'Required OBS module binary is missing: {binary}')
            if not data.is_dir():
                raise RuntimeError(f'Required OBS module data directory is missing: {data}')
            module = ffi.new('obs_module_t **')
            # libobs splits module names at '/', including on Windows.
            result = library.obs_open_module(
                module, binary.as_posix().encode(), data.as_posix().encode())
            if result != 0:
                raise RuntimeError(
                    f'obs_open_module failed for {binary} (code {result}; data: {data})')
            if module[0] == ffi.NULL:
                raise RuntimeError(f'obs_open_module returned a NULL module for {binary}')
            if not library.obs_init_module(module[0]):
                raise RuntimeError(
                    f'obs_init_module returned false for {binary} (data: {data})')
        library.obs_post_load_modules()
        from pylibobs import enum_source_types
        registered = set(enum_source_types())
        missing = sorted(source for _, _, source in required_modules if source not in registered)
        if missing:
            raise RuntimeError(
                'Required Windows source types were not registered: ' + ', '.join(missing))
print('Corrected OBS core qualified')
"""


def verify_runtime(binary: Path, runtime: Path) -> None:
    runtime = runtime.resolve()
    environment = os.environ.copy()
    environment["LIBOBS_PATH"] = str(binary.resolve())
    environment["OBS_DATA_PATH"] = str(runtime / "data")
    # Keep dependencies visible to helper processes; the child retains an
    # AddDllDirectory handle for libobs's LoadLibraryExW module loads.
    environment["PATH"] = str(runtime) + os.pathsep + environment.get("PATH", "")
    major, minor, patch = map(int, obs_sources.OBS_VERSION.split("."))
    modules = [
        (
            str(module_binary_path(runtime, "windows", name)),
            str(module_data_path(runtime, "windows", name)),
            source,
        )
        for name, source in _REQUIRED_MODULES.items()
    ]
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            _VERIFY_RUNTIME,
            str(binary.resolve()),
            str(runtime),
            str(major << 24 | minor << 16 | patch),
            json.dumps(modules),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode or "Corrected OBS core qualified" not in result.stdout:
        raise ObsRuntimeBuildError(
            f"OBS core qualification failed ({result.returncode}):\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
        )


def install_runtime(binary: Path, sources: Path, runtime: Path) -> Path:
    destination = runtime / "obs.dll"
    if destination.is_symlink():
        raise ObsRuntimeBuildError("The owned OBS core must not be a symbolic link")
    with tempfile.TemporaryDirectory(prefix=".solin-obs-core-", dir=runtime) as directory:
        staging = Path(directory)
        candidate = staging / destination.name
        shutil.copy2(binary, candidate)
        verify_runtime(candidate, runtime)
        license_dir = runtime / "data" / "solin-obs-runtime"
        license_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sources / "COPYING", license_dir / "COPYING")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if (
            not destination.is_file()
            or hashlib.sha256(destination.read_bytes()).hexdigest() != digest
        ):
            _retry_windows_sharing(lambda: os.replace(candidate, destination))
        receipt = {
            "schema": 1,
            "obs_version": obs_sources.OBS_VERSION,
            "source_sha256": obs_sources.SOURCE_SHA256,
            "library": {"path": destination.name, "sha256": digest},
            "patches": obs_sources.patch_receipts(SOURCE_DIRECTORY / "patches"),
        }
        manifest = staging / MANIFEST_NAME
        manifest.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        _retry_windows_sharing(lambda: os.replace(manifest, runtime / MANIFEST_NAME))
    return destination


def _run(command: list[str]) -> None:
    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def build_obs_runtime(
    *,
    build_directory: Path,
    cache_directory: Path,
    configuration: str = "Release",
    jobs: int = 0,
) -> Path:
    if target() != ("windows", "x86_64"):
        raise ObsRuntimeBuildError("The monitoring core correction applies only to Windows x86_64")
    if configuration not in {"Release", "RelWithDebInfo"}:
        raise ObsRuntimeBuildError(
            "The installed OBS core requires a release runtime configuration"
        )
    if jobs < 0:
        raise ObsRuntimeBuildError("--jobs must be greater than or equal to zero")
    runtime = installed_runtime("windows", "x86_64")
    sources = prepare_sources(cache_directory.resolve())
    sdk = prepare_sdk("windows", "x86_64", cache_directory.resolve() / "dependencies")
    cmake = shutil.which("cmake")
    if cmake is None:
        raise ObsRuntimeBuildError("CMake 3.28 or newer and the MSVC x64 toolchain are required")
    build_directory = build_directory.resolve()
    _run(
        [
            cmake,
            "-S",
            str(sources),
            "-B",
            str(build_directory),
            "-A",
            "x64",
            f"-DOBS_VERSION_OVERRIDE={obs_sources.OBS_VERSION}",
            f"-DCMAKE_PREFIX_PATH={sdk}",
            f"-DSOLIN_OBS_CONTRACT_ROOT={SOURCE_DIRECTORY}",
        ]
    )
    command = [cmake, "--build", str(build_directory), "--config", configuration, "--parallel"]
    if jobs:
        command.append(str(jobs))
    _run(command)
    _run(
        [
            str(Path(cmake).with_name("ctest.exe")),
            "--test-dir",
            str(build_directory),
            "--build-config",
            configuration,
            "--output-on-failure",
        ]
    )
    binary = build_directory / "libobs" / configuration / "obs.dll"
    cache = (build_directory / "CMakeCache.txt").read_text(encoding="utf-8")
    match = re.search(r"^CMAKE_LINKER:FILEPATH=(.+)$", cache, re.M)
    if match is None:
        raise ObsRuntimeBuildError("CMake did not identify the core's MSVC linker")
    verify_exports(Path(match[1].strip()), binary, runtime / "obs.dll")
    return install_runtime(binary, sources, runtime)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=ROOT / "build/native/obs-runtime")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "build/dependencies/obs-runtime")
    parser.add_argument("--configuration", choices=("Release", "RelWithDebInfo"), default="Release")
    parser.add_argument("--jobs", type=int, default=0)
    options = parser.parse_args(arguments)
    try:
        result = build_obs_runtime(
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
    print(f"Corrected OBS core installed and qualified: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
