"""Build the pinned macOS pylibobs wheel from official OBS binaries.

The published pylibobs 0.1.2 macOS wheel flattens the libobs framework executable
into a dylib path. OBS 32's Cocoa data lookup requires libobs to be loaded from
inside ``libobs.framework`` so ``NSBundle`` can resolve its shader resources.
Build both macOS architectures from the pinned source and official OBS release
while preserving that framework identity and pylibobs' private module layout.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

PYLIBOBS_VERSION = "0.1.2"
OBS_VERSION = "32.1.2"
DEPLOYMENT_TARGET = "12.0"
SOURCE_URL = (
    "https://files.pythonhosted.org/packages/6a/f1/"
    "0b822be7942c00a1676d0b020c409b0048d5904a8ca411cb36a1122428ba/"
    f"pylibobs-{PYLIBOBS_VERSION}.tar.gz"
)
SOURCE_SHA256 = "ff9c08b57142756924cd0e44111b5e020ce6bc7f8438e872f8fc9dfa18b29b12"
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
REPO_ROOT = Path(__file__).resolve().parents[1]
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}


@dataclass(frozen=True)
class RuntimeSpec:
    release_label: str
    sha256: str


RUNTIME_SPECS = {
    "x86_64": RuntimeSpec(
        release_label="Intel",
        sha256="f7febee4c52e97930ffa9d8bcae79ee4c60c411827688cfbe36bc53edc51616e",
    ),
    "arm64": RuntimeSpec(
        release_label="Apple",
        sha256="2aeb3aaa99544fefd557f10ac6550e73df71540dd57528b2a1e6f39a55ebacfb",
    ),
}
ARCHITECTURE_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
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


def _runtime_url(spec: RuntimeSpec) -> str:
    return (
        f"https://github.com/obsproject/obs-studio/releases/download/{OBS_VERSION}/"
        f"OBS-Studio-{OBS_VERSION}-macOS-{spec.release_label}.dmg"
    )


def _wheel_name(architecture: str) -> str:
    return f"pylibobs-{PYLIBOBS_VERSION}-py3-none-macosx_12_0_{architecture}.whl"


def _patch_macos_locator(source: Path) -> None:
    """Backport the framework-aware bundled-runtime lookup to pylibobs 0.1.2.

    ``libobs/obs-cocoa.m`` resolves core data through ``NSBundle`` before the
    generic data-path fallbacks. Loading a copied/flattened framework executable
    therefore makes ``bundleWithIdentifier`` return nil and OBS 32.1.2 crashes
    while initializing video shaders. The PyPI 0.1.2 sdist also derives bundled
    plugin/data directories directly from ``libobs_path.parent``; once libobs is
    correctly loaded from ``Versions/A/libobs`` that points inside the framework,
    not at the architecture bundle root. Backport the later bundled-root helper
    and fail closed if the pinned source no longer matches the audited shapes.
    """
    locator = source / "pylibobs" / "_lib.py"
    text = locator.read_text(encoding="utf-8")
    candidates_pattern = re.compile(
        r"    # Try the most-likely path first per platform\n"
        r"    candidates = \[\n"
        r"(?:        .*\n){3}"
        r"    \]\n"
    )
    candidates = """    # Keep the framework executable first on macOS so Cocoa can resolve its NSBundle.\n    candidates = []\n    if system == \"Darwin\":\n        candidates.append(\n            base / \"Frameworks\" / \"libobs.framework\" / \"Versions\" / \"A\" / \"libobs\"\n        )\n    candidates.extend(\n        [\n            base / lib_name,  # win/linux flat layout\n            base / \"Frameworks\" / lib_name,  # legacy macOS flat layout\n            base / \"Frameworks\" / \"libobs.0.dylib\",  # macOS versioned alias\n        ]\n    )\n"""
    text, count = candidates_pattern.subn(candidates, text, count=1)
    if count != 1:
        raise BindingBuildError("Pinned pylibobs macOS locator no longer matches the audited source")

    helper_marker = "\n\n# OBS helpers that libobs spawns"
    helper = """

def _bundled_root(libobs_path: str | Path) -> Path | None:
    \"\"\"Return the platform/architecture bundle containing ``libobs_path``.\"\"\"
    libs_root = _LIBS_DIR.resolve()
    for candidate in Path(libobs_path).resolve().parents:
        if candidate.parent.parent == libs_root:
            return candidate
    return None
"""
    if helper_marker not in text:
        raise BindingBuildError("Pinned pylibobs helper boundary no longer matches the audited source")
    text = text.replace(helper_marker, helper + helper_marker, 1)

    data_pattern = re.compile(
        r"    # Co-located bundled data \(same directory as the DLL\)\n"
        r"    for candidate in \[\n"
        r"        libobs_path\.parent / \"data\",\n"
        r"        libobs_path\.parent\.parent\.parent / \"data\",  # OBS install layout\n"
        r"    \]:\n"
        r"        if candidate\.exists\(\):\n"
        r"            return str\(candidate\)\n"
    )
    data_lookup = """    bundled_root = _bundled_root(libobs_path)\n    candidates = []\n    if bundled_root is not None:\n        candidates.append(bundled_root / \"data\")\n    candidates.extend(\n        [\n            libobs_path.parent / \"data\",\n            libobs_path.parent.parent.parent / \"data\",  # OBS install layout\n        ]\n    )\n    for candidate in candidates:\n        if candidate.exists():\n            return str(candidate)\n"""
    text, count = data_pattern.subn(data_lookup, text, count=1)
    if count != 1:
        raise BindingBuildError("Pinned pylibobs data lookup no longer matches the audited source")

    modules_pattern = re.compile(
        r"    # 1\. Bundled layout \(fetch_libs\.py extracts plugins next to obs\.dll\)\n"
        r"    bundled_plugins = lib_dir / \"obs-plugins\"\n"
        r"    bundled_data = lib_dir / \"data\" / \"obs-plugins\" / \"%module%\"\n"
        r"    if bundled_plugins\.exists\(\):\n"
        r"        return str\(bundled_plugins\), str\(bundled_data\)\n"
    )
    modules_lookup = """    # 1. Bundled layout rooted at _libs/<platform>/<architecture>.\n    bundled_root = _bundled_root(libobs_path)\n    if bundled_root is not None:\n        bundled_plugins = bundled_root / \"obs-plugins\"\n        bundled_data = bundled_root / \"data\" / \"obs-plugins\" / \"%module%\"\n        if bundled_plugins.exists():\n            return str(bundled_plugins), str(bundled_data)\n"""
    text, count = modules_pattern.subn(modules_lookup, text, count=1)
    if count != 1:
        raise BindingBuildError("Pinned pylibobs module lookup no longer matches the audited source")

    locator.write_text(text, encoding="utf-8")


