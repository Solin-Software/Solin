"""Build ``solin-galene-bridge`` (tools/solin-galene-bridge) for the host platform.

The bridge is a Go program with two cgo dependencies: libopus (Opus encode/decode,
through ``gopkg.in/hraban/opus.v2``, built with the ``nolibopusfile`` tag so libopusfile
is not needed) and miniaudio (playback, through ``github.com/gen2brain/malgo``, compiled
from source; it loads the system audio libraries at run time). The result goes to
``build/hosted-meeting/<platform>/solin-galene-bridge[.exe]``, where
``solin.core.hosted_meeting.binaries`` looks for it.

Requirements (Go >= the version in tools/solin-galene-bridge/go.mod, on PATH):

Linux
    A C compiler, pkg-config and the libopus headers, e.g.
    ``sudo apt install gcc pkg-config libopus-dev`` (Fedora: ``gcc pkgconf opus-devel``).
    Nothing audio-related is linked: miniaudio dlopens libpulse (PipeWire's pulse
    server) or libasound when ``receive``/``devices`` run.

Windows
    MSYS2 with the MinGW-w64 toolchain, run from a shell where the MinGW ``gcc`` and
    ``pkg-config`` come first on PATH (e.g. the "MSYS2 MINGW64" shell, or
    ``C:\\msys64\\mingw64\\bin`` prepended to PATH for a native Go):
    ``pacman -S mingw-w64-x86_64-gcc mingw-w64-x86_64-pkgconf mingw-w64-x86_64-opus``.
    cgo must be enabled (this script sets ``CGO_ENABLED=1``). The executable is linked
    with ``-static`` by default so it does not depend on libopus-0.dll,
    libwinpthread-1.dll or libgcc DLLs; pass ``--no-static-runtime`` to opt out.

macOS
    Xcode command line tools and Homebrew's ``brew install opus pkg-config``. libopus
    is linked statically by default (Homebrew ships ``libopus.a``) so the binary does
    not reference ``/opt/homebrew/opt/opus/lib/libopus.0.dylib``; set
    ``MACOSX_DEPLOYMENT_TARGET`` to the oldest macOS Solin supports to avoid
    "built for newer macOS" linker warnings.

On every platform libopus is linked statically when ``libopus.a`` is found next to the
pkg-config library directory (``--opus-linkage auto``, the default); ``static`` makes
that mandatory and ``dynamic`` uses whatever pkg-config reports.

Usage::

    python scripts/build_galene_bridge.py [--version V] [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRECTORY = REPOSITORY_ROOT / "tools" / "solin-galene-bridge"
VERSION_FILE = REPOSITORY_ROOT / "src" / "solin" / "version.py"
NAME = "solin-galene-bridge"


class BridgeBuildError(RuntimeError):
    """The bridge cannot be built on this machine."""


def platform_key() -> str:
    """Same rule as ``solin.core.hosted_meeting.binaries.platform_key()``."""

    system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def executable_name() -> str:
    return f"{NAME}.exe" if sys.platform == "win32" else NAME


def default_version() -> str:
    """``<Solin version>+<git short hash>[.dirty]``, or ``dev`` pieces when unknown."""

    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', VERSION_FILE.read_text(encoding="utf-8"), re.MULTILINE)
    version = match.group(1) if match else "dev"
    try:
        commit = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(REPOSITORY_ROOT), "status", "--porcelain", "--", str(SOURCE_DIRECTORY)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return version
    return f"{version}+{commit}{'.dirty' if dirty else ''}"


def require_tool(name: str, hint: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise BridgeBuildError(f"{name} not found on PATH ({hint})")
    return path


def pkg_config(*args: str) -> str:
    result = subprocess.run(["pkg-config", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise BridgeBuildError(f"pkg-config {' '.join(args)} failed: {result.stderr.strip() or 'opus not found'}")
    return result.stdout.strip()


def static_opus_shim(directory: Path, mode: str) -> Path | None:
    """Write an ``opus.pc`` that links ``libopus.a`` by path; ``None`` to link normally."""

    if mode == "dynamic":
        return None
    libdir = Path(pkg_config("--variable=libdir", "opus"))
    archive = libdir / "libopus.a"
    if not archive.is_file():
        if mode == "static":
            raise BridgeBuildError(f"--opus-linkage static: {archive} does not exist")
        print(f"note: {archive} not found, linking libopus dynamically", file=sys.stderr)
        return None
    cflags = pkg_config("--cflags", "opus")
    extra = " -lm" if sys.platform.startswith("linux") else ""
    shim = directory / "opus.pc"
    shim.write_text(
        "Name: opus\n"
        "Description: libopus, static (solin-galene-bridge build shim)\n"
        f"Version: {pkg_config('--modversion', 'opus')}\n"
        f"Cflags: {cflags}\n"
        f"Libs: {archive.as_posix()}{extra}\n",
        encoding="utf-8",
    )
    return shim


def build(version: str, output_dir: Path, opus_linkage: str, static_runtime: bool) -> Path:
    require_tool("go", "install Go from https://go.dev/dl/")
    require_tool("pkg-config", "see this script's docstring for the per-OS requirements")
    if sys.platform == "win32":
        require_tool("gcc", "MSYS2 mingw-w64-x86_64-gcc must be on PATH")
    pkg_config("--exists", "opus")

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / executable_name()
    env = dict(os.environ, CGO_ENABLED="1")
    ldflags = f"-s -w -X main.version={version}"
    if sys.platform == "win32" and static_runtime:
        ldflags += " -extldflags=-static"
    with tempfile.TemporaryDirectory(prefix="bridge-pc-") as scratch:
        shim = static_opus_shim(Path(scratch), opus_linkage)
        if shim is not None:
            env["PKG_CONFIG_PATH"] = os.pathsep.join(filter(None, [str(shim.parent), env.get("PKG_CONFIG_PATH", "")]))
        command = [
            "go", "build", "-tags", "nolibopusfile", "-trimpath",
            "-ldflags", ldflags, "-o", str(output), ".",
        ]
        print("+", shlex.join(command), f"(libopus {'static' if shim else 'dynamic'})", flush=True)
        subprocess.run(command, cwd=SOURCE_DIRECTORY, env=env, check=True)
    return output


def check(binary: Path, expected_version: str) -> None:
    reported = subprocess.run([str(binary), "version"], check=True, capture_output=True, text=True).stdout.strip()
    if reported != expected_version:
        raise BridgeBuildError(f"{binary} reports version {reported!r}, expected {expected_version!r}")
    print(f"built {binary} ({reported})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", default=None, help="version string embedded in the binary")
    parser.add_argument(
        "--output-dir", type=Path, default=REPOSITORY_ROOT / "build" / "hosted-meeting" / platform_key(),
        help="destination directory (default: build/hosted-meeting/<platform>)",
    )
    parser.add_argument("--opus-linkage", choices=("auto", "static", "dynamic"), default="auto")
    parser.add_argument("--no-static-runtime", action="store_true",
                        help="Windows: do not link the MinGW runtime statically")
    args = parser.parse_args(argv)
    version = args.version or default_version()
    try:
        binary = build(version, args.output_dir.resolve(), args.opus_linkage, not args.no_static_runtime)
        check(binary, version)
    except BridgeBuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
