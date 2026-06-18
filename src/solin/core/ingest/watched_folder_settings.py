from __future__ import annotations

from dataclasses import dataclass

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


@dataclass(frozen=True, slots=True)
class WatchedFolderSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "WatchedFolderSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def path(self) -> str:
        return self.settings.string(SettingsKey.WATCHED_FOLDER_PATH)

    def set_path(self, path: str) -> None:
        normalized = str(path or "")
        if not normalized:
            self.clear_path()
            return
        self.settings.set_value(SettingsKey.WATCHED_FOLDER_PATH, normalized)

    def clear_path(self) -> None:
        self.settings.remove(SettingsKey.WATCHED_FOLDER_PATH)
