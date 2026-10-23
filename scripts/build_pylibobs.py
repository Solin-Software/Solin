"""Build an audited pylibobs revision using its own runtime packaging recipe.

An immutable source revision is required. A matching local checkout can be used
before the fork is published; otherwise the revision must exist in the fork.
No binding source or native dependency is patched by this application.
"""

from __future__ import annotations

import argparse
import hashlib
import fnmatch
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

PYLIBOBS_VERSION = "32.2.2"
OBS_VERSION = "32.2.2"
SOURCE_REPOSITORY = "https://github.com/AlexsanderMe/pylibobs.git"
ARCHITECTURE_ALIASES = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}
_PLATFORMS = {"darwin": "macos", "win32": "windows", "linux": "linux"}
_LAYOUTS = {
    "macos": {
        "library": "Frameworks/libobs.framework/Versions/A/libobs",
        "suffix": ".dylib",
        "required": ("Frameworks/libobs-opengl.dylib", "obs-ffmpeg-mux"),
        "capture": ("mac-capture", "mac-avcapture"),
    },
    "windows": {
        "library": "obs.dll",
        "suffix": ".dll",
        "required": ("libobs-d3d11.dll", "obs-ffmpeg-mux.exe"),
        "capture": ("win-dshow",),
    },
    "linux": {
        "library": "libobs.so.0",
        "suffix": ".so",
        "required": ("libobs-opengl.so*",),
        "capture": ("linux-v4l2",),
    },
}
_CORE_PLUGINS = ("obs-ffmpeg", "image-source", "obs-transitions")


class BindingBuildError(RuntimeError):
    """The requested binding revision could not produce a qualified runtime."""


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, **kwargs)


