from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QByteArray

from solin.core.foundation.constants import QSETTINGS_MAIN_WINDOW_GEOMETRY_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


@dataclass(frozen=True, slots=True)
class WindowGeometrySettingsStore:
    """Typed profile-scoped settings for main window geometry."""

    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "WindowGeometrySettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
            )
        )

    def initial_size(self, default_width: int, default_height: int) -> tuple[int, int]:
        """Return the legacy size fallback used until full geometry is persisted."""

        return (
            int(self.settings.value(SettingsKey.LEGACY_WINDOW_WIDTH, default_width, int)),
            int(self.settings.value(SettingsKey.LEGACY_WINDOW_HEIGHT, default_height, int)),
        )

    def geometry(self) -> QByteArray:
        value = self.settings.value(
            SettingsKey.WINDOW_GEOMETRY,
            QByteArray(),
            QByteArray,
        )
        return value if isinstance(value, QByteArray) else QByteArray()

    def save_geometry(self, geometry: QByteArray) -> None:
        self.settings.set_value(
            SettingsKey.WINDOW_GEOMETRY,
            QByteArray(geometry),
            sync=False,
        )
        self.settings.remove(SettingsKey.LEGACY_WINDOW_WIDTH, sync=False)
        self.settings.remove(SettingsKey.LEGACY_WINDOW_HEIGHT, sync=False)
        self.settings.sync()

    def sidebar_collapsed(self, default: bool = False) -> bool:
        return bool(
            self.settings.value(SettingsKey.SIDEBAR_COLLAPSED, default, bool)
        )

    def save_sidebar_collapsed(self, collapsed: bool) -> None:
        self.settings.set_value(SettingsKey.SIDEBAR_COLLAPSED, bool(collapsed))
