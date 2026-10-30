"""Stage the installed libobs runtime and qualify the packaged sidecar."""

from __future__ import annotations

import argparse
import base64
import filecmp
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from xml.parsers.expat import ExpatError

if __package__:
    from . import _windows_pe, obs_sources
else:
    import _windows_pe
    import obs_sources

from solin.core.foundation.constants import LIBOBS_SIDECAR_ARGUMENT
from solin.core.scenes.content_frame_channel import SharedFrameChannelReader
from solin.core.scenes.ipc_protocol import (
    SceneIpcEnvelope,
    SceneIpcError,
    encode_envelope,
    read_envelope,
)


class LibobsPackagingError(RuntimeError):
    """The private runtime or packaged process did not meet its contract."""


_PLATFORMS = {"win32": "windows", "linux": "linux", "darwin": "macos"}
_ARCHITECTURES = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}
_WINDOWS_HELPERS = (
    "obs-ffmpeg-mux.exe",
    "obs-amf-test.exe",
    "obs-nvenc-test.exe",
    "obs-qsv-test.exe",
)
# Keep the C runtime and hardware-facing loader stack supplied by the host.
# Other linked libraries, including FFmpeg, belong to the private runtime.
_LINUX_HOST_LIBRARIES = frozenset({
    "libc.so.6", "libm.so.6", "libdl.so.2", "librt.so.1", "libpthread.so.0",
    "libutil.so.1", "libresolv.so.2", "libGL.so.1", "libEGL.so.1",
    "libGLX.so.0", "libGLdispatch.so.0", "libOpenGL.so.0", "libglapi.so.0",
    "libgbm.so.1", "libvulkan.so.1", "libva.so.2", "libva-drm.so.2", "libva-x11.so.2",
    "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1",
})
_LINUX_DOCUMENTATION_ROOT = Path("/usr/share/doc")
# Effects loaded by OBS 32.1.2's graphics initialization, including OpenGL.
_MACOS_LIBOBS_EFFECTS = (
    "default", "default_rect", "opaque", "solid", "repeat", "format_conversion",
    "bicubic_scale", "lanczos_scale", "area", "bilinear_lowres_scale", "premultiplied_alpha",
)
_MEDIA_EXTRACT_ABI = {
    "windows": {"avcodec": 61, "avformat": 61, "avutil": 59, "swscale": 8},
    "linux": {"avcodec": 60, "avformat": 60, "avutil": 58, "swscale": 7},
    "macos": {"avcodec": 61, "avformat": 61, "avutil": 59, "swscale": 8},
}
_MEDIA_EXTRACT_MANIFEST = "media-extract-manifest.json"
_MEDIA_PLAYBACK_MANIFEST = "media-playback-manifest.json"
_OBS_RUNTIME_MANIFEST = "obs-runtime-manifest.json"
_OBS_RUNTIME_PATCHES = Path(__file__).resolve().parents[1] / "native/obs_runtime/patches"
_MEDIA_PLAYBACK_PATCHES = Path(__file__).resolve().parents[1] / "native/media_playback/patches"
_WINDOWS_REDISTRIBUTABLES = frozenset({
    "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
})
_WINDOWS_SYSTEM_IMPORTS = frozenset({
    "advapi32.dll", "bcrypt.dll", "crypt32.dll", "gdi32.dll", "kernel32.dll",
    "msvcrt.dll", "ncrypt.dll", "ole32.dll", "oleaut32.dll", "secur32.dll",
    "shell32.dll", "user32.dll", "version.dll", "winmm.dll", "ws2_32.dll",
    "ucrtbase.dll",
})


def media_extract_name(target_platform: str) -> str:
    return "solin-media-extract.exe" if target_platform == "windows" else "solin-media-extract"


def _stage_windows_redists(root: Path, application_dir: Path) -> None:
    """Reuse Nuitka's existing redistributables beside the private helper."""
    required = {
        name.casefold()
        for binary in [root / media_extract_name("windows"), *root.glob("*.dll")]
        for name in _windows_pe.pe_imports(binary)
        if name.casefold() in _WINDOWS_REDISTRIBUTABLES
    }
    for name in required:
        destination = root / name
        if not destination.is_file():
            source = application_dir / name
            _require_file(source)
            shutil.copy2(source, destination)


def _audit_windows_media_extract(root: Path, helper: Path, architecture: str) -> None:
    expected = {f"{name}-{major}.dll" for name, major in _MEDIA_EXTRACT_ABI["windows"].items()}
    imports = {name.casefold() for name in _windows_pe.pe_imports(helper)}
    if not imports or any(name.startswith("obs") for name in imports):
        raise LibobsPackagingError("Extraction helper must run independently of OBS")
    for name in expected:
        _require_file(root / name)
    pending = [helper, *(root / name for name in expected)]
    visited: set[Path] = set()
    while pending:
        binary = pending.pop()
        if binary in visited:
            continue
        visited.add(binary)
        if architecture != "x86_64" or _windows_pe.pe_machine(binary) != 0x8664:
            raise LibobsPackagingError(f"Invalid extraction runtime PE architecture: {binary}")
        for name in _windows_pe.pe_imports(binary):
            lowered = name.casefold()
            if lowered in _WINDOWS_SYSTEM_IMPORTS or lowered.startswith(("api-ms-win-", "ext-ms-win-")):
                continue
            dependency = root / name
            if dependency.is_file():
                if not dependency.resolve().is_relative_to(root.resolve()):
                    raise LibobsPackagingError(f"Extraction dependency escapes its runtime: {name}")
                pending.append(dependency)
            elif lowered not in _WINDOWS_REDISTRIBUTABLES:
                raise LibobsPackagingError(f"Unbundled extraction dependency: {name}")


