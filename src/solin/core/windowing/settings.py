from __future__ import annotations

from dataclasses import dataclass

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

    def size(self, default_width: int, default_height: int) -> tuple[int, int]:
        return (
            int(self.settings.value(SettingsKey.WINDOW_WIDTH, default_width, int)),
            int(self.settings.value(SettingsKey.WINDOW_HEIGHT, default_height, int)),
        )

    def save_size(self, width: int, height: int) -> None:
        self.settings.set_value(SettingsKey.WINDOW_WIDTH, int(width), sync=False)
        self.settings.set_value(SettingsKey.WINDOW_HEIGHT, int(height), sync=False)
        self.settings.sync()
