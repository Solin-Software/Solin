from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

DEFAULT_BACKGROUND_SONG_VOLUME = 25
DEFAULT_BACKGROUND_SONG_FADE_SECONDS = 5
DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS = 10


def _clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def clamp_background_song_volume(value: Any) -> int:
    return _clamp_int(value, 0, 100, DEFAULT_BACKGROUND_SONG_VOLUME)


def clamp_background_song_fade_seconds(value: Any) -> int:
    return _clamp_int(value, 0, 30, DEFAULT_BACKGROUND_SONG_FADE_SECONDS)


def clamp_background_song_stop_before_seconds(value: Any) -> int:
    return _clamp_int(value, 0, 300, DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS)


@dataclass(frozen=True, slots=True)
class BackgroundSongSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "BackgroundSongSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.BACKGROUND_SONG_ENABLED, False, bool))

    def set_enabled(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.BACKGROUND_SONG_ENABLED, bool(enabled))

    def volume_percent(self) -> int:
        return clamp_background_song_volume(
            self.settings.value(
                SettingsKey.BACKGROUND_SONG_VOLUME,
                DEFAULT_BACKGROUND_SONG_VOLUME,
            )
        )

    def set_volume_percent(self, value: int) -> None:
        self.settings.set_value(SettingsKey.BACKGROUND_SONG_VOLUME, clamp_background_song_volume(value))

    def fade_seconds(self) -> int:
        return clamp_background_song_fade_seconds(
            self.settings.value(
                SettingsKey.BACKGROUND_SONG_FADE_SECONDS,
                DEFAULT_BACKGROUND_SONG_FADE_SECONDS,
            )
        )

    def set_fade_seconds(self, value: int) -> None:
        self.settings.set_value(
            SettingsKey.BACKGROUND_SONG_FADE_SECONDS,
            clamp_background_song_fade_seconds(value),
        )

    def stop_before_seconds(self) -> int:
        return clamp_background_song_stop_before_seconds(
            self.settings.value(
                SettingsKey.BACKGROUND_SONG_STOP_BEFORE_SECONDS,
                DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS,
            )
        )

    def set_stop_before_seconds(self, value: int) -> None:
        self.settings.set_value(
            SettingsKey.BACKGROUND_SONG_STOP_BEFORE_SECONDS,
            clamp_background_song_stop_before_seconds(value),
        )