def stage_media_extract_dependencies(root: Path, target_platform: str) -> None:
    """Fill missing Linux dependencies from the installed archive, never replace wheel libraries."""
    if target_platform == "macos":
        for name, major in _MEDIA_EXTRACT_ABI["macos"].items():
            alias = root / "Frameworks" / f"lib{name}.{major}.dylib"
            if not alias.exists():
                canonical = alias.with_name(f"lib{name}.dylib")
                _require_file(canonical)
                alias.symlink_to(canonical.name)
        return
    if target_platform != "linux":
        return
    result = subprocess.run(
        ["pkg-config", "--variable=libdir", "libavformat"],
        check=True, capture_output=True, text=True,
    )
    libdir = Path(result.stdout.strip())
    pending = [
        (f"lib{name}.so.{major}", libdir / f"lib{name}.so.{major}")
        for name, major in _MEDIA_EXTRACT_ABI["linux"].items()
    ]
    visited: set[str] = set()
    while pending:
        name, source = pending.pop()
        if name in visited or _linux_host_library(name):
            continue
        visited.add(name)
        destination = root / name
        if destination.exists():
            _require_file(destination)
            continue
        _require_file(source)
        dependencies = _linux_linked_libraries(source)
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
            candidate = Path(stream.name)
        try:
            shutil.copy2(source, candidate)
            _set_linux_rpath(root, candidate)
            candidate.replace(destination)
        finally:
            candidate.unlink(missing_ok=True)
        _copy_linux_dependency_notice(source, root)
        pending.extend(dependencies.items())


def relocate_media_extract(root: Path, helper: Path, target_platform: str, architecture: str) -> None:
    """Bind the helper to one private native dependency graph before qualification."""
    if target_platform == "windows":
        _audit_windows_media_extract(root, helper, architecture)
    elif target_platform == "linux":
        _relocate_linux(root, binaries=[helper])
        for name, major in _MEDIA_EXTRACT_ABI["linux"].items():
            binary = root / f"lib{name}.so.{major}"
            _require_file(binary)
            for dependency, library in _linux_linked_libraries(binary).items():
                if not _linux_host_library(dependency) and not library.is_relative_to(root.resolve()):
                    raise LibobsPackagingError(f"Extraction dependency escaped the private runtime: {dependency}")
    elif target_platform == "macos":
        result = subprocess.run(
            ["lipo", str(helper), "-verify_arch", architecture], check=True, capture_output=True,
        )
        if result.returncode != 0:
            raise LibobsPackagingError(f"Invalid extraction helper architecture: {helper}")
        _relocate_macos(root, binaries=[helper])
    else:
        raise LibobsPackagingError(f"Unsupported extraction platform: {target_platform}")


def _media_extract_environment(target_platform: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH",
                 "DYLD_INSERT_LIBRARIES", "DISPLAY", "WAYLAND_DISPLAY", "LIBOBS_PATH", "OBS_DATA_PATH",
                 "SOLIN_MEDIA_EXTRACT_FFMPEG_DIR"):
        environment.pop(name, None)
    environment["PATH"] = (
        str(Path(environment.get("SystemRoot", "C:/Windows")) / "System32")
        if target_platform == "windows" else ""
    )
    return environment


def _run_media_extract(command: list[str], *, environment: dict[str, str], cwd: Path) -> tuple[dict, bytes]:
    result = subprocess.run(command, capture_output=True, timeout=35, env=environment, cwd=cwd)
    if result.returncode != 0:
        diagnostics = result.stderr or result.stdout.partition(b"\n")[0]
        raise LibobsPackagingError(
            f"Extraction helper failed qualification (exit {result.returncode}): "
            + diagnostics.decode("utf-8", errors="replace")[-4000:]
        )
    header, separator, pixels = result.stdout.partition(b"\n")
    try:
        metadata = json.loads(header)
    except (UnicodeDecodeError, ValueError) as error:
        raise LibobsPackagingError("Extraction helper returned invalid JSON") from error
    if (not separator or not isinstance(metadata, dict) or metadata.get("protocol") != 1
            or metadata.get("status") != "ok"):
        raise LibobsPackagingError("Extraction helper protocol is unsupported")
    return metadata, pixels


def _verify_media_extract_abi(version: dict, root: Path, target_platform: str) -> None:
    abi = version.get("abi")
    expected = _MEDIA_EXTRACT_ABI[target_platform]
    if not isinstance(abi, dict) or set(abi) != set(expected):
        raise LibobsPackagingError("Extraction helper FFmpeg ABI inventory is incomplete")
    for name, major in expected.items():
        entry = abi[name]
        if not isinstance(entry, dict):
            raise LibobsPackagingError("Extraction helper FFmpeg ABI record is invalid")
        compiled, runtime = entry.get("compiled"), entry.get("runtime")
        if (type(compiled) is not int or type(runtime) is not int or compiled >> 16 != major
                or runtime >> 16 != major or runtime < compiled):
            raise LibobsPackagingError("Extraction helper FFmpeg ABI does not match the packaged runtime")
        filename = (
            f"{name}-{major}.dll" if target_platform == "windows"
            else f"lib{name}.so.{major}" if target_platform == "linux"
            else f"Frameworks/lib{name}.{major}.dylib"
        )
        library = entry.get("library")
        wanted = root / filename
        _require_file(wanted)
        if (not isinstance(library, str) or not Path(library).is_absolute()
                or Path(library).resolve() != wanted.resolve()
                or not wanted.resolve().is_relative_to(root.resolve())):
            raise LibobsPackagingError(f"Extraction helper loaded an unbundled FFmpeg library: {name}")


