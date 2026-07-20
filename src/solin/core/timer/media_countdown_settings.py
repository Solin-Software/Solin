"""Profile-scoped persistence for automatic media countdown preferences."""

from __future__ import annotations

from dataclasses import dataclass

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

from .media_countdown_automation import (
    DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS,
    MediaCountdownAutomationConfig,
    clamp_media_countdown_lead_seconds,
    normalize_media_countdown_presentation,
)
from .models import MediaCountdownPresentation


@dataclass(frozen=True, slots=True)
class MediaCountdownAutomationSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "MediaCountdownAutomationSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def load(self) -> MediaCountdownAutomationConfig:
        return MediaCountdownAutomationConfig(
            enabled=bool(
                self.settings.value(
                    SettingsKey.MEDIA_COUNTDOWN_AUTOMATIC_ENABLED,
                    False,
                    bool,
                )
            ),
            lead_seconds=clamp_media_countdown_lead_seconds(
                self.settings.value(
                    SettingsKey.MEDIA_COUNTDOWN_LEAD_SECONDS,
                    DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS,
                )
            ),
            presentation=normalize_media_countdown_presentation(
                self.settings.string(SettingsKey.MEDIA_COUNTDOWN_PRESENTATION)
                or MediaCountdownPresentation.CIRCULAR.value
            ),
        )

    def set_enabled(self, enabled: bool) -> None:
        self.settings.set_value(
            SettingsKey.MEDIA_COUNTDOWN_AUTOMATIC_ENABLED,
            bool(enabled),
        )

    def set_lead_seconds(self, seconds: int) -> None:
        self.settings.set_value(
            SettingsKey.MEDIA_COUNTDOWN_LEAD_SECONDS,
            clamp_media_countdown_lead_seconds(seconds),
        )

    def set_presentation(self, presentation: MediaCountdownPresentation | str) -> None:
        normalized = normalize_media_countdown_presentation(presentation)
        self.settings.set_value(
            SettingsKey.MEDIA_COUNTDOWN_PRESENTATION,
            normalized.value,
        )


__all__ = ["MediaCountdownAutomationSettingsStore"]
