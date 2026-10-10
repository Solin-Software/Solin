from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtGui import QGuiApplication

from ..styles.theme import PALETTE
from ..ui.titlebar import apply_titlebar_color

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WindowStateContext:
    """Window operations required by persisted shell state management."""

    width: Callable[[], int]
    height: Callable[[], int]
    move: Callable[[int, int], None]
    is_minimized: Callable[[], bool]
    show_normal: Callable[[], None]
    is_visible: Callable[[], bool]
    show: Callable[[], None]
    raise_window: Callable[[], None]
    activate_window: Callable[[], None]
    win_id: Callable[[], Any]
    titlebar_window: Any


class WindowStateController:
    """Owns main-window positioning, foreground activation, and titlebar styling."""

    def __init__(self, context: WindowStateContext) -> None:
        self._context = context

    def center_on_primary_screen(self) -> None:
        primary = QGuiApplication.primaryScreen()
        if primary is None:
            return
        context = self._context
        geometry = primary.availableGeometry()
        context.move(
            geometry.x() + (geometry.width() - context.width()) // 2,
            geometry.y() + (geometry.height() - context.height()) // 2,
        )

    def apply_titlebar_color(self) -> None:
        apply_titlebar_color(self._context.titlebar_window, PALETTE.titlebar)

    def bring_to_front(self) -> None:
        """
        Bring the main window to the foreground reliably.

        On Windows, main.py calls AllowSetForegroundWindow() in the second
        instance; finish here with a direct SetForegroundWindow() call.
        """
        context = self._context
        if context.is_minimized():
            context.show_normal()
        elif not context.is_visible():
            context.show()

        context.raise_window()
        context.activate_window()

        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(context.win_id())
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001 - Win32 foreground API boundary
                log.debug("Failed to force main window foreground on Windows", exc_info=True)
