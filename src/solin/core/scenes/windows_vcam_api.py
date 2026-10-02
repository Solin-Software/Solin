"""Guard Windows-only syscall entry points used by the virtual-camera transport."""

from __future__ import annotations

import ctypes
import sys


def require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("the virtual camera is Windows-only")


def load_windows_library(name: str) -> ctypes.CDLL:
    if sys.platform == "win32":
        return ctypes.WinDLL(name, use_last_error=True)
    raise RuntimeError("the virtual camera is Windows-only")


def windows_last_error() -> int:
    if sys.platform == "win32":
        return ctypes.get_last_error()
    raise RuntimeError("the virtual camera is Windows-only")


def windows_file_descriptor(handle: int, flags: int) -> int:
    if sys.platform == "win32":
        import msvcrt

        return msvcrt.open_osfhandle(handle, flags)
    raise RuntimeError("the virtual camera is Windows-only")
