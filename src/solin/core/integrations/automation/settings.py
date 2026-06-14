from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class _ProfilePrefsSettings:
    settings: SettingsStore

    @classmethod
    def for_profile_settings(cls, profile_settings: ProfileSettings):
        return cls(
            SettingsStore.for_namespace(
                profile_settings.organization,
                QSETTINGS_PREFS_APP,
            )
        )


class OBSConnectionSettings(Protocol):
    def is_configured(self) -> bool: ...

    def websocket_port(self, default: int = 4455) -> int: ...

    def password(self) -> str: ...


@dataclass(frozen=True, slots=True)
class OBSSettingsStore(_ProfilePrefsSettings):
    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.OBS_ENABLED, False, bool))

    def websocket_port(self, default: int = 4455) -> int:
        return _as_int(self.settings.value(SettingsKey.OBS_PORT, default, int), default)

    def is_configured(self) -> bool:
        return self.websocket_port(default=0) > 0

    def password(self) -> str:
        return self.settings.string(SettingsKey.OBS_PASSWORD)

    def default_scene(self) -> str:
        return self.settings.string(SettingsKey.OBS_DEFAULT_SCENE)

    def media_window_scene(self) -> str:
        return self.settings.string(SettingsKey.OBS_MEDIA_WINDOW_SCENE)

    def has_media_window_scene(self) -> bool:
        scene = self.media_window_scene()
        return bool(scene and not scene.startswith("-") and not scene.startswith("\u2014"))

    def ndi_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.OBS_NDI_ENABLED, False, bool))

    def ndi_source(self) -> str:
        return self.settings.string(SettingsKey.OBS_NDI_SOURCE).strip()

    def ndi_stream_configured(self) -> bool:
        return self.ndi_enabled() and bool(self.ndi_source())


class ZoomParticipantSettings(Protocol):
    def show_participants(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ZoomSettingsStore(_ProfilePrefsSettings):
    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.ZOOM_ENABLED, False, bool))

    def show_participants(self) -> bool:
        return bool(
            self.settings.value(SettingsKey.ZOOM_SHOW_PARTICIPANTS, True, bool)
        )


@dataclass(frozen=True, slots=True)
class AutoShareSettingsStore(_ProfilePrefsSettings):
    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.SHARE_ENABLED, False, bool))

    def hotkey(self) -> str:
        hotkey = self.settings.string(SettingsKey.SHARE_HOTKEY).strip()
        if hotkey:
            return hotkey
        return (
            self.settings.string(SettingsKey.SHARE_START_HOTKEY).strip()
            or self.settings.string(SettingsKey.SHARE_STOP_HOTKEY).strip()
        )

    def is_configured(self) -> bool:
        return self.is_enabled() and bool(self.hotkey())

    def click_position(self) -> tuple[int, int]:
        return (
            _as_int(self.settings.value(SettingsKey.SHARE_CLICK_X, -1, int), -1),
            _as_int(self.settings.value(SettingsKey.SHARE_CLICK_Y, -1, int), -1),
        )


@dataclass(frozen=True, slots=True)
class CameraSettingsStore(_ProfilePrefsSettings):
    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.CAMERA_ENABLED, False, bool))

    def backend(self) -> str:
        return self.settings.string(SettingsKey.CAMERA_BACKEND)

    def device_name(self) -> str:
        return self.settings.string(SettingsKey.CAMERA_DEVICE_NAME)
