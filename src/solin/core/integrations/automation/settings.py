from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey, _LegacySettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.profiles.settings import ProfileSettings

from .auto_key_actions import AutoKeyAction, parse_actions, serialize_actions


def _as_int(value: object, default: int) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


_UNCONFIGURED_TARGET = (-1.0, -1.0)


def _valid_target_position(x_ratio: float, y_ratio: float) -> bool:
    return 0.0 <= x_ratio <= 1.0 and 0.0 <= y_ratio <= 1.0


def _legacy_target_dialog_bounds(x: int, y: int) -> tuple[int, int, int, int] | None:
    from .screen_share import find_zoom_share_dialog_bounds_at_point

    return find_zoom_share_dialog_bounds_at_point(
        x,
        y,
        require_identified_dialog=True,
    )


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

    def set_enabled(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.OBS_ENABLED, bool(enabled))

    def websocket_port(self, default: int = 4455) -> int:
        return _as_int(self.settings.value(SettingsKey.OBS_PORT, default, int), default)

    def raw_websocket_port(self) -> str:
        return self.settings.string(SettingsKey.OBS_PORT)

    def is_configured(self) -> bool:
        return self.websocket_port(default=0) > 0

    def password(self) -> str:
        return self.settings.string(SettingsKey.OBS_PASSWORD)

    def set_connection(self, port: int, password: str) -> None:
        self.settings.set_value(SettingsKey.OBS_PORT, int(port), sync=False)
        self.settings.set_value(SettingsKey.OBS_PASSWORD, password, sync=False)
        self.settings.sync()

    def default_scene(self) -> str:
        return self.settings.string(SettingsKey.OBS_DEFAULT_SCENE)

    def media_window_scene(self) -> str:
        return self.settings.string(SettingsKey.OBS_MEDIA_WINDOW_SCENE)

    def set_scenes(self, default_scene: str, media_scene: str) -> None:
        self.settings.set_value(SettingsKey.OBS_DEFAULT_SCENE, default_scene, sync=False)
        self.settings.set_value(
            SettingsKey.OBS_MEDIA_WINDOW_SCENE,
            media_scene,
            sync=False,
        )
        self.settings.sync()

    def has_media_window_scene(self) -> bool:
        scene = self.media_window_scene()
        return bool(scene and not scene.startswith("-") and not scene.startswith("\u2014"))

    def ndi_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.OBS_NDI_ENABLED, False, bool))

    def set_ndi_enabled(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.OBS_NDI_ENABLED, bool(enabled))

    def ndi_source(self) -> str:
        return self.settings.string(SettingsKey.OBS_NDI_SOURCE).strip()

    def set_ndi_source(self, source: str) -> None:
        self.settings.set_value(SettingsKey.OBS_NDI_SOURCE, source)

    def ndi_stream_configured(self) -> bool:
        return self.ndi_enabled() and bool(self.ndi_source())


class ZoomParticipantSettings(Protocol):
    def show_participants(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ZoomSettingsStore(_ProfilePrefsSettings):
    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.ZOOM_ENABLED, False, bool))

    def set_enabled(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.ZOOM_ENABLED, bool(enabled))

    def show_participants(self) -> bool:
        return bool(
            self.settings.value(SettingsKey.ZOOM_SHOW_PARTICIPANTS, True, bool)
        )

    def set_show_participants(self, show: bool) -> None:
        self.settings.set_value(SettingsKey.ZOOM_SHOW_PARTICIPANTS, bool(show))


@dataclass(frozen=True, slots=True)
class AutoKeySettingsStore(_ProfilePrefsSettings):
    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.AUTO_KEYS_ENABLED, False, bool))

    def set_enabled(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.AUTO_KEYS_ENABLED, bool(enabled))

    def actions(self) -> list[AutoKeyAction]:
        return parse_actions(self.settings.string(SettingsKey.AUTO_KEYS_ACTIONS, "[]"))

    def save_actions(self, actions: list[AutoKeyAction]) -> None:
        self.settings.set_value(SettingsKey.AUTO_KEYS_ACTIONS, serialize_actions(actions))

    def action_count_for_event(self, event: str) -> int:
        return sum(
            1
            for action in self.actions()
            if action.enabled and action.event == event
        )