def verify_media_extract(
    helper: Path, *, target_platform: str, qualification_fixtures: Path | None = None,
    architecture: str | None = None,
) -> dict:
    """Exercise the actual helper without FFmpeg executables, OBS or graphics."""
    _require_file(helper)
    environment = _media_extract_environment(target_platform)
    with tempfile.TemporaryDirectory(prefix="solin-extraction-smoke-") as directory:
        work = Path(directory)
        version, extra = _run_media_extract(
            [str(helper.resolve()), "--version"], environment=environment, cwd=work,
        )
        if extra:
            raise LibobsPackagingError("Extraction helper version must not contain image bytes")
        _verify_media_extract_abi(version, helper.parent, target_platform)
        title = "Solin — ação 🎵"
        audio = work / "canção.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\x00\x00" * 16000)
        tag = title.encode("utf-8") + b"\x00"
        info = b"INFOINAM" + struct.pack("<I", len(tag)) + tag
        info += b"\x00" * (len(tag) % 2)
        data = audio.read_bytes() + b"LIST" + struct.pack("<I", len(info)) + info
        audio.write_bytes(data[:4] + struct.pack("<I", len(data) - 8) + data[8:])
        options = ["--width", "2", "--height", "2", "--title", "1", "--duration", "1"]
        metadata, pixels = _run_media_extract(
            [str(helper.resolve()), "--source", str(audio), "--image-policy", "none", *options],
            environment=environment, cwd=work,
        )
        if (metadata.get("title") != title or metadata.get("duration_ms") != 2000
                or metadata.get("has_video") is not False or pixels):
            raise LibobsPackagingError("Extraction helper did not read the smoke title and duration")
        image = work / "imagem.png"
        image.write_bytes(_smoke_png())
        metadata, pixels = _run_media_extract(
            [str(helper.resolve()), "--source", str(image), "--image-policy", "frame", *options],
            environment=environment, cwd=work,
        )
        expected_image = {"width": 2, "height": 2, "stride": 8, "kind": "frame", "bytes": 16}
        if metadata.get("image") != expected_image or pixels != b"\xff" * 16:
            raise LibobsPackagingError("Extraction helper did not decode the smoke image to BGRA")
        fixtures = qualification_fixtures or (
            Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "media_extract"
        )
        for filename, policy, title, has_video in (
            ("video.mp4", "frame", "Canção — ação 🎵", True),
            ("video-cover.mp4", "cover", None, True),
            ("audio-cover.mp3", "cover", None, False),
        ):
            source = fixtures / filename
            _require_file(source)
            metadata, pixels = _run_media_extract(
                [str(helper.resolve()), "--source", str(source.resolve()),
                 "--image-policy", policy, *options],
                environment=environment, cwd=work,
            )
            image_metadata = metadata.get("image")
            if (not isinstance(image_metadata, dict) or image_metadata.get("kind") != policy
                    or image_metadata.get("bytes") != len(pixels) or not pixels
                    or (title is not None and metadata.get("title") != title)
                    or metadata.get("has_video") is not has_video
                    or type(metadata.get("duration_ms")) is not int or metadata["duration_ms"] <= 0):
                raise LibobsPackagingError(f"Extraction helper did not qualify the media fixture: {filename}")
            width, height, stride = (image_metadata.get(key) for key in ("width", "height", "stride"))
            if (type(width) is not int or type(height) is not int or type(stride) is not int
                    or not 1 <= width <= 2 or not 1 <= height <= 2 or stride != width * 4
                    or len(pixels) != height * stride):
                raise LibobsPackagingError(f"Extraction helper returned invalid fixture geometry: {filename}")
            offset = (height // 2) * stride + (width // 2) * 4
            blue, green, red, alpha = pixels[offset:offset + 4]
            if (alpha != 255 or blue >= 25
                    or (policy == "frame" and (red <= 230 or green >= 25))
                    or (policy == "cover" and (green <= 190 or red >= 25))):
                raise LibobsPackagingError(f"Extraction helper returned invalid fixture pixels: {filename}")
    return {
        "protocol": 1,
        "target": {
            "platform": target_platform,
            "architecture": architecture or _ARCHITECTURES.get(platform.machine().lower()),
        },
        "helper": {
            "path": media_extract_name(target_platform),
            "sha256": _native_library_digest(helper).hex(),
        },
        "libraries": {
            name: {
                "path": Path(entry["library"]).resolve().relative_to(helper.parent.resolve()).as_posix(),
                "sha256": _native_library_digest(Path(entry["library"])).hex(),
                "compiled": entry["compiled"],
                "runtime": entry["runtime"],
            }
            for name, entry in version["abi"].items()
        },
        "qualified": ["private_abi_origins", "unicode_title", "duration", "bgra_pixels", "embedded_covers"],
    }


def _mux_helper(root: Path, target_platform: str) -> Path:
    name = "obs-ffmpeg-mux.exe" if target_platform == "windows" else "obs-ffmpeg-mux"
    direct = root / name
    if direct.is_file():
        return direct
    candidates = [path for path in root.rglob(name) if path.is_file()]
    if len(candidates) != 1:
        raise LibobsPackagingError(f"Required libobs mux helper is missing or ambiguous: {direct}")
    return candidates[0]


def _require_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise LibobsPackagingError(f"Required libobs runtime file is missing or empty: {path}")


def _validate_macos_links(root: Path) -> None:
    """Only preserve relative, live links contained in this relocatable runtime."""
    resolved_root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_symlink():
            continue
        try:
            target = path.resolve(strict=True)
        except OSError as exc:
            raise LibobsPackagingError(f"Invalid libobs runtime link: {path}") from exc
        if not target.is_relative_to(resolved_root):
            raise LibobsPackagingError(f"libobs runtime link escapes its bundle: {path}")
        if path.readlink().is_absolute():
            raise LibobsPackagingError(f"libobs runtime link must be relative: {path}")
        if target.is_dir() and path.parent.resolve().is_relative_to(target):
            raise LibobsPackagingError(f"Cyclic libobs runtime directory link: {path}")


def _framework_alias_matches(alias: Path, canonical: Path) -> bool:
    """Accept intact links or identical copies produced by wheel installation."""
    if alias.is_symlink():
        return alias.resolve() == canonical.resolve()
    if alias.is_dir() and canonical.is_dir():
        names = {path.name for path in canonical.iterdir()}
        return names == {path.name for path in alias.iterdir()} and all(
            _framework_alias_matches(alias / name, canonical / name) for name in names
        )
    return alias.is_file() and canonical.is_file() and filecmp.cmp(
        alias, canonical, shallow=False,
    )


def _macos_framework_aliases(root: Path) -> list[tuple[Path, Path, str]]:
    """Validate the pinned single-version frameworks and describe their aliases."""
    aliases: list[tuple[Path, Path, str]] = []
    for framework in sorted((root / "Frameworks").glob("*.framework")):
        versions = framework / "Versions"
        candidates = (
            [path for path in versions.iterdir() if path.name != "Current"]
            if versions.is_dir() else []
        )
        if len(candidates) != 1 or not candidates[0].is_dir() or candidates[0].is_symlink():
            raise LibobsPackagingError(f"Expected one canonical framework version: {framework}")
        version = candidates[0]
        metadata_path = version / "Resources" / "Info.plist"
        _require_file(metadata_path)
        try:
            with metadata_path.open("rb") as stream:
                metadata = plistlib.load(stream)
        except (plistlib.InvalidFileException, ValueError, ExpatError) as exc:
            raise LibobsPackagingError(f"Invalid framework metadata: {metadata_path}") from exc
        if not isinstance(metadata, dict):
            raise LibobsPackagingError(f"Invalid framework metadata: {metadata_path}")
        executable = metadata.get("CFBundleExecutable")
        identifier = metadata.get("CFBundleIdentifier")
        if (
            not isinstance(executable, str)
            or not executable
            or executable in {".", ".."}
            or "/" in executable
            or "\\" in executable
            or not isinstance(identifier, str)
            or not identifier
            or metadata.get("CFBundlePackageType") != "FMWK"
        ):
            raise LibobsPackagingError(f"Invalid framework metadata: {metadata_path}")
        _require_file(version / executable)
        if framework.name == "libobs.framework":
            if (
                version.name != "A"
                or executable != "libobs"
                or identifier != "com.obsproject.libobs"
            ):
                raise LibobsPackagingError(f"Unexpected pinned libobs framework metadata: {metadata_path}")
            for effect in _MACOS_LIBOBS_EFFECTS:
                _require_file(version / "Resources" / f"{effect}.effect")
        framework_aliases = [(versions / "Current", version, version.name)]
        framework_aliases.extend(
            (framework / name, version / name, f"Versions/Current/{name}")
            for name in (executable, "Resources", "Headers", "Modules")
            if (version / name).exists()
        )
        for alias, canonical, relative in framework_aliases:
            if (alias.exists() or alias.is_symlink()) and not _framework_alias_matches(alias, canonical):
                raise LibobsPackagingError(f"Conflicting framework alias: {alias}")
            aliases.append((alias, canonical, relative))
    return aliases


def _normalize_macos_frameworks(root: Path) -> None:
    """Rebuild wheel-expanded aliases in staging without changing the installation."""
    _validate_macos_links(root)
    for alias, canonical, relative in _macos_framework_aliases(root):
        if alias.is_symlink():
            continue
        if alias.is_dir():
            shutil.rmtree(alias)
        else:
            alias.unlink(missing_ok=True)
        alias.symlink_to(relative, target_is_directory=canonical.is_dir())


def _native_build_receipt(
    root: Path, manifest_name: str, binary: Path, artifact: str, identity: dict,
    label: str, *, rehash: bool = False,
) -> None:
    """Verify a built native artifact; reseal only after owned relocation."""
    manifest = root / manifest_name
    _require_file(manifest)
    _require_file(binary)
    try:
        receipt = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LibobsPackagingError(f"Invalid native {label} receipt") from error
    if (
        not isinstance(receipt, dict)
        or type(receipt.get("schema")) is not int or receipt["schema"] != 1
        or any(receipt.get(key) != expected for key, expected in identity.items())
        or not isinstance(receipt.get(artifact), dict)
        or receipt[artifact].get("path") != binary.relative_to(root).as_posix()
    ):
        raise LibobsPackagingError(f"Native {label} receipt does not identify the binary")
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if rehash:
        receipt[artifact]["sha256"] = digest
        manifest.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    elif receipt[artifact].get("sha256") != digest:
        raise LibobsPackagingError(f"Native {label} binary differs from its build receipt")


def _media_playback_receipt(root: Path, suffix: str, *, rehash: bool = False) -> None:
    _native_build_receipt(
        root, _MEDIA_PLAYBACK_MANIFEST, root / "obs-plugins" / f"solin-media-source{suffix}",
        "plugin",
        {
            "source_id": "solin_media_source",
            "obs_version": obs_sources.OBS_VERSION,
            "source_sha256": obs_sources.SOURCE_SHA256,
            "patches": obs_sources.patch_receipts(_MEDIA_PLAYBACK_PATCHES),
        },
        "media playback", rehash=rehash,
    )


def validate_runtime(
    root: Path, target_platform: str, *, linux_mux_helper: Path | None = None,
) -> None:
    """Validate resources loaded dynamically, beyond Nuitka's import graph."""
    if target_platform == "windows":
        libraries = ("obs.dll", "libobs-d3d11.dll")
        suffix = ".dll"
    elif target_platform == "linux":
        libraries = ("libobs.so.0", "libobs-opengl.so")
        suffix = ".so"
    elif target_platform == "macos":
        _validate_macos_links(root)
        _macos_framework_aliases(root)
        libraries = (
            "Frameworks/libobs.framework/Versions/A/libobs",
            "Frameworks/libobs-opengl.dylib",
        )
        suffix = ".dylib"
    else:
        raise LibobsPackagingError(f"Unsupported libobs platform: {target_platform}")
    _require_file(root / media_extract_name(target_platform))
    if linux_mux_helper is not None and target_platform != "linux":
        raise LibobsPackagingError("An external mux helper is only supported for Linux.")
    _require_file(linux_mux_helper if linux_mux_helper is not None else _mux_helper(root, target_platform))
    for name in libraries:
        _require_file(root / name)
    for name in ("obs-ffmpeg", "image-source", "obs-transitions", "solin-media-source"):
        _require_file(root / "obs-plugins" / f"{name}{suffix}")
    _media_playback_receipt(root, suffix)
    if target_platform == "windows":
        _require_file(root / "obs-plugins" / "win-wasapi.dll")
        _native_build_receipt(
            root, _OBS_RUNTIME_MANIFEST, root / "obs.dll", "library",
            {"obs_version": obs_sources.OBS_VERSION, "source_sha256": obs_sources.SOURCE_SHA256,
             "patches": obs_sources.patch_receipts(_OBS_RUNTIME_PATCHES)},
            "OBS core",
        )
        _require_file(root / "data/solin-obs-runtime/COPYING")
    _require_file(root / "data" / "libobs" / "default.effect")
    _require_file(root / "data" / "obs-plugins" / "obs-ffmpeg" / "locale" / "en-US.ini")


def _native_files(root: Path) -> list[Path]:
    result: list[Path] = []
    visited: set[Path] = set()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            path = path.resolve()
            if path in visited:
                continue
            visited.add(path)
            with path.open("rb") as stream:
                magic = stream.read(4)
            if magic in (
                b"\x7fELF",
                b"\xcf\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xce",
                b"\xca\xfe\xba\xbe",
                b"\xbe\xba\xfe\xca",
                b"\xca\xfe\xba\xbf",
                b"\xbf\xba\xfe\xca",
            ):
                result.append(path)
    return result


def _linux_host_library(name: str) -> bool:
    return name in _LINUX_HOST_LIBRARIES or name.startswith(("libdrm", "libnss_"))


def _linux_needed_libraries(binary: Path) -> frozenset[str]:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = subprocess.run(
        ["readelf", "--dynamic", "--wide", str(binary)],
        check=True,
        capture_output=True,
        env=environment,
    )
    needed = set()
    marker = "Shared library: ["
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if "(NEEDED)" not in line:
            continue
        start = line.find(marker)
        end = line.find("]", start + len(marker)) if start >= 0 else -1
        if start < 0 or end < 0:
            raise LibobsPackagingError(f"Invalid ELF dependency record in {binary}: {line.strip()}")
        name = line[start + len(marker):end]
        if not name or Path(name).name != name:
            raise LibobsPackagingError(f"Invalid shared-library name: {name}")
        needed.add(name)
    return frozenset(needed)


def _linux_linked_libraries(
    binary: Path, *, private_root: Path | None = None,
) -> dict[str, Path]:
    needed = _linux_needed_libraries(binary)
    environment = dict(os.environ)
    environment.pop("LD_LIBRARY_PATH", None)
    environment.pop("LD_PRELOAD", None)
    environment["LC_ALL"] = "C"
    if private_root is not None:
        private_root = private_root.resolve()
        resolved_binary = binary.resolve()
        search_paths = [private_root]
        if resolved_binary.is_relative_to(private_root):
            search_paths = list(dict.fromkeys((resolved_binary.parent, private_root)))
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(map(str, search_paths))
    result = subprocess.run(
        ["ldd", str(binary)], check=True, capture_output=True, env=environment,
    )
    if b"not found" in result.stdout or b"not found" in result.stderr:
        raise LibobsPackagingError(
            f"Unresolved libobs dependencies in {binary}: "
            + (result.stdout + result.stderr).decode("utf-8", errors="replace")
        )
    libraries = {}
    for line in result.stdout.decode("utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "=>" and fields[2].startswith("/"):
            name = fields[0]
            if name not in needed:
                continue
            if Path(name).name != name:
                raise LibobsPackagingError(f"Invalid shared-library name: {name}")
            libraries[name] = Path(fields[2]).resolve()
        elif len(fields) >= 2 and fields[0].startswith("/"):
            library = Path(fields[0]).resolve()
            if library.name in needed:
                libraries[library.name] = library
    missing = sorted(needed.difference(libraries))
    if missing:
        raise LibobsPackagingError(
            f"Unresolved direct libobs dependencies in {binary}: {', '.join(missing)}"
        )
    return libraries


def _copy_linux_dependency_notice(library: Path, root: Path) -> None:
    """Preserve the distribution's copyright notice for each bundled dependency."""
    candidates = [library.as_posix()]
    # Older package records may retain /lib paths on a merged-/usr host.
    if candidates[0].startswith("/usr/lib/"):
        candidates.append(candidates[0].removeprefix("/usr"))
    owner = []
    for candidate in candidates:
        result = subprocess.run(
            ["dpkg-query", "--search", candidate], check=False, capture_output=True,
        )
        if result.returncode == 0:
            owner = result.stdout.decode("utf-8").splitlines()
            break
    if not owner or ": " not in owner[0]:
        raise LibobsPackagingError(f"No distribution package owns {library}")
    package = owner[0].split(": ", 1)[0].split(":", 1)[0]
    if not package or Path(package).name != package:
        raise LibobsPackagingError(f"Invalid dependency package name: {package}")
    notice = _LINUX_DOCUMENTATION_ROOT / package / "copyright"
    _require_file(notice)
    destination = root / "licenses" / "system" / package / "copyright"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(notice, destination)


def _set_linux_rpath(root: Path, binary: Path, *, include_binary_directory: bool = True) -> None:
    relative = os.path.relpath(root, binary.parent).replace(os.sep, "/")
    rpath = f"$ORIGIN:$ORIGIN/{relative}" if include_binary_directory else f"$ORIGIN/{relative}"
    subprocess.run(
        ["patchelf", "--set-rpath", rpath, str(binary)],
        check=True,
        capture_output=True,
    )


def _relocate_linux(root: Path, *, binaries: Sequence[Path] | None = None) -> None:
    """Stage one deterministic ELF closure while retaining host C/graphics libraries."""
    root_resolved = root.resolve()
    if binaries is not None:
        targets = list(dict.fromkeys(path.resolve() for path in binaries))
        for binary in targets:
            # A helper copied beside the application must not search its own
            # directory before the private libobs runtime. Nuitka may leave
            # flat copies of shared libraries there, which would otherwise
            # shadow the qualified runtime we just staged.
            _set_linux_rpath(
                root,
                binary,
                include_binary_directory=binary.is_relative_to(root_resolved),
            )
        for binary in targets:
            for name, library in _linux_linked_libraries(binary).items():
                if _linux_host_library(name):
                    continue
                if not library.is_relative_to(root_resolved):
                    raise LibobsPackagingError(f"Mux helper dependency was not bundled: {name}")
        return

    initial = sorted(_native_files(root), key=lambda path: path.as_posix())
    # Discover every originally staged binary before copying any host dependency.
    # The search path contains only immutable staged files, so sibling libobs
    # libraries resolve without letting an earlier copied dependency hide a
    # conflicting provider needed by a later plugin.
    discovered = {
        binary.resolve(): _linux_linked_libraries(binary, private_root=root_resolved)
        for binary in initial
    }
    # Fingerprint immutable payloads before patchelf changes ELF bytes. Comparing
    # a relocated copy with its original creates false SONAME conflicts.
    identities = {path.resolve(): _native_library_digest(path) for path in initial}
    pending: list[tuple[Path, dict[str, Path]]] = []
    for binary in initial:
        resolved = binary.resolve()
        _set_linux_rpath(root, resolved)
        pending.append((resolved, discovered[resolved]))
    visited: set[Path] = set()
    while pending:
        binary, libraries = pending.pop(0)
        if binary in visited:
            continue
        visited.add(binary)
        for name, library in sorted(libraries.items()):
            if _linux_host_library(name):
                continue
            if library.is_relative_to(root_resolved):
                continue
            destination = root / name
            _require_file(library)
            if destination.exists():
                if not destination.is_file():
                    raise LibobsPackagingError(f"Private libobs dependency is not a file: {name}")
                if identities.get(destination.resolve()) != _native_library_digest(library):
                    raise LibobsPackagingError(f"Conflicting private libobs dependency: {name}")
                _copy_linux_dependency_notice(library, root)
                continue
            # Expand new host dependencies against the canonical private graph.
            # A previous bootstrap may already have relocated their providers;
            # probing the host graph would compare patched ELF bytes to originals.
            dependency_libraries = _linux_linked_libraries(library, private_root=root_resolved)
            shutil.copy2(library, destination)
            identities[destination.resolve()] = _native_library_digest(library)
            _copy_linux_dependency_notice(library, root)
            _set_linux_rpath(root, destination)
            pending.append((destination.resolve(), dependency_libraries))
            pending.sort(key=lambda item: item[0].as_posix())

    # Re-resolve after the private RPATH is final. Every non-host SONAME must
    # now bind inside the staged runtime; this also catches ABI/version errors.
    for binary in sorted(visited, key=lambda path: path.as_posix()):
        for name, library in _linux_linked_libraries(binary).items():
            if _linux_host_library(name):
                continue
            if not library.is_relative_to(root_resolved):
                raise LibobsPackagingError(
                    f"Private libobs dependency escaped the staged runtime: "
                    f"{binary} -> {name} => {library}"
                )


def _native_library_digest(binary: Path) -> bytes:
    with binary.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def _relocate_macos(root: Path, *, binaries: Sequence[Path] | None = None) -> None:
    """Resolve OBS's framework paths relative to each staged Mach-O loader."""
    inventory = _native_files(root)
    root_resolved = root.resolve()
    frameworks = (root / "Frameworks").resolve()
    targets = (
        inventory if binaries is None
        else list(dict.fromkeys(path.resolve() for path in binaries))
    )
    for binary in targets:
        output = subprocess.run(
            ["otool", "-L", str(binary)],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        for line in output.splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith(("/System/Library/", "/usr/lib/")):
                continue
            preserve_dependency = False
            if dependency.startswith("@loader_path/"):
                target = (binary.parent / dependency.removeprefix("@loader_path/")).resolve()
                preserve_dependency = target.is_file()
            elif dependency.startswith("@rpath/"):
                target = root / "Frameworks" / dependency.removeprefix("@rpath/")
                preserve_dependency = target.is_file()
            elif "/Frameworks/" in dependency:
                target = root / "Frameworks" / dependency.split("/Frameworks/", 1)[1]
            else:
                target = root / "Frameworks" / Path(dependency).name
            if not target.is_file():
                candidates = [p for p in inventory if p.name == Path(dependency).name]
                if len(candidates) != 1:
                    raise LibobsPackagingError(
                        f"Unresolved libobs dependency: {binary} -> {dependency}"
                    )
                target = candidates[0]
            target = target.resolve()
            if not target.is_relative_to(root_resolved):
                raise LibobsPackagingError(f"libobs dependency escapes its bundle: {dependency}")
            if target == binary.resolve():  # the dylib's own install name
                continue
            if preserve_dependency:
                continue
            if target.is_relative_to(frameworks):
                relocated = f"@rpath/{target.relative_to(frameworks).as_posix()}"
            else:
                relative = os.path.relpath(target, binary.parent).replace(os.sep, "/")
                relocated = f"@loader_path/{relative}"
            if relocated != dependency:
                subprocess.run(
                    ["install_name_tool", "-change", dependency, relocated, str(binary)],
                    check=True,
                    capture_output=True,
                )
        # A mux helper copied next to the host must not retain the wheel-root
        # @loader_path/Frameworks search path. Normalize LC_RPATH as well as imports.
        load_commands = subprocess.run(
            ["otool", "-l", str(binary)],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        paths = [
            line.strip()[5:].split(" (offset", 1)[0]
            for line in load_commands.splitlines()
            if line.strip().startswith("path ") and " (offset" in line
        ]
        relative = os.path.relpath(root / "Frameworks", binary.parent).replace(os.sep, "/")
        rpath = "@loader_path" if relative == "." else f"@loader_path/{relative}"
        for previous in paths:
            if previous != rpath:
                subprocess.run(
                    ["install_name_tool", "-delete_rpath", previous, str(binary)],
                    check=True,
                    capture_output=True,
                )
        if rpath not in paths:
            subprocess.run(
                ["install_name_tool", "-add_rpath", rpath, str(binary)],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(binary)], check=True, capture_output=True
        )


def _retry_windows_sharing(operation: Callable[[], None]) -> None:
    """Bound retries for transient Windows handles; propagate other filesystem errors."""
    for delay in (0.05, 0.1, 0.2, 0.4, 0.8, None):
        try:
            operation()
            return
        except PermissionError as error:
            if os.name != "nt" or error.winerror not in (5, 32, 33) or delay is None:
                raise
            time.sleep(delay)


def _publish_runtime(staging: Path, destination: Path) -> None:
    """Replace the complete runtime, restoring the previous payload on publish failure."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.with_name(f".{destination.name}-{uuid.uuid4().hex}.backup")
    had_destination = destination.exists()
    if had_destination:
        _retry_windows_sharing(lambda: os.replace(destination, backup))
    try:
        _retry_windows_sharing(lambda: os.replace(staging, destination))
    except OSError:
        if had_destination:
            try:
                _retry_windows_sharing(lambda: os.replace(backup, destination))
            except OSError as error:
                raise LibobsPackagingError(
                    f"Runtime replacement and rollback failed; previous runtime remains at {backup}"
                ) from error
        raise
    if had_destination:
        _retry_windows_sharing(lambda: shutil.rmtree(backup))


def stage_runtime(
    *,
    package_dir: Path,
    application_dir: Path,
    target_platform: str,
    architecture: str,
    license_files: Sequence[Path],
    linux_mux_helper: Path | None = None,
    qualification_fixtures: Path | None = None,
) -> Path:
    """Copy one installed target without leaving references to the build machine."""
    source = package_dir / "_libs" / target_platform / architecture
    validate_runtime(source, target_platform, linux_mux_helper=linux_mux_helper)
    if not application_dir.is_dir():
        raise LibobsPackagingError(f"Application directory does not exist: {application_dir}")
    if not license_files:
        raise LibobsPackagingError("The installed pylibobs distribution has no license notice.")
    for notice in license_files:
        _require_file(notice)
    solin_notices = [Path(__file__).resolve().parents[1] / name for name in ("LICENSE", "NOTICE")]
    for notice in solin_notices:
        _require_file(notice)
    destination = application_dir / "pylibobs" / "_libs" / target_platform / architecture
    if not destination.resolve().is_relative_to(application_dir.resolve()):
        raise LibobsPackagingError("The runtime destination must stay inside the application.")
    if source.resolve() == destination.resolve() or application_dir.resolve().is_relative_to(
        source.resolve()
    ):
        raise LibobsPackagingError("The runtime source must be outside the destination.")
    # Stage fresh files so a repeated build cannot retain plugins from an older runtime.
    with tempfile.TemporaryDirectory(prefix=".libobs-stage-", dir=application_dir) as directory:
        staging = Path(directory) / architecture
        shutil.copytree(source, staging, symlinks=target_platform == "macos")
        if linux_mux_helper is not None:
            shutil.copy2(linux_mux_helper, staging / "obs-ffmpeg-mux")
        if target_platform == "linux":
            _relocate_linux(staging)
        elif target_platform == "macos":
            _normalize_macos_frameworks(staging)
            _relocate_macos(staging)
        else:
            _stage_windows_redists(staging, application_dir)
            _audit_windows_media_extract(staging, staging / media_extract_name(target_platform), architecture)
        # The source receipt was verified before copying. Relocation can alter
        # Mach-O/ELF bytes while preserving the same qualified native code.
        _media_playback_receipt(
            staging, {"windows": ".dll", "linux": ".so", "macos": ".dylib"}[target_platform],
            rehash=True,
        )
        validate_runtime(staging, target_platform)
        receipt = verify_media_extract(
            staging / media_extract_name(target_platform), target_platform=target_platform,
            qualification_fixtures=qualification_fixtures,
            architecture=architecture,
        )
        if not isinstance(receipt, dict):
            raise LibobsPackagingError("Extraction qualification did not produce a receipt")
        (staging / _MEDIA_EXTRACT_MANIFEST).write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        _publish_runtime(staging, destination)
    notices = application_dir / "licenses" / "pylibobs"
    notices.mkdir(parents=True, exist_ok=True)
    for index, notice in enumerate(license_files):
        shutil.copy2(notice, notices / f"{index}-{notice.name}")
    application_notices = application_dir / "licenses" / "solin"
    application_notices.mkdir(parents=True, exist_ok=True)
    for notice in solin_notices:
        shutil.copy2(notice, application_notices / notice.name)
    if target_platform == "windows":
        # OBS locates these via the host executable, including read-only installs.
        for name in _WINDOWS_HELPERS:
            helper = (
                _mux_helper(destination, target_platform)
                if name == "obs-ffmpeg-mux.exe"
                else destination / name
            )
            if helper.is_file():
                shutil.copy2(helper, application_dir / name)
    else:
        # obs-ffmpeg resolves its mux process next to the host executable on all
        # platforms. Relocate the copy against the private runtime, not a system OBS.
        helper = application_dir / "obs-ffmpeg-mux"
        shutil.copy2(_mux_helper(destination, target_platform), helper)
        helper.chmod(helper.stat().st_mode | 0o111)
        if target_platform == "macos":
            _relocate_macos(destination, binaries=[helper])
        else:
            _relocate_linux(destination, binaries=[helper])
    return destination


def _smoke_png() -> bytes:
    """A verified 2×2 opaque white RGB PNG, generated once with Pillow."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAE0lEQVR4nGP8//8/AwMDEwMYAAAkBgMBXaJOiAAAAABJRU5ErkJggg=="
    )


def _decoded_media_event(response: SceneIpcEnvelope, path: str) -> bool:
    payload = response.payload
    return (
        response.message_type == "media_playback_state"
        and payload.get("path") == path
        and type(payload.get("slot")) is int
        and payload["slot"] == 0
        and type(payload.get("state")) is int
        and payload["state"] in (1, 6)
        and type(payload.get("duration_ms")) is int
        and payload["duration_ms"] > 0
        and type(payload.get("position_ms")) is int
        and payload["position_ms"] > 0
        and not payload.get("error_code")
    )


def _run_preview_smoke(
    command: list[str],
    *,
    requests: Sequence[SceneIpcEnvelope],
    timeout: float,
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[bytes]:
    """Keep IPC open until image pixels and decoded media progress are confirmed."""
    preview = SharedFrameChannelReader(None, 32, 18, create=True)
    try:
        requests[1].payload["preview_egress"] = {
            "transport": "shared_memory_bgra",
            "handle_token": preview.name,
            "width": 32,
            "height": 18,
        }
        requests[1].payload["render_enabled"] = {"editor": True}
        with (
            tempfile.TemporaryFile() as errors,
            subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=errors,
                env=env,
                cwd=cwd,
            ) as process,
        ):
            assert process.stdin is not None and process.stdout is not None
            output: list[bytes] = []
            protocol_errors: list[SceneIpcError] = []
            rejected = threading.Event()
            decoded = threading.Event()
            media_path = str(requests[2].payload["path"])

            def read_protocol() -> None:
                try:
                    while response := read_envelope(process.stdout):
                        output.append(encode_envelope(response))
                        if _decoded_media_event(response, media_path):
                            decoded.set()
                        if response.message_type == "error" or (
                            response.message_type == "ack"
                            and response.payload.get("applied") is not True
                        ):
                            rejected.set()
                except SceneIpcError as error:
                    protocol_errors.append(error)

            output_reader = threading.Thread(target=read_protocol)
            output_reader.start()
            deadline = time.monotonic() + timeout
            rendered = False
            try:
                process.stdin.write(b"".join(encode_envelope(request) for request in requests))
                process.stdin.flush()
                while process.poll() is None and time.monotonic() < deadline:
                    frame = preview.read_latest()
                    if frame is not None and (frame.width, frame.height) == (32, 18):
                        offset = (frame.height // 2) * frame.stride + (frame.width // 2) * 4
                        if frame.data[offset : offset + 4] == b"\xff\xff\xff\xff":
                            rendered = True
                    if (rendered and decoded.is_set()) or rejected.is_set() or protocol_errors:
                        break
                    time.sleep(0.01)
                process.stdin.close()
                process.wait(timeout=max(5.0, deadline - time.monotonic()))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                output_reader.join(timeout=5)
            errors.seek(0)
            diagnostics = errors.read()
            if protocol_errors:
                raise LibobsPackagingError(f"Invalid packaged sidecar IPC: {protocol_errors[0]}")
            if not rendered:
                raise LibobsPackagingError(
                    "Packaged compositor did not render the decoded smoke image:\n"
                    + diagnostics.decode("utf-8", errors="replace")[-16000:]
                )
            if not decoded.is_set():
                raise LibobsPackagingError(
                    "Packaged media source did not decode the smoke WAV:\n"
                    + diagnostics.decode("utf-8", errors="replace")[-16000:]
                )
            return subprocess.CompletedProcess(
                command, process.returncode, b"".join(output), diagnostics
            )
    finally:
        preview.close()
        preview.unlink()


def verify_packaged_sidecar(executable: Path, *, timeout: float = 45.0) -> None:
    """Exercise binary IPC, native startup, heartbeat and orderly EOF shutdown."""
    _require_file(executable)
    deadline = int((time.monotonic() + timeout) * 1000)
    environment = os.environ.copy()
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "LIBOBS_PATH",
        "OBS_DATA_PATH",
        "SOLIN_LIBOBS_SIDECAR_NO_RUNTIME",
    ):
        environment.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="solin-libobs-smoke-") as directory:
        isolated = Path(directory)
        for name in (
            "APPDATA",
            "LOCALAPPDATA",
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            location = isolated / name
            location.mkdir()
            environment[name] = str(location)
        image = isolated / "smoke.png"
        image.write_bytes(_smoke_png())
        media = isolated / "smoke.wav"
        with wave.open(str(media), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(44100)
            stream.writeframes(b"\0\0" * 88200)
        environment["SOLIN_SCENE_IMAGES_DIR"] = str(isolated)
        payloads = (
            ("hello", {}),
            (
                "hydrate",
                {
                    "document": {
                        "sources": [
                            {
                                "id": "smoke-image",
                                "type": "image",
                                "configuration": {"asset_id": image.name},
                            }
                        ],
                        "scenes": [
                            {
                                "id": "smoke-scene",
                                "layers": [
                                    {
                                        "id": "smoke-layer",
                                        "source_id": "smoke-image",
                                        "visible": True,
                                    },
                                ],
                            }
                        ],
                    },
                    "active_scenes": {
                        "virtual_camera": "smoke-scene",
                        "media_windows": "smoke-scene",
                        "editor": "smoke-scene",
                    },
                },
            ),
            (
                "open_media",
                {"path": str(media), "is_local_file": True, "autoplay": True, "volume_percent": 0},
            ),
            ("ping", {}),
        )
        requests = [
            SceneIpcEnvelope(
                message_type=kind,
                request_id=kind,
                session_id="packaged-smoke",
                process_generation="packaged-smoke",
                sequence=index,
                document_revision=0,
                deadline_monotonic_ms=deadline,
                payload=payload,
            )
            for index, (kind, payload) in enumerate(payloads, start=1)
        ]
        result = _run_preview_smoke(
            [str(executable.resolve()), LIBOBS_SIDECAR_ARGUMENT],
            requests=requests,
            timeout=timeout,
            env=environment,
            cwd=isolated,
        )
    diagnostics = result.stderr.decode("utf-8", errors="replace")[-16000:]
    if result.returncode:
        raise LibobsPackagingError(
            f"Packaged sidecar exited with {result.returncode}:\n{diagnostics}"
        )
    responses: dict[str, SceneIpcEnvelope] = {}
    media_decoded = False
    stream = io.BytesIO(result.stdout)
    try:
        while response := read_envelope(stream):
            responses[response.request_id] = response
            media_decoded = media_decoded or _decoded_media_event(response, str(media))
    except SceneIpcError as error:
        raise LibobsPackagingError(
            f"Invalid packaged sidecar IPC: {error}\n{diagnostics}"
        ) from error
    for request, expected in zip(requests, ("hello_ack", "ack", "ack", "heartbeat"), strict=True):
        response = responses.get(request.request_id)
        if response is None or response.message_type != expected:
            raise LibobsPackagingError(f"Missing packaged sidecar {expected}:\n{diagnostics}")
        if (response.session_id, response.process_generation, response.sequence) != (
            request.session_id,
            request.process_generation,
            request.sequence,
        ):
            raise LibobsPackagingError("Packaged sidecar response correlation mismatch.")
        if expected == "ack" and response.payload.get("applied") is not True:
            raise LibobsPackagingError(
                f"Packaged sidecar rejected {request.message_type}: "
                f"{response.payload}\n{diagnostics}"
            )
    if responses["hello"].payload.get("hardware_compositing") is not True:
        raise LibobsPackagingError(f"Packaged libobs runtime failed to initialize:\n{diagnostics}")
    if not media_decoded:
        raise LibobsPackagingError(
            f"Packaged media source did not decode the smoke WAV:\n{diagnostics}"
        )


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-dir", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--linux-mux-helper", type=Path)
    parser.add_argument("--qualification-fixtures", type=Path)
    options = parser.parse_args(arguments)
    try:
        import pylibobs

        if not pylibobs.__file__:
            raise LibobsPackagingError("The installed pylibobs package has no filesystem location.")
        target = _PLATFORMS.get(sys.platform)
        architecture = _ARCHITECTURES.get(platform.machine().lower())
        if target is None or architecture is None:
            raise LibobsPackagingError(
                f"Unsupported libobs target: {sys.platform}/{platform.machine()}"
            )
        distribution = importlib.metadata.distribution("pylibobs")
        licenses = [
            Path(distribution.locate_file(path))
            for path in distribution.files or ()
            if ".dist-info/licenses/" in str(path).replace("\\", "/")
        ]
        destination = stage_runtime(
            package_dir=Path(pylibobs.__file__).resolve().parent,
            application_dir=options.application_dir,
            target_platform=target,
            architecture=architecture,
            license_files=licenses,
            linux_mux_helper=options.linux_mux_helper,
            qualification_fixtures=options.qualification_fixtures,
        )
        verify_packaged_sidecar(options.executable)
    except (ImportError, OSError, LibobsPackagingError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Packaged and verified libobs runtime: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
