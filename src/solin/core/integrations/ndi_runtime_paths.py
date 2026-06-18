"""Pure NDI runtime library path discovery policy."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

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
    Path("obs-studio") / "obs-plugins" / "64bit",
    Path("obs-studio") / "bin" / "64bit",
    Path("NDI") / "NDI 6 Runtime" / "v6" / "Bin" / "x64",
    Path("NDI") / "NDI 6 Runtime" / "v6",
    Path("NDI") / "NDI 5 Runtime" / "v5" / "Bin" / "x64",
    Path("NDI") / "NDI 5 Runtime" / "v5",
    Path("NewTek") / "NDI 6 Runtime" / "v6" / "Bin" / "x64",
    Path("NewTek") / "NDI 5 Runtime" / "v5" / "Bin" / "x64",
)


def candidate_ndi_library_paths(environ: Mapping[str, str]) -> list[str]:
    candidates: list[str] = list(NDI_RUNTIME_LIBRARY_NAMES)

    for base in _present_values(environ, NDI_RUNTIME_ENV_KEYS):
        for name in NDI_RUNTIME_LIBRARY_NAMES:
            candidates.append(str(Path(base) / name))
            candidates.append(str(Path(base) / "Bin" / "x64" / name))

    for root in _program_files_roots(environ):
        for relative_dir in NDI_RUNTIME_RELATIVE_DIRS:
            for name in NDI_RUNTIME_LIBRARY_NAMES:
                candidates.append(str(Path(root) / relative_dir / name))

    return candidates


def _program_files_roots(environ: Mapping[str, str]) -> list[str]:
    roots = _present_values(environ, PROGRAM_FILES_ENV_KEYS)
    return roots or [r"C:\Program Files", r"C:\Program Files (x86)"]


def _present_values(environ: Mapping[str, str], keys: tuple[str, ...]) -> list[str]:
    return [value for key in keys if (value := environ.get(key))]

