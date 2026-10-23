"""Locate the hosted-meeting helper executables.

Three helpers ship with Solin: ``galene`` (the media server), ``solin-galene-bridge``
(program publisher and commenter-audio player) and ``frpc`` (the hub tunnel). Lookup
order, never ``PATH``:

1. an environment override (``SOLIN_GALENE``, ``SOLIN_GALENE_BRIDGE``, ``SOLIN_FRPC``);
2. ``<app>/bin/<name>`` next to a compiled build;
3. ``build/hosted-meeting/<platform>/<name>`` in a development checkout, where
   ``scripts/build_galene_bridge.py`` and ``scripts/fetch_hosted_meeting_helpers.py`` put them.
"""

from __future__ import annotations

import os
import platform
import sys
from enum import StrEnum
from pathlib import Path


class Helper(StrEnum):
    GALENE = "galene"
    BRIDGE = "solin-galene-bridge"
    FRPC = "frpc"


_ENV = {
    Helper.GALENE: "SOLIN_GALENE",
    Helper.BRIDGE: "SOLIN_GALENE_BRIDGE",
    Helper.FRPC: "SOLIN_FRPC",
}

_REPO_ROOT = Path(__file__).resolve().parents[4]


def platform_key() -> str:
    """``linux-amd64``, ``windows-amd64``, ``darwin-arm64``…"""

    system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def executable_name(helper: Helper) -> str:
    return f"{helper.value}.exe" if sys.platform == "win32" else helper.value


def candidates(helper: Helper) -> list[Path]:
    found: list[Path] = []
    override = os.environ.get(_ENV[helper], "").strip()
    if override:
        found.append(Path(override))
    name = executable_name(helper)
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        found.append(Path(sys.executable).resolve().parent / "bin" / name)
    found.append(Path(sys.argv[0]).resolve().parent / "bin" / name)
    found.append(_REPO_ROOT / "build" / "hosted-meeting" / platform_key() / name)
    return found


def find_helper(helper: Helper) -> Path | None:
    """The first existing, executable candidate, or ``None``."""

    for path in candidates(helper):
        if path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK)):
            return path
    return None
