from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


class MediaPlaybackSettings(Protocol):
    def auto_download_on_play(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class MediaSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "MediaSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def auto_download_on_play(self) -> bool:
        return bool(self.settings.value(SettingsKey.AUTO_DOWNLOAD_ON_PLAY, True, bool))

    def set_auto_download_on_play(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.AUTO_DOWNLOAD_ON_PLAY, bool(enabled))

    def meetings_auto_download(self) -> bool:
        return bool(self.settings.value(SettingsKey.MEETINGS_AUTO_DOWNLOAD, False, bool))

    def set_meetings_auto_download(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.MEETINGS_AUTO_DOWNLOAD, bool(enabled))

    def sjjm_announce_mode(self) -> bool:
        return bool(self.settings.value(SettingsKey.SJJM_ANNOUNCE_MODE, False, bool))

    def set_sjjm_announce_mode(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.SJJM_ANNOUNCE_MODE, bool(enabled))

    def start_videos_paused(self) -> bool:
        return bool(self.settings.value(SettingsKey.START_VIDEOS_PAUSED, False, bool))

    def set_start_videos_paused(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.START_VIDEOS_PAUSED, bool(enabled))
