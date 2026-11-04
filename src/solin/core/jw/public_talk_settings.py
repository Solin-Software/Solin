from __future__ import annotations

from dataclasses import dataclass

from solin.core.foundation.constants import QSETTINGS_APP_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


@dataclass(frozen=True, slots=True)
class PublicTalkSettingsStore:
    """Profile-scoped reference to the external public-talk publication.

    The importing workflow validates the source before saving its path.
    """

    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> PublicTalkSettingsStore:
        return cls(
            settings=SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_APP_APP,
            )
        )

    def source_path(self) -> str:
        return self.settings.string(SettingsKey.PUBLIC_TALK_SOURCE_PATH)

    def set_source_path(self, path: str) -> None:
        self.settings.set_value(SettingsKey.PUBLIC_TALK_SOURCE_PATH, path)
