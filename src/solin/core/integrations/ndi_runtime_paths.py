"""Pure NDI runtime library path discovery policy (per platform).

The NDI receiver ([solin.core.integrations.ndi]) loads ``libndi`` via ctypes, so
this returns the ordered list of library names/paths to try — bare names first
(so the OS loader's own search runs), then explicit runtime/SDK locations. The
policy is platform-specific: ``Processing.NDI.Lib.*.dll`` on Windows,
``libndi.so*`` on Linux, ``libndi.dylib`` on macOS.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath

# ── Windows ──────────────────────────────────────────────────────────────────
NDI_RUNTIME_LIBRARY_NAMES = (
    "Processing.NDI.Lib.x64.dll",
    "Processing.NDI.Lib.x86.dll",
)

NDI_RUNTIME_ENV_KEYS = (
    "NDI_RUNTIME_DIR_V6",
    "NDI_RUNTIME_DIR_V5",
    "NDI_RUNTIME_DIR",
)

PROGRAM_FILES_ENV_KEYS = (
    "ProgramFiles",
    "ProgramFiles(x86)",
)

NDI_RUNTIME_RELATIVE_DIRS = (
    PureWindowsPath("obs-studio") / "obs-plugins" / "64bit",
    PureWindowsPath("obs-studio") / "bin" / "64bit",
    PureWindowsPath("NDI") / "NDI 6 Runtime" / "v6" / "Bin" / "x64",
    PureWindowsPath("NDI") / "NDI 6 Runtime" / "v6",
    PureWindowsPath("NDI") / "NDI 5 Runtime" / "v5" / "Bin" / "x64",
    PureWindowsPath("NDI") / "NDI 5 Runtime" / "v5",
    PureWindowsPath("NewTek") / "NDI 6 Runtime" / "v6" / "Bin" / "x64",
    PureWindowsPath("NewTek") / "NDI 5 Runtime" / "v5" / "Bin" / "x64",
)

# ── Linux ────────────────────────────────────────────────────────────────────
LINUX_LIBRARY_NAMES = ("libndi.so.6", "libndi.so.5", "libndi.so.4", "libndi.so")
LINUX_RUNTIME_DIRS = (
    "/usr/lib/x86_64-linux-gnu",
    "/usr/local/lib",
    "/usr/lib",
    "/opt/ndi/lib",
)
LINUX_HOME_RELATIVE = ("NDI SDK for Linux/lib/x86_64-linux-gnu",)

# ── macOS ────────────────────────────────────────────────────────────────────
MACOS_LIBRARY_NAMES = ("libndi.dylib",)
MACOS_RUNTIME_DIRS = (
    "/usr/local/lib",
    "/opt/homebrew/lib",
    "/Library/NDI SDK for Apple/lib/macOS",
)
MACOS_HOME_RELATIVE = ("NDI SDK for Apple/lib/macOS",)


def candidate_ndi_library_paths(environ: Mapping[str, str], platform: str) -> list[str]:
    """Ordered ctypes library candidates for the given ``platform`` value.

    ``platform`` is the caller's ``sys.platform`` (passed in so this policy module
    stays dependency-free — see the architecture-boundary contract test)."""
    if platform.startswith("win"):
        return _windows_candidates(environ)
    if platform == "darwin":
        return _posix_candidates(
            environ, MACOS_LIBRARY_NAMES, MACOS_RUNTIME_DIRS, MACOS_HOME_RELATIVE
        )
    return _posix_candidates(
        environ, LINUX_LIBRARY_NAMES, LINUX_RUNTIME_DIRS, LINUX_HOME_RELATIVE
    )


def _windows_candidates(environ: Mapping[str, str]) -> list[str]:
    candidates: list[str] = list(NDI_RUNTIME_LIBRARY_NAMES)

    for base in _present_values(environ, NDI_RUNTIME_ENV_KEYS):
        base_path = PureWindowsPath(base)
        for name in NDI_RUNTIME_LIBRARY_NAMES:
            candidates.append(str(base_path / name))
            candidates.append(str(base_path / "Bin" / "x64" / name))

    for root in _program_files_roots(environ):
        root_path = PureWindowsPath(root)
        for relative_dir in NDI_RUNTIME_RELATIVE_DIRS:
            for name in NDI_RUNTIME_LIBRARY_NAMES:
                candidates.append(str(root_path / relative_dir / name))

    return candidates


def _posix_candidates(
    environ: Mapping[str, str],
    names: tuple[str, ...],
    dirs: tuple[str, ...],
    home_relative: tuple[str, ...],
) -> list[str]:
    # Bare names first so the OS loader's own search (LD_LIBRARY_PATH / ldconfig
    # on Linux, DYLD paths on macOS) resolves an already-installed runtime.
    candidates: list[str] = list(names)

    for base in _present_values(environ, NDI_RUNTIME_ENV_KEYS):
        base_path = PurePosixPath(base)
        for name in names:
            candidates.append(str(base_path / name))
            candidates.append(str(base_path / "lib" / name))

    home = environ.get("HOME")
    if home:
        for relative in home_relative:
            for name in names:
                candidates.append(str(PurePosixPath(home) / relative / name))

    for directory in dirs:
        for name in names:
            candidates.append(str(PurePosixPath(directory) / name))

    return candidates


def _program_files_roots(environ: Mapping[str, str]) -> list[str]:
    roots = _present_values(environ, PROGRAM_FILES_ENV_KEYS)
    return roots or [r"C:\Program Files", r"C:\Program Files (x86)"]


def _present_values(environ: Mapping[str, str], keys: tuple[str, ...]) -> list[str]:
    return [value for key in keys if (value := environ.get(key))]
