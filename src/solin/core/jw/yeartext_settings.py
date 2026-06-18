from __future__ import annotations

from dataclasses import dataclass

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


@dataclass(frozen=True, slots=True)
class YeartextSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "YeartextSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def text(self) -> tuple[str, str]:
        return (
            self.settings.string(SettingsKey.YEARLY_QUOTE),
            self.settings.string(SettingsKey.YEARLY_REFERENCE),
        )

    def set_text(self, quote: str, reference: str) -> None:
        self.settings.set_value(SettingsKey.YEARLY_QUOTE, str(quote or ""), sync=False)
        self.settings.set_value(
            SettingsKey.YEARLY_REFERENCE,
            str(reference or ""),
            sync=False,
        )
        self.settings.sync()
