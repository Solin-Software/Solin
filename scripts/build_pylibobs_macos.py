"""Build the missing Intel wheel from pinned pylibobs and official OBS binaries.

Run before installing requirements on macOS Intel. Other hosts use published
wheels and this command exits without downloading or changing their environment.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PYLIBOBS_VERSION = "0.1.2"
OBS_VERSION = "32.1.2"
DEPLOYMENT_TARGET = "12.0"
WHEEL_NAME = f"pylibobs-{PYLIBOBS_VERSION}-py3-none-macosx_12_0_x86_64.whl"
SOURCE_URL = (
    "https://files.pythonhosted.org/packages/6a/f1/"
    "0b822be7942c00a1676d0b020c409b0048d5904a8ca411cb36a1122428ba/"
    f"pylibobs-{PYLIBOBS_VERSION}.tar.gz"
)
SOURCE_SHA256 = "ff9c08b57142756924cd0e44111b5e020ce6bc7f8438e872f8fc9dfa18b29b12"
RUNTIME_URL = (
    f"https://github.com/obsproject/obs-studio/releases/download/{OBS_VERSION}/"
    f"OBS-Studio-{OBS_VERSION}-macOS-Intel.dmg"
)
RUNTIME_SHA256 = "f7febee4c52e97930ffa9d8bcae79ee4c60c411827688cfbe36bc53edc51616e"
REQUIRED_PLUGINS = (
    "obs-ffmpeg",
    "image-source",
    "obs-filters",
    "obs-transitions",
    "obs-outputs",
    "obs-x264",
    "coreaudio-encoder",
    "mac-capture",
    "mac-avcapture",
    "mac-virtualcam",
)
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


class BindingBuildError(RuntimeError):
    pass


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def _download(url: str, destination: Path, expected_digest: str) -> None:
    if destination.is_file():
        with destination.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_digest:
                raise BindingBuildError(
                    f"Cached artifact failed SHA-256 verification: {destination}"
                )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    download = tempfile.NamedTemporaryFile(dir=destination.parent, delete=False)
    temporary = Path(download.name)
    try:
        with download as stream:
            with urllib.request.urlopen(url, timeout=120) as response:
                shutil.copyfileobj(response, stream)
        with temporary.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_digest:
                raise BindingBuildError(f"Downloaded artifact failed SHA-256 verification: {url}")
        temporary.replace(destination)
    finally:
        download.close()
        temporary.unlink(missing_ok=True)


def _copy_runtime(contents: Path, destination: Path) -> None:
    """Adapt OBS's app layout to the binding's bundled-library/module contract."""
    frameworks = destination / "Frameworks"
    original_frameworks = contents / "Frameworks"
    shutil.copytree(
        original_frameworks,
        frameworks,
        ignore=shutil.ignore_patterns(
            "*.framework",
            "*.app",
            "*.dSYM",
            "obs-scripting.dylib",
            "libobs-metal.dylib",
            "libobs-frontend-api.1.dylib",
        ),
    )
    # Keep one libobs binary. Rewrite every plugin's framework install name to
    # this alias below, avoiding two independent copies of libobs global state.
    shutil.copy2(
        original_frameworks / "libobs.framework" / "Versions" / "A" / "libobs",
        frameworks / "libobs.dylib",
    )
    if not (frameworks / "libobs-opengl.dylib").is_file():
        raise BindingBuildError("Official OBS runtime is missing libobs-opengl.dylib")
    shutil.copy2(contents / "MacOS" / "obs-ffmpeg-mux", destination / "obs-ffmpeg-mux")
    # OBS embeds shader data inside libobs.framework, and module resources
    # inside each .plugin. The binding expects data/libobs and data/obs-plugins.
    data_root = destination / "data"
    shutil.copytree(
        original_frameworks / "libobs.framework" / "Versions" / "A" / "Resources",
        data_root / "libobs",
    )
    plugins = destination / "Frameworks" / "obs-plugins"
    plugins.mkdir()
    for name in REQUIRED_PLUGINS:
        bundle = contents / "PlugIns" / f"{name}.plugin" / "Contents"
        binary = bundle / "MacOS" / name
        if not binary.is_file():
            raise BindingBuildError(f"Official OBS runtime is missing plugin {name}")
        shutil.copy2(binary, plugins / f"{name}.dylib")
        # Bundled module lookup is relative to the flat libobs.dylib directory.
        data = bundle / "Resources"
        if data.is_dir():
            target = destination / "data" / "obs-plugins" / name
            shutil.copytree(data, target, dirs_exist_ok=True)
    # The stager owns the canonical top-level paths too. Wheel tooling expands
    # symlinks; retain explicit directories for both binding and staging paths.
    shutil.copytree(plugins, destination / "obs-plugins")
    shutil.copytree(destination / "data", frameworks / "data")


def _relocate_runtime(root: Path) -> None:
    frameworks = root / "Frameworks"
    for binary in sorted(root.rglob("*")):
        if not binary.is_file():
            continue
        with binary.open("rb") as stream:
            if stream.read(4) not in MACHO_MAGICS:
                continue
        architecture = _run(["lipo", "-archs", str(binary)], capture_output=True, text=True)
        if "x86_64" not in architecture.stdout.split():
            raise BindingBuildError(f"Runtime binary does not support Intel: {binary}")
        if binary == frameworks / "libobs.dylib":
            _run(["install_name_tool", "-id", "@rpath/libobs.dylib", str(binary)])
        dependencies = _run(
            ["otool", "-arch", "x86_64", "-L", str(binary)],
            capture_output=True,
            text=True,
        ).stdout
        for line in dependencies.splitlines()[1:]:
            dependency = line.strip().split(" (compatibility version", 1)[0]
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if "libobs.framework/" in dependency:
                relocated = "@rpath/libobs.dylib"
            elif dependency.startswith("@rpath/"):
                relocated = dependency
            else:
                # Official release paths outside the system directories must
                # resolve to bundled Frameworks, even when originally absolute
                # or relative to OBS.app's executable.
                relative_dependency = dependency.split("Frameworks/", 1)[-1]
                if relative_dependency == dependency:
                    relative_dependency = Path(dependency).name
                relocated = f"@rpath/{relative_dependency}"
            target = frameworks / relocated.removeprefix("@rpath/")
            if not target.is_file():
                raise BindingBuildError(
                    f"Unbundled native dependency {dependency} required by {binary}"
                )
            if relocated != dependency:
                _run(["install_name_tool", "-change", dependency, relocated, str(binary)])
        relative = os.path.relpath(frameworks, binary.parent).replace(os.sep, "/")
        rpath = "@loader_path" if relative == "." else f"@loader_path/{relative}"
        info = _run(
            ["otool", "-arch", "x86_64", "-l", str(binary)],
            capture_output=True,
            text=True,
        ).stdout
        for line in info.splitlines():
            if line.strip().startswith("path ") and " (offset" in line:
                previous = line.strip()[5:].split(" (offset", 1)[0]
                if previous.startswith("@executable_path") or previous.startswith("/"):
                    _run(["install_name_tool", "-delete_rpath", previous, str(binary)])
        if f"path {rpath} (offset" not in info:
            _run(["install_name_tool", "-add_rpath", rpath, str(binary)])
        # Relocation changes signed bytes. Ad-hoc signing is required for dyld
        # integrity checks; final application signing belongs to packaging.
        _run(["codesign", "--force", "--sign", "-", str(binary)])


def _extract_runtime(archive: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="obs-mount-") as temporary:
        mount = Path(temporary) / "mounted"
        mount.mkdir()
        _run(
            [
                "hdiutil",
                "attach",
                str(archive),
                "-mountpoint",
                str(mount),
                "-readonly",
                "-nobrowse",
                "-quiet",
            ]
        )
        try:
            applications = list(mount.glob("*.app"))
            if len(applications) != 1:
                raise BindingBuildError("Expected one OBS application inside the pinned DMG")
            _copy_runtime(applications[0] / "Contents", destination)
        finally:
            _run(["hdiutil", "detach", str(mount), "-quiet"])
    _relocate_runtime(destination)


def _verify_source_runtime(source: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source)
    # Require the built runtime and data rather than developer/system overrides.
    for name in ("LIBOBS_PATH", "OBS_DATA_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        environment.pop(name, None)
    _run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "from pylibobs import OBSContext, Source",
                    "from pylibobs._ffi import ffi, get_lib",
                    "from pylibobs._lib import get_bundled_modules",
                    "from pylibobs.display import render_source_letterboxed",
                    "from pathlib import Path",
                    f"assert ffi.string(get_lib().obs_get_version_string()).decode() == {OBS_VERSION!r}",
                    f"assert set({REQUIRED_PLUGINS!r}) <= {{m[0] for m in get_bundled_modules()}}",
                    "with OBSContext() as context:",
                    "    graphics = Path('pylibobs/_libs/macos/x86_64/Frameworks/libobs-opengl.dylib').resolve()",
                    "    context.set_video(320, 180, fps_num=30, graphics_module=str(graphics))",
                    "    context.set_audio()",
                    "    context.load_modules()",
                    "    for kind in ('ffmpeg_source', 'image_source', 'color_source_v3'):",
                    "        source = Source.create(kind, 'smoke-' + kind, {})",
                    "        try:",
                    "            flags = get_lib().obs_source_get_output_flags(source._ptr)",
                    "            assert flags & 1, 'Missing native video source: ' + kind",
                    "        finally:",
                    "            source.release()",
                )
            ),
        ],
        cwd=source,
        env=environment,
        timeout=90,
    )


def build_wheel(output_dir: Path, cache_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_archive = cache_dir / f"pylibobs-{PYLIBOBS_VERSION}.tar.gz"
    runtime_archive = cache_dir / f"OBS-Studio-{OBS_VERSION}-macOS-Intel.dmg"
    _download(SOURCE_URL, source_archive, SOURCE_SHA256)
    _download(RUNTIME_URL, runtime_archive, RUNTIME_SHA256)
    with tempfile.TemporaryDirectory(prefix="pylibobs-intel-") as temporary:
        work = Path(temporary)
        with tarfile.open(source_archive) as archive:
            archive.extractall(work, filter="data")
        source = work / f"pylibobs-{PYLIBOBS_VERSION}"
        runtime = source / "pylibobs" / "_libs" / "macos" / "x86_64"
        _extract_runtime(runtime_archive, runtime)
        notices = runtime / "licenses"
        notices.mkdir()
        shutil.copy2(source / "LICENSE", notices / "pylibobs-LICENSE")
        (notices / "OBS-NOTICE.txt").write_text(
            f"OBS Studio {OBS_VERSION} (GPL-2.0-or-later)\n"
            f"https://github.com/obsproject/obs-studio/tree/{OBS_VERSION}\n"
            "Native libraries and plugins are from the official Intel release.\n",
            encoding="utf-8",
        )
        _verify_source_runtime(source)
        environment = dict(os.environ, MACOSX_DEPLOYMENT_TARGET=DEPLOYMENT_TARGET)
        wheels = work / "wheels"
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheels),
                str(source),
            ],
            env=environment,
        )
        wheel = wheels / WHEEL_NAME
        if not wheel.is_file():
            raise BindingBuildError(f"Build did not produce the expected Intel wheel {WHEEL_NAME}")
        with zipfile.ZipFile(wheel) as archive:
            if not any(name.endswith("/Frameworks/libobs.dylib") for name in archive.namelist()):
                raise BindingBuildError("Built wheel does not contain libobs")
        destination = output_dir / WHEEL_NAME
        with tempfile.NamedTemporaryFile(dir=output_dir, delete=False) as stream:
            temporary_wheel = Path(stream.name)
        try:
            shutil.copy2(wheel, temporary_wheel)
            temporary_wheel.replace(destination)
        finally:
            temporary_wheel.unlink(missing_ok=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".pip-wheels"))
    parser.add_argument("--cache-dir", type=Path, default=Path("build/dependencies/pylibobs"))
    args = parser.parse_args(argv)
    if sys.platform != "darwin" or platform.machine().lower() != "x86_64":
        print("Published pylibobs wheel is available for this host; Intel build is unnecessary.")
        return 0
    try:
        print(build_wheel(args.output_dir.resolve(), args.cache_dir.resolve()))
    except (BindingBuildError, OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        parser.exit(1, f"pylibobs Intel build failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