def _copy_runtime(contents: Path, destination: Path) -> None:
    """Preserve OBS's framework layout and expose pylibobs' flat module contract.

    The official macOS runtime is a graph of frameworks with internal rpaths.
    Keep that graph intact, matching pylibobs' own macOS extractor, and only
    flatten the plugin binaries/data that pylibobs enumerates from
    ``obs-plugins``. Rewriting the entire framework graph is both unnecessary
    and unsafe because it changes load semantics OBS already qualified.
    """
    frameworks = destination / "Frameworks"
    original_frameworks = contents / "Frameworks"
    shutil.copytree(
        original_frameworks,
        frameworks,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            "*.dSYM",
            "Qt*.framework",
            "Chromium Embedded Framework.framework",
            "Sparkle.framework",
            "Syphon.framework",
            "OBS Helper*.app",
            "obs-scripting.dylib",
            "libobs-frontend-api.1.dylib",
        ),
    )
    # Keep libobs in its framework bundle. On macOS OBS resolves shader data via
    # NSBundle before checking generic data paths; copying this executable to a
    # flat dylib path loses the framework identity and crashes in obs-cocoa.m.
    framework_libobs = frameworks / "libobs.framework" / "Versions" / "A" / "libobs"
    if not framework_libobs.is_file():
        raise BindingBuildError("Official OBS runtime is missing libobs.framework")
    if not (frameworks / "libobs-opengl.dylib").is_file():
        raise BindingBuildError("Official OBS runtime is missing libobs-opengl.dylib")
    shutil.copy2(contents / "MacOS" / "obs-ffmpeg-mux", destination / "obs-ffmpeg-mux")

    # Preserve OBS's canonical Resources/data tree when present. The framework
    # also carries libobs shader resources; mirror those into pylibobs' lookup
    # path so source-built and published-wheel layouts behave the same way.
    data_root = destination / "data"
    application_data = contents / "Resources" / "data"
    if application_data.is_dir():
        shutil.copytree(application_data, data_root, dirs_exist_ok=True, symlinks=True)
    framework_resources = framework_libobs.parent / "Resources"
    if framework_resources.is_dir():
        shutil.copytree(framework_resources, data_root / "libobs", dirs_exist_ok=True)

    plugins = destination / "obs-plugins"
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


