"""Runtime platform identity for release compatibility checks."""

from __future__ import annotations

import platform
import sys

from solin.core.releases.compatibility import ReleaseTarget


def current_release_target() -> ReleaseTarget:
    operating_system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    if sys.platform == "darwin":
        os_version = platform.mac_ver()[0]
    elif sys.platform == "win32":
        os_version = platform.version()
    else:
        libc, version = platform.libc_ver()
        os_version = version if libc == "glibc" else ""
    return ReleaseTarget(operating_system, architecture, os_version)
