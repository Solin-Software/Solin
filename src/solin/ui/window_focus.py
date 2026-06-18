"""Window focus adapters for presentation workflows."""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger(__name__)


def raise_projection_window(window: Any) -> None:
    """Raise a projection window after external screen-share automation."""
    if window.isMinimized():
        window.showFullScreen()

    window.raise_()
    window.activateWindow()
    _force_windows_foreground(window)


def _force_windows_foreground(window: Any) -> None:
    if sys.platform != "win32":
        return

    try:
        import ctypes

        hwnd = int(window.winId())
        hwnd_topmost = -1
        swp_nosize = 0x0001
        swp_nomove = 0x0002
        swp_showwindow = 0x0040
        user32 = ctypes.windll.user32
        user32.SetWindowPos(
            hwnd,
            hwnd_topmost,
            0,
            0,
            0,
            0,
            swp_nomove | swp_nosize | swp_showwindow,
        )
        user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - Win32 foreground API boundary
        log.debug("Failed to refocus projection window after auto-share", exc_info=True)
