"""Native Win32 helpers for Zoom automation workers."""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)


def initialize_com_for_current_thread() -> None:
    """Initialize COM for the current thread on Windows."""
    if sys.platform != "win32":
        return
    try:
        # COINIT_MULTITHREADED = 0x0
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)
        # S_OK=0, S_FALSE=1 (already initialized) are both fine.
        if hr not in (0, 1):
            log.debug("CoInitializeEx returned 0x%08X", hr)
    except Exception:  # noqa: BLE001 - COM initialization boundary
        log.debug("Failed to initialize COM for Zoom worker", exc_info=True)


def share_selection_dialog_open() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.FindWindowW("ZPShareEntranceClass", None))
    except Exception:  # noqa: BLE001 - Win32 window probe boundary
        log.debug("Failed to probe Zoom share-selection dialog", exc_info=True)
        return False
