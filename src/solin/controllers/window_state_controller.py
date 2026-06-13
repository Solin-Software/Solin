from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication, QIcon

from ..core.foundation.constants import QSETTINGS_MAIN_WINDOW_GEOMETRY_APP
from ..core.foundation.settings_keys import SettingsKey
from ..core.profiles import settings as _ps
from ..core.ui.titlebar import apply_titlebar_color

log = logging.getLogger(__name__)


class WindowStateController:
    """Owns persisted size, startup icon, centering, and titlebar styling."""

    _DEFAULT_WIDTH = 1200
    _DEFAULT_HEIGHT = 760
    _TITLEBAR_COLOR = "#1A231F"

    def __init__(self, window) -> None:
        self._window = window

    def restore_size(self) -> None:
        prefs = _ps.prefs(QSETTINGS_MAIN_WINDOW_GEOMETRY_APP)
        width = prefs.value(SettingsKey.WINDOW_WIDTH, self._DEFAULT_WIDTH, int)
        height = prefs.value(SettingsKey.WINDOW_HEIGHT, self._DEFAULT_HEIGHT, int)
        width, height = self._clamped_size(
            width,
            height,
            self._window.minimumWidth(),
            self._window.minimumHeight(),
        )
        self._window.resize(width, height)

    def save_size(self) -> None:
        prefs = _ps.prefs(QSETTINGS_MAIN_WINDOW_GEOMETRY_APP)
        prefs.setValue(SettingsKey.WINDOW_WIDTH, self._window.width())
        prefs.setValue(SettingsKey.WINDOW_HEIGHT, self._window.height())

    def apply_icon(self) -> None:
        icon_path = self._application_asset_path("icon.ico")
        if icon_path.is_file():
            self._window.setWindowIcon(QIcon(str(icon_path)))

    def center_on_primary_screen(self) -> None:
        primary = QGuiApplication.primaryScreen()
        if primary is None:
            return
        geometry = primary.availableGeometry()
        self._window.move(
            geometry.x() + (geometry.width() - self._window.width()) // 2,
            geometry.y() + (geometry.height() - self._window.height()) // 2,
        )

    def apply_titlebar_color(self) -> None:
        apply_titlebar_color(self._window, self._TITLEBAR_COLOR)

    def bring_to_front(self) -> None:
        """
        Traz a janela principal para frente de forma robusta.

        No Windows, main.py chama AllowSetForegroundWindow() na segunda
        instancia; aqui finalizamos com SetForegroundWindow() direto.
        """
        window = self._window
        if window.isMinimized():
            window.showNormal()
        elif not window.isVisible():
            window.show()

        window.raise_()
        window.activateWindow()

        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = int(window.winId())
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001 - Win32 foreground API boundary
                log.debug("Failed to force main window foreground on Windows", exc_info=True)

    @staticmethod
    def _application_asset_path(filename: str) -> Path:
        package_root = Path(__file__).resolve().parents[1]
        for root in (package_root.parent, package_root.parents[1], Path.cwd()):
            candidate = root / "assets" / filename
            if candidate.is_file():
                return candidate
        return package_root.parents[1] / "assets" / filename

    @staticmethod
    def _clamped_size(
        width,
        height,
        min_width: int,
        min_height: int,
    ) -> tuple[int, int]:
        return max(min_width, int(width)), max(min_height, int(height))
