"""Profile-scoped persistence for shared media-countdown preferences."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

from .media_countdown_automation import (
    DEFAULT_MEDIA_COUNTDOWN_LEAD_SECONDS,
    MediaCountdownAutomationConfig,
    clamp_media_countdown_lead_seconds,
)
from .models import MediaCountdownPresentation


def normalize_media_countdown_presentation(value: Any) -> MediaCountdownPresentation:
    if isinstance(value, MediaCountdownPresentation):
        return value
    try:
        return MediaCountdownPresentation(str(value))
    except ValueError:
        return MediaCountdownPresentation.CIRCULAR


@dataclass(frozen=True, slots=True)
class MediaCountdownSettings:
    presentation: MediaCountdownPresentation = MediaCountdownPresentation.CIRCULAR
    automation: MediaCountdownAutomationConfig = field(
        default_factory=MediaCountdownAutomationConfig
    )

    def normalized(self) -> "MediaCountdownSettings":
        return MediaCountdownSettings(
            presentation=normalize_media_countdown_presentation(self.presentation),
            automation=self.automation.normalized(),
        )


@dataclass(frozen=True, slots=True)
class MediaCountdownSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "MediaCountdownSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def load(self) -> MediaCountdownSettings:
        return MediaCountdownSettings(
            presentation=normalize_media_countdown_presentation(
                self.settings.string(SettingsKey.MEDIA_COUNTDOWN_PRESENTATION)
                or MediaCountdownPresentation.CIRCULAR.value
            ),
            automation=MediaCountdownAutomationConfig(
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


__all__ = [
    "MediaCountdownSettings",
    "MediaCountdownSettingsStore",
    "normalize_media_countdown_presentation",
]