def _validate_revision(source: Path, revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise BindingBuildError("--source-ref must be a full immutable Git commit SHA")
    actual = _run(
        ["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True
    ).stdout.strip()
    if actual != revision:
        raise BindingBuildError(f"Source checkout is at {actual}, expected {revision}")
    dirty = _run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=source,
        capture_output=True,
        text=True,
    )
    if dirty.stdout.strip():
        raise BindingBuildError("Commit tracked source changes before building pylibobs")


def _export_source(checkout: Path, revision: str, destination: Path) -> None:
    archive = destination.parent / "source.tar"
    _run(["git", "archive", "--format=tar", f"--output={archive}", revision], cwd=checkout)
    destination.mkdir()
    with tarfile.open(archive) as contents:
        contents.extractall(destination, filter="data")
    metadata = tomllib.loads((destination / "pyproject.toml").read_text(encoding="utf-8"))
    if metadata["project"]["version"] != PYLIBOBS_VERSION:
        raise BindingBuildError(f"The audited source must provide pylibobs {PYLIBOBS_VERSION}")


def _verify_source_runtime(source: Path, target_platform: str, architecture: str) -> None:
    environment = dict(os.environ, PYTHONPATH=str(source))
    for key in (
        "LIBOBS_PATH",
        "OBS_DATA_PATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
    ):
        environment.pop(key, None)
    layout = _LAYOUTS[target_platform]
    required_plugins = _CORE_PLUGINS + layout["capture"]
    script = f"""
from pylibobs import OBSContext, Source
from pylibobs._ffi import ffi, get_lib
from pylibobs._lib import find_libobs, get_bundled_modules
from pathlib import Path
expected = Path('pylibobs/_libs/{target_platform}/{architecture}/{layout["library"]}').resolve()
assert Path(find_libobs()).resolve() == expected
assert ffi.string(get_lib().obs_get_version_string()).decode() == {OBS_VERSION!r}
modules = {{name for name, _, _ in get_bundled_modules()}}
assert set({required_plugins!r}) <= modules
assert ffi.typeof("obs_task_t").kind == "function"
assert ffi.typeof("obs_task_handler_t").kind == "function"
assert ffi.typeof("obs_scene_atomic_update_func").kind == "function"
frame_getter = ffi.typeof(get_lib().obs_source_get_frame)
assert frame_getter.args == (ffi.typeof("obs_source_t *"),)
assert frame_getter.result == ffi.typeof("struct obs_source_frame *")
assert ffi.sizeof("struct media_frames_per_second") == 8
"""
    # Cocoa runners have a display server. Other platforms qualify graphics
    # later in their packaged application smoke, under the appropriate session.
    if target_platform == "macos":
        script += """
with OBSContext() as context:
    context.set_video(width=320, height=180, fps_num=30)
    context.set_audio()
    context.load_modules()
    for kind in ('ffmpeg_source', 'image_source', 'color_source_v3'):
        source = Source.create(kind, 'runtime-' + kind)
        try:
            assert get_lib().obs_source_get_output_flags(source._ptr) & 1, kind
        finally:
            source.release()
"""
    _run([sys.executable, "-c", script], cwd=source, env=environment, timeout=120)


def _verify_built_wheel(wheel: Path, target_platform: str, architecture: str) -> dict:
    root = f"pylibobs/_libs/{target_platform}/{architecture}/"
    layout = _LAYOUTS[target_platform]
    required = [
        layout["library"],
        "data/libobs/default.effect",
        "runtime.json",
        *layout["required"],
    ]
    required.extend(
        f"obs-plugins/{name}{layout['suffix']}" for name in _CORE_PLUGINS + layout["capture"]
    )
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        missing = [
            name
            for name in required
            if not any(fnmatch.fnmatchcase(item, root + name) for item in names)
        ]
        if target_platform == "macos" and "pylibobs/native/macos_surfaces.dylib" not in names:
            missing.append("pylibobs/native/macos_surfaces.dylib")
        if missing:
            raise BindingBuildError(
                f"Built wheel is missing runtime artifacts: {', '.join(missing)}"
            )
        runtime = json.loads(archive.read(root + "runtime.json"))
    if not isinstance(runtime, dict) or runtime.get("obs_version") != OBS_VERSION:
        raise BindingBuildError(f"The built runtime must provide OBS {OBS_VERSION}")
    return runtime


def build_wheel(
    output_dir: Path,
    cache_dir: Path,
    target_platform: str,
    architecture: str,
    *,
    source_ref: str,
    source_dir: Path | None = None,
    source_repository: str = SOURCE_REPOSITORY,
) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", source_ref):
        raise BindingBuildError("Provide --source-ref with the audited full Git commit SHA")
    output_dir = output_dir.resolve()
    cache_dir = cache_dir.resolve()
    source_dir = source_dir.resolve() if source_dir is not None else None
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pylibobs-build-") as temporary:
        work = Path(temporary)
        checkout = source_dir
        if checkout is None:
            checkout = work / "checkout"
            try:
                _run(["git", "clone", "--no-checkout", source_repository, str(checkout)])
                _run(["git", "checkout", "--detach", source_ref], cwd=checkout)
            except subprocess.CalledProcessError as exc:
                raise BindingBuildError(
                    "The pinned revision must be published in the fork, or supplied with --source-dir"
                ) from exc
        _validate_revision(checkout, source_ref)
        source = work / "source"
        _export_source(checkout, source_ref, source)
        # Reuse only the binding's SHA256-verified download cache. The binding
        # owns extraction, relocation, native build and signing.
        _run(
            [
                sys.executable,
                "scripts/fetch_libs.py",
                "--platform",
                target_platform,
                "--arch",
                architecture,
                "--version",
                OBS_VERSION,
                "--cache-dir",
                str(cache_dir),
            ],
            cwd=source,
        )
        _verify_source_runtime(source, target_platform, architecture)
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
            ]
        )
        built = list(wheels.glob(f"pylibobs-{PYLIBOBS_VERSION}-py3-none-*.whl"))
        if len(built) != 1:
            raise BindingBuildError("Expected one architecture-specific pylibobs wheel")
        runtime = _verify_built_wheel(built[0], target_platform, architecture)
        destination = output_dir / built[0].name
        with tempfile.NamedTemporaryFile(dir=output_dir, delete=False) as stream:
            staged = Path(stream.name)
        try:
            shutil.copy2(built[0], staged)
            with staged.open("rb") as stream:
                wheel_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            staged.replace(destination)
        finally:
            staged.unlink(missing_ok=True)
        provenance = {
            "source_repository": source_repository,
            "source_revision": source_ref,
            "wheel": destination.name,
            "sha256": wheel_digest,
            "runtime": runtime,
        }
        destination.with_suffix(".build.json").write_text(
            json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
        )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(".pip-wheels"))
    parser.add_argument("--cache-dir", type=Path, default=Path("build/dependencies/pylibobs"))
    parser.add_argument(
        "--source-dir", type=Path, help="Local committed pylibobs checkout matching --source-ref"
    )
    parser.add_argument(
        "--source-ref",
        default=os.environ.get("PYLIBOBS_SOURCE_REF"),
        help="Full immutable Git commit SHA",
    )
    parser.add_argument("--source-repository", default=SOURCE_REPOSITORY)
    args = parser.parse_args(argv)
    target_platform = _PLATFORMS.get(sys.platform)
    architecture = ARCHITECTURE_ALIASES.get(platform.machine().lower())
    if (target_platform, architecture) not in {
        ("macos", "x86_64"),
        ("macos", "arm64"),
        ("windows", "x86_64"),
        ("linux", "x86_64"),
    }:
        parser.exit(1, f"Unsupported runtime target: {sys.platform}/{platform.machine()}\n")
    if not args.source_ref:
        parser.exit(
            1,
            "Set --source-ref or PYLIBOBS_SOURCE_REF to the audited binding commit; use --source-dir for an unpublished checkout.\n",
        )
    try:
        print(
            build_wheel(
                args.output_dir.resolve(),
                args.cache_dir.resolve(),
                target_platform,
                architecture,
                source_ref=args.source_ref,
                source_dir=args.source_dir.resolve() if args.source_dir else None,
                source_repository=args.source_repository,
            )
        )
    except (
        BindingBuildError,
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        parser.exit(1, f"pylibobs build failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