@dataclass(frozen=True, slots=True)
class AutoShareSettingsStore(_ProfilePrefsSettings):
    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.SHARE_ENABLED, False, bool))

    def set_enabled(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.SHARE_ENABLED, bool(enabled))

    def hotkey(self) -> str:
        hotkey = self.settings.string(SettingsKey.SHARE_HOTKEY).strip()
        if hotkey:
            return hotkey
        return (
            self.settings.string(SettingsKey.SHARE_START_HOTKEY).strip()
            or self.settings.string(SettingsKey.SHARE_STOP_HOTKEY).strip()
        )

    def ensure_hotkey(self) -> str:
        hotkey = self.hotkey()
        if hotkey and not self.settings.string(SettingsKey.SHARE_HOTKEY).strip():
            self.set_hotkey(hotkey)
        return hotkey

    def set_hotkey(self, hotkey: str) -> None:
        self.settings.set_value(SettingsKey.SHARE_HOTKEY, hotkey, sync=False)
        self.settings.remove(SettingsKey.SHARE_START_HOTKEY, sync=False)
        self.settings.remove(SettingsKey.SHARE_STOP_HOTKEY, sync=False)
        self.settings.sync()

    def is_configured(self) -> bool:
        return self.is_enabled() and bool(self.hotkey()) and self.has_target_position()

    def target_position(self) -> tuple[float, float]:
        x_ratio = _as_float(
            self.settings.value(SettingsKey.SHARE_TARGET_X_RATIO, -1.0),
            -1.0,
        )
        y_ratio = _as_float(
            self.settings.value(SettingsKey.SHARE_TARGET_Y_RATIO, -1.0),
            -1.0,
        )
        if _valid_target_position(x_ratio, y_ratio):
            self._remove_legacy_target_keys()
            return x_ratio, y_ratio
        return self._migrate_legacy_target_position()

    def has_target_position(self) -> bool:
        return _valid_target_position(*self.target_position())

    def set_target_position(self, x_ratio: float, y_ratio: float) -> None:
        x_ratio = float(x_ratio)
        y_ratio = float(y_ratio)
        if not _valid_target_position(x_ratio, y_ratio):
            raise ValueError("Share target ratios must be finite values between 0 and 1")
        self.settings.set_value(
            SettingsKey.SHARE_TARGET_X_RATIO,
            x_ratio,
            sync=False,
        )
        self.settings.set_value(
            SettingsKey.SHARE_TARGET_Y_RATIO,
            y_ratio,
            sync=False,
        )
        self.settings.remove(_LegacySettingsKey.SHARE_CLICK_X, sync=False)
        self.settings.remove(_LegacySettingsKey.SHARE_CLICK_Y, sync=False)
        self.settings.sync()

    def _migrate_legacy_target_position(self) -> tuple[float, float]:
        keys = self.settings.all_keys()
        if (
            _LegacySettingsKey.SHARE_CLICK_X not in keys
            and _LegacySettingsKey.SHARE_CLICK_Y not in keys
        ):
            return _UNCONFIGURED_TARGET

        x = _as_int(self.settings.value(_LegacySettingsKey.SHARE_CLICK_X, -1), -1)
        y = _as_int(self.settings.value(_LegacySettingsKey.SHARE_CLICK_Y, -1), -1)
        bounds = _legacy_target_dialog_bounds(x, y) if x >= 0 and y >= 0 else None
        self.settings.remove(_LegacySettingsKey.SHARE_CLICK_X, sync=False)
        self.settings.remove(_LegacySettingsKey.SHARE_CLICK_Y, sync=False)

        if bounds is None:
            self.settings.sync()
            return _UNCONFIGURED_TARGET

        left, top, width, height = bounds
        if width <= 1 or height <= 1:
            self.settings.sync()
            return _UNCONFIGURED_TARGET

        x_ratio = (x - left) / (width - 1)
        y_ratio = (y - top) / (height - 1)
        if not _valid_target_position(x_ratio, y_ratio):
            self.settings.sync()
            return _UNCONFIGURED_TARGET

        self.settings.set_value(
            SettingsKey.SHARE_TARGET_X_RATIO,
            x_ratio,
            sync=False,
        )
        self.settings.set_value(
            SettingsKey.SHARE_TARGET_Y_RATIO,
            y_ratio,
            sync=False,
        )
        self.settings.sync()
        return x_ratio, y_ratio

    def _remove_legacy_target_keys(self) -> None:
        keys = self.settings.all_keys()
        if (
            _LegacySettingsKey.SHARE_CLICK_X not in keys
            and _LegacySettingsKey.SHARE_CLICK_Y not in keys
        ):
            return
        self.settings.remove(_LegacySettingsKey.SHARE_CLICK_X, sync=False)
        self.settings.remove(_LegacySettingsKey.SHARE_CLICK_Y, sync=False)
        self.settings.sync()


@dataclass(frozen=True, slots=True)
class CameraSettingsStore(_ProfilePrefsSettings):
    def is_enabled(self) -> bool:
        return bool(self.settings.value(SettingsKey.CAMERA_ENABLED, False, bool))

    def set_enabled(self, enabled: bool) -> None:
        self.settings.set_value(SettingsKey.CAMERA_ENABLED, bool(enabled))

    def backend(self) -> str:
        return self.settings.string(SettingsKey.CAMERA_BACKEND)

    def device_name(self) -> str:
        return self.settings.string(SettingsKey.CAMERA_DEVICE_NAME)

    def set_device(self, backend: str, name: str) -> None:
        self.settings.set_value(SettingsKey.CAMERA_BACKEND, backend, sync=False)
        self.settings.set_value(SettingsKey.CAMERA_DEVICE_NAME, name, sync=False)
        self.settings.sync()