def _relocate_runtime(root: Path, architecture: str) -> None:
    """Make dylib resolution independent of the process hosting pylibobs."""
    frameworks = root / "Frameworks"
    framework_libobs = frameworks / "libobs.framework" / "Versions" / "A" / "libobs"
    if not framework_libobs.is_file():
        raise BindingBuildError(f"Required macOS runtime binary is missing: {framework_libobs}")
    architectures = _run(
        ["lipo", "-archs", str(framework_libobs)], capture_output=True, text=True
    )
    if architecture not in architectures.stdout.split():
        raise BindingBuildError(
            f"Runtime binary does not support {architecture}: {framework_libobs}"
        )

    # The framework executable itself uses @rpath dependencies that the OBS.app
    # host normally supplies via @executable_path. pylibobs loads it from Python,
    # so give it a loader-relative path back to this private Frameworks tree while
    # keeping the executable inside libobs.framework for NSBundle resource lookup.
    targets = [framework_libobs, frameworks / "libobs-opengl.dylib", root / "obs-ffmpeg-mux"]
    targets.extend(sorted((root / "obs-plugins").glob("*.dylib")))
    for binary in targets:
        if not binary.is_file():
            raise BindingBuildError(f"Required macOS runtime binary is missing: {binary}")
        with binary.open("rb") as stream:
            if stream.read(4) not in MACHO_MAGICS:
                raise BindingBuildError(f"Required macOS runtime binary is not Mach-O: {binary}")
        binary_architectures = _run(
            ["lipo", "-archs", str(binary)], capture_output=True, text=True
        )
        if architecture not in binary_architectures.stdout.split():
            raise BindingBuildError(f"Runtime binary does not support {architecture}: {binary}")
        dependencies = _run(
            ["otool", "-arch", architecture, "-L", str(binary)],
            capture_output=True,
            text=True,
        ).stdout
        for line in dependencies.splitlines()[1:]:
            dependency = line.strip().split(" (compatibility version", 1)[0]
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if "libobs.framework/" in dependency or dependency == "@rpath/libobs.dylib":
                relative_libobs = os.path.relpath(framework_libobs, binary.parent).replace(
                    os.sep, "/"
                )
                relocated = f"@loader_path/{relative_libobs}"
                target = framework_libobs
            elif dependency.startswith("@rpath/"):
                relocated = dependency
                target = frameworks / relocated.removeprefix("@rpath/")
            else:
                relative_dependency = dependency.split("Frameworks/", 1)[-1]
                if relative_dependency == dependency:
                    relative_dependency = Path(dependency).name
                relocated = f"@rpath/{relative_dependency}"
                target = frameworks / relative_dependency
            if not target.is_file():
                raise BindingBuildError(
                    f"Unbundled native dependency {dependency} required by {binary}"
                )
            if target.resolve() == binary.resolve():
                continue
            if relocated != dependency:
                _run(["install_name_tool", "-change", dependency, relocated, str(binary)])
        relative = os.path.relpath(frameworks, binary.parent).replace(os.sep, "/")
        rpath = "@loader_path" if relative == "." else f"@loader_path/{relative}"
        info = _run(
            ["otool", "-arch", architecture, "-l", str(binary)],
            capture_output=True,
            text=True,
        ).stdout
        if f"path {rpath} (offset" not in info:
            _run(["install_name_tool", "-add_rpath", rpath, str(binary)])
        # install_name_tool invalidates the original signature. Final product
        # signing still happens after the runtime is staged into Solin.app.
        _run(["codesign", "--force", "--sign", "-", str(binary)])


def _extract_runtime(archive: Path, destination: Path, architecture: str) -> None:
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
    _relocate_runtime(destination, architecture)


