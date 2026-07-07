import uuid

import pytest

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.integrations.automation import settings as automation_settings
from solin.core.integrations.automation.settings import (
    AutoShareSettingsStore,
    CameraSettingsStore,
    OBSSettingsStore,
    ZoomSettingsStore,
)
from solin.core.profiles.settings import ProfileSettings


def _profile_settings() -> ProfileSettings:
    return ProfileSettings.for_profile_id(f"automation_settings_{uuid.uuid4().hex}")


def _settings(profile_settings: ProfileSettings) -> SettingsStore:
    return SettingsStore.for_namespace(
        profile_settings.organization,
        QSETTINGS_PREFS_APP,
    )


def test_obs_settings_reads_connection_scenes_and_ndi_configuration():
    profile_settings = _profile_settings()
    settings = _settings(profile_settings)
    store = OBSSettingsStore(settings)
    settings.clear()
    try:
        assert store.is_enabled() is False
        assert store.is_configured() is False
        assert store.websocket_port() == 4455

        store.set_enabled(True)
        store.set_connection(4444, "secret")
        store.set_scenes("Idle", "Media")
        store.set_ndi_enabled(True)
        store.set_ndi_source(" Program ")

        assert store.is_enabled() is True
        assert store.is_configured() is True
        assert store.websocket_port() == 4444
        assert store.password() == "secret"
        assert store.default_scene() == "Idle"
        assert store.media_window_scene() == "Media"
        assert store.has_media_window_scene() is True
        assert store.ndi_enabled() is True
        assert store.ndi_source() == "Program"
        assert store.ndi_stream_configured() is True
    finally:
        settings.clear()


def test_auto_share_settings_uses_current_hotkey_and_relative_target():
    profile_settings = _profile_settings()
    settings = _settings(profile_settings)
    store = AutoShareSettingsStore(settings)
    settings.clear()
    try:
        settings.set_value(SettingsKey.SHARE_ENABLED, True)
        settings.set_value(SettingsKey.SHARE_START_HOTKEY, "LegacyStart")
        settings.set_value(SettingsKey.SHARE_STOP_HOTKEY, "LegacyStop")
        store.set_target_position(0.25, 0.75)

        assert store.ensure_hotkey() == "LegacyStart"
        assert store.hotkey() == "LegacyStart"

        store.set_hotkey("Ctrl+Shift+S")

        assert store.hotkey() == "Ctrl+Shift+S"
        assert store.is_configured() is True
        assert store.target_position() == (0.25, 0.75)
        assert settings.string(SettingsKey.SHARE_START_HOTKEY) == ""
        assert settings.string(SettingsKey.SHARE_STOP_HOTKEY) == ""
    finally:
        settings.clear()


def test_auto_share_settings_requires_enabled_hotkey_and_target():
    profile_settings = _profile_settings()
    settings = _settings(profile_settings)
    store = AutoShareSettingsStore(settings)
    settings.clear()
    try:
        store.set_enabled(True)
        store.set_hotkey("Alt+S")

        assert store.is_configured() is False

        store.set_target_position(0.4, 0.6)

        assert store.is_configured() is True
    finally:
        settings.clear()


@pytest.mark.parametrize(
    ("x_ratio", "y_ratio"),
    [(-0.01, 0.5), (0.5, 1.01), (float("nan"), 0.5)],
)
def test_auto_share_settings_rejects_invalid_relative_targets(x_ratio, y_ratio):
    profile_settings = _profile_settings()
    settings = _settings(profile_settings)
    store = AutoShareSettingsStore(settings)
    settings.clear()
    try:
        with pytest.raises(ValueError, match="between 0 and 1"):
            store.set_target_position(x_ratio, y_ratio)
    finally:
        settings.clear()


def test_auto_share_settings_migrates_legacy_coordinates_once(monkeypatch):
    profile_settings = _profile_settings()
    settings = _settings(profile_settings)
    store = AutoShareSettingsStore(settings)
    settings.clear()
    try:
        settings.set_value("share/click_x", 300)
        settings.set_value("share/click_y", 250)
        monkeypatch.setattr(
            automation_settings,
            "_legacy_target_dialog_bounds",
            lambda _x, _y: (100, 50, 401, 401),
        )

        assert store.target_position() == (0.5, 0.5)
        assert "share/click_x" not in settings.all_keys()
        assert "share/click_y" not in settings.all_keys()
        assert settings.value(SettingsKey.SHARE_TARGET_X_RATIO, -1.0, float) == 0.5
        assert settings.value(SettingsKey.SHARE_TARGET_Y_RATIO, -1.0, float) == 0.5
    finally:
        settings.clear()


def test_auto_share_settings_discards_unmigratable_legacy_coordinates(monkeypatch):
    profile_settings = _profile_settings()
    settings = _settings(profile_settings)
    store = AutoShareSettingsStore(settings)
    settings.clear()
    try:
        settings.set_value("share/click_x", 300)
        settings.set_value("share/click_y", 250)
        monkeypatch.setattr(
            automation_settings,
            "_legacy_target_dialog_bounds",
            lambda _x, _y: None,
        )

        assert store.target_position() == (-1.0, -1.0)
        assert "share/click_x" not in settings.all_keys()
        assert "share/click_y" not in settings.all_keys()
    finally:
        settings.clear()


def test_zoom_and_camera_settings_read_defaults_and_saved_values():
    profile_settings = _profile_settings()
    settings = _settings(profile_settings)
    zoom = ZoomSettingsStore(settings)
    camera = CameraSettingsStore(settings)
    settings.clear()
    try:
        assert zoom.is_enabled() is False
        assert zoom.show_participants() is True
        assert camera.is_enabled() is False
        assert camera.backend() == ""
        assert camera.device_name() == ""

        zoom.set_enabled(True)
        zoom.set_show_participants(False)
        camera.set_enabled(True)
        camera.set_device("dshow", "Cam")

        assert zoom.is_enabled() is True
        assert zoom.show_participants() is False
        assert camera.is_enabled() is True
        assert camera.backend() == "dshow"
        assert camera.device_name() == "Cam"
    finally:
        settings.clear()
