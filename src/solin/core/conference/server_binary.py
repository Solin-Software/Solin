"""Locate the bundled LiveKit server binary.

Lookup order:
1. ``SOLIN_LIVEKIT_SERVER`` (an explicit path, for development and diagnostics).
2. Compiled builds: ``<app dir>/bin/livekit-server[.exe]``, staged by the build scripts.
3. Development checkouts: ``build/livekit/<platform>/``, fetched by
   ``scripts/fetch_livekit_server.py`` (Linux, Windows) or built by
   ``scripts/build_livekit_server.py`` (macOS).

Never resolved through PATH: Solin always runs the version it was qualified with.
"""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from pathlib import Path

LIVEKIT_SERVER_VERSION = "1.13.10"
OVERRIDE_ENVIRONMENT = "SOLIN_LIVEKIT_SERVER"
_REPO_ROOT = Path(__file__).resolve().parents[4]


class ServerUnavailableError(RuntimeError):
    """The LiveKit server binary is missing or unsupported on this platform."""


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """LiveKit server platform for this machine, e.g. ``linux-amd64``.

    LiveKit publishes Linux and Windows builds; macOS builds are compiled from the same
    pinned source by ``scripts/build_livekit_server.py``.
    """
    operating_system = {"win32": "windows", "linux": "linux", "darwin": "darwin"}.get(
        system or sys.platform
    )
    architecture = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get((machine or platform.machine()).casefold())
    if operating_system is None or architecture is None:
        raise ServerUnavailableError(
            f"Hosting meetings is not supported on {system or sys.platform}/{machine or platform.machine()}"
        )
    return f"{operating_system}-{architecture}"


def executable_name(system: str | None = None) -> str:
    return "livekit-server.exe" if (system or sys.platform) == "win32" else "livekit-server"


def server_executable(
    *,
    environ: Mapping[str, str] | None = None,
    compiled: bool | None = None,
    app_executable: str | None = None,
    repo_root: Path = _REPO_ROOT,
) -> Path:
    environ = os.environ if environ is None else environ
    override = environ.get(OVERRIDE_ENVIRONMENT, "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise ServerUnavailableError(f"{OVERRIDE_ENVIRONMENT} does not point to a file: {path}")
        return path
    if compiled is None:
        from solin.core.foundation.constants import IS_COMPILED

        compiled = IS_COMPILED
    name = executable_name()
    if compiled:
        path = Path(app_executable or sys.executable).resolve().parent / "bin" / name
    else:
        path = repo_root / "build" / "livekit" / platform_key() / name
    if not path.is_file():
        hint = "" if compiled else " (run scripts/fetch_livekit_server.py)"
        raise ServerUnavailableError(f"LiveKit server not found at {path}{hint}")
    return path