def _verify_source_runtime(source: Path, architecture: str) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(source), str(REPO_ROOT / "src")))
    # Require the built runtime and data rather than developer/system overrides.
    for name in ("LIBOBS_PATH", "OBS_DATA_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
        environment.pop(name, None)
    _run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import pylibobs",
                    "from pylibobs import OBSContext, Source",
                    "from pylibobs._ffi import ffi, get_lib",
                    "from pylibobs._lib import find_libobs, get_bundled_modules, get_obs_module_dirs",
                    "from pylibobs.display import render_source_letterboxed",
                    "from solin.core.media.pylibobs_compat import set_video_compat",
                    "from pathlib import Path",
                    f"assert ffi.string(get_lib().obs_get_version_string()).decode() == {OBS_VERSION!r}",
                    "modules = {m[0] for m in get_bundled_modules()}",
                    f"missing = set({REQUIRED_PLUGINS!r}) - modules",
                    "assert not missing, f'Missing bundled modules {sorted(missing)}; discovered={sorted(modules)}; libobs={find_libobs()}; dirs={get_obs_module_dirs()}'",
                    "print('pylibobs smoke: startup', flush=True)",
                    "with OBSContext() as context:",
                    "    print('pylibobs smoke: startup ok', flush=True)",
                    f"    graphics = Path('pylibobs/_libs/macos/{architecture}/Frameworks/libobs-opengl.dylib').resolve()",
                    "    print('pylibobs smoke: video', flush=True)",
                    "    graphics_owner = set_video_compat(context, binding=pylibobs, width=320, height=180, fps_num=30, graphics_module=str(graphics))",
                    "    print('pylibobs smoke: video ok', flush=True)",
                    "    print('pylibobs smoke: audio', flush=True)",
                    "    context.set_audio()",
                    "    print('pylibobs smoke: audio ok', flush=True)",
                    "    print('pylibobs smoke: modules', flush=True)",
                    "    context.load_modules()",
                    "    print('pylibobs smoke: modules ok', flush=True)",
                    "    for kind in ('ffmpeg_source', 'image_source', 'color_source_v3'):",
                    "        print('pylibobs smoke: source ' + kind, flush=True)",
                    "        source = Source.create(kind, 'smoke-' + kind, {})",
                    "        try:",
                    "            flags = get_lib().obs_source_get_output_flags(source._ptr)",
                    "            assert flags & 1, 'Missing native video source: ' + kind",
                    "        finally:",
                    "            source.release()",
                    "        print('pylibobs smoke: source ' + kind + ' ok', flush=True)",
                    "_ = graphics_owner",
                    "print('pylibobs smoke: shutdown ok', flush=True)",
                )
            ),
        ],
        cwd=source,
        env=environment,
        timeout=90,
    )


def _verify_built_wheel(wheel: Path, architecture: str) -> None:
    """Require every runtime artifact that downstream packaging depends on."""
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    expected_libobs = (
        f"pylibobs/_libs/macos/{architecture}/"
        "Frameworks/libobs.framework/Versions/A/libobs"
    )
    if expected_libobs not in names:
        raise BindingBuildError("Built wheel does not contain libobs")
    expected_mux = f"pylibobs/_libs/macos/{architecture}/obs-ffmpeg-mux"
    if expected_mux not in names:
        raise BindingBuildError("Built wheel does not contain obs-ffmpeg-mux")


def build_wheel(output_dir: Path, cache_dir: Path, architecture: str) -> Path:
    spec = RUNTIME_SPECS[architecture]
    output_dir.mkdir(parents=True, exist_ok=True)
    source_archive = cache_dir / f"pylibobs-{PYLIBOBS_VERSION}.tar.gz"
    runtime_archive = cache_dir / f"OBS-Studio-{OBS_VERSION}-macOS-{spec.release_label}.dmg"
    _download(SOURCE_URL, source_archive, SOURCE_SHA256)
    _download(_runtime_url(spec), runtime_archive, spec.sha256)
    with tempfile.TemporaryDirectory(prefix=f"pylibobs-{architecture}-") as temporary:
        work = Path(temporary)
        with tarfile.open(source_archive) as archive:
            archive.extractall(work, filter="data")
        source = work / f"pylibobs-{PYLIBOBS_VERSION}"
        _patch_macos_locator(source)
        runtime = source / "pylibobs" / "_libs" / "macos" / architecture
        _extract_runtime(runtime_archive, runtime, architecture)
        notices = runtime / "licenses"
        notices.mkdir()
        shutil.copy2(source / "LICENSE", notices / "pylibobs-LICENSE")
        (notices / "OBS-NOTICE.txt").write_text(
            f"OBS Studio {OBS_VERSION} (GPL-2.0-or-later)\n"
            f"https://github.com/obsproject/obs-studio/tree/{OBS_VERSION}\n"
            f"Native libraries and plugins are from the official {spec.release_label} release.\n",
            encoding="utf-8",
        )
        _verify_source_runtime(source, architecture)
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
        wheel_name = _wheel_name(architecture)
        wheel = wheels / wheel_name
        if not wheel.is_file():
            raise BindingBuildError(f"Build did not produce the expected macOS wheel {wheel_name}")
        _verify_built_wheel(wheel, architecture)
        destination = output_dir / wheel_name
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
    if sys.platform != "darwin":
        print("Custom pylibobs wheel is only required on macOS.")
        return 0
    architecture = ARCHITECTURE_ALIASES.get(platform.machine().lower())
    if architecture not in RUNTIME_SPECS:
        parser.exit(1, f"Unsupported macOS architecture: {platform.machine()}\n")
    try:
        print(build_wheel(args.output_dir.resolve(), args.cache_dir.resolve(), architecture))
    except (BindingBuildError, OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        parser.exit(1, f"pylibobs macOS build failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
