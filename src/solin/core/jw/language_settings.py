from __future__ import annotations

from dataclasses import dataclass

from solin.core.foundation.constants import QSETTINGS_APP_APP, QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


@dataclass(frozen=True, slots=True)
class JWLanguageSettingsStore:
    """Typed profile-scoped settings for JW media language selection."""

    settings: SettingsStore
    legacy_settings: SettingsStore

    @classmethod
    def for_profile_settings(cls, profile_settings: ProfileSettings) -> "JWLanguageSettingsStore":
        return cls(
            settings=SettingsStore.for_namespace(profile_settings.organization, QSETTINGS_APP_APP),
            legacy_settings=SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            ),
        )

    def media_language_code(self) -> str:
        code = self.settings.string(SettingsKey.MEDIA_LANGUAGE_CODE)
        if code:
            return code

        legacy = self.legacy_settings.string(SettingsKey.LEGACY_JW_LANGUAGE)
        if legacy:
            self.set_media_language_code(legacy)
        return legacy

    def set_media_language_code(self, code: str) -> None:
        self.settings.set_value(SettingsKey.MEDIA_LANGUAGE_CODE, code)
