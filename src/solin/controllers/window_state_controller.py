from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtGui import QGuiApplication

from ..styles.theme import PALETTE
from ..ui.titlebar import apply_titlebar_color
from ..core.windowing.settings import WindowGeometrySettingsStore

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WindowStateContext:
    """Window operations required by persisted shell state management."""

    minimum_width: Callable[[], int]
    minimum_height: Callable[[], int]
    resize: Callable[[int, int], None]
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
    """Owns persisted size, startup icon, centering, and titlebar styling."""

    _DEFAULT_WIDTH = 1200
    _DEFAULT_HEIGHT = 760

    def __init__(
        self,
        context: WindowStateContext,
        geometry_settings: WindowGeometrySettingsStore,
    ) -> None:
        self._context = context
        self._geometry_settings = geometry_settings

    def restore_size(self) -> None:
        context = self._context
        width, height = self._geometry_settings.size(
            self._DEFAULT_WIDTH,
            self._DEFAULT_HEIGHT,
        )
        width, height = self._clamped_size(
            width,
            height,
            context.minimum_width(),
            context.minimum_height(),
        )
        context.resize(width, height)

    def save_size(self) -> None:
        context = self._context
        self._geometry_settings.save_size(context.width(), context.height())

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
        Traz a janela principal para frente de forma robusta.

        No Windows, main.py chama AllowSetForegroundWindow() na segunda
        instancia; aqui finalizamos com SetForegroundWindow() direto.
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

    @staticmethod
    def _clamped_size(
        width,
        height,
        min_width: int,
        min_height: int,
    ) -> tuple[int, int]:
        return max(min_width, int(width)), max(min_height, int(height))
