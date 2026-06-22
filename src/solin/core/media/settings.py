from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from solin.core.foundation.constants import (
    ORDER_NEXT,
    ORDER_OFF,
    ORDER_RANDOM,
    QSETTINGS_PREFS_APP,
)
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

_PLAYBACK_ORDERS = frozenset({ORDER_OFF, ORDER_NEXT, ORDER_RANDOM})


def _float_setting(value: object, default: float) -> float:
    if not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


@dataclass(frozen=True, slots=True)
class ProjectionPlaybackSettingsStore:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(
        cls,
        profile_settings: ProfileSettings,
    ) -> "ProjectionPlaybackSettingsStore":
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )

    def loop_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.PLAYBACK_LOOP, False, bool))

    def set_loop_enabled(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.PLAYBACK_LOOP, bool(enabled))

    def playback_order(self) -> str:
        order = self.settings.string(SettingsKey.PLAYBACK_ORDER, ORDER_OFF)
        return order if order in _PLAYBACK_ORDERS else ORDER_OFF

    def set_playback_order(self, order: str) -> None:
        self.settings.set_value(
            SettingsKey.PLAYBACK_ORDER,
            order if order in _PLAYBACK_ORDERS else ORDER_OFF,
        )

    def speed(self) -> float:
        speed = _float_setting(
            self.settings.value(SettingsKey.PLAYBACK_SPEED, 1.0),
            1.0,
        )
        return max(0.25, min(4.0, speed))

    def set_speed(self, speed: float) -> None:
        self.settings.set_value(SettingsKey.PLAYBACK_SPEED, max(0.25, min(4.0, speed)))

    def volume(self) -> float:
        volume = _float_setting(
            self.settings.value(SettingsKey.PLAYBACK_VOLUME, 0.80),
            0.80,
        )
        return max(0.0, min(1.0, volume))

    def set_volume(self, volume: float) -> None:
        self.settings.set_value(SettingsKey.PLAYBACK_VOLUME, max(0.0, min(1.0, volume)))

    def image_match_projection_aspect(self) -> bool:
        return bool(
            self.settings.value(
                SettingsKey.IMAGE_MATCH_PROJECTION_ASPECT,
                False,
                bool,
            )
        )

    def set_image_match_projection_aspect(self, enabled: bool) -> None:
        self.settings.set_value(
            SettingsKey.IMAGE_MATCH_PROJECTION_ASPECT,
            bool(enabled),
        )

    def image_constrain_to_frame(self) -> bool:
        return bool(
            self.settings.value(
                SettingsKey.IMAGE_CONSTRAIN_TO_FRAME,
                False,
                bool,
            )
        )

    def set_image_constrain_to_frame(self, enabled: bool) -> None:
        self.settings.set_value(
            SettingsKey.IMAGE_CONSTRAIN_TO_FRAME,
            bool(enabled),
        )
