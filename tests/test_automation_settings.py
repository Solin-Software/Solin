import uuid

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
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

        settings.set_value(SettingsKey.OBS_ENABLED, True)
        settings.set_value(SettingsKey.OBS_PORT, "4444")
        settings.set_value(SettingsKey.OBS_PASSWORD, "secret")
        settings.set_value(SettingsKey.OBS_DEFAULT_SCENE, "Idle")
        settings.set_value(SettingsKey.OBS_MEDIA_WINDOW_SCENE, "Media")
        settings.set_value(SettingsKey.OBS_NDI_ENABLED, True)
        settings.set_value(SettingsKey.OBS_NDI_SOURCE, " Program ")

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


def test_auto_share_settings_prefers_current_hotkey_before_legacy_keys():
    profile_settings = _profile_settings()
    settings = _settings(profile_settings)
    store = AutoShareSettingsStore(settings)
    settings.clear()
    try:
        settings.set_value(SettingsKey.SHARE_ENABLED, True)
        settings.set_value(SettingsKey.SHARE_HOTKEY, "Ctrl+Shift+S")
        settings.set_value(SettingsKey.SHARE_START_HOTKEY, "LegacyStart")
        settings.set_value(SettingsKey.SHARE_STOP_HOTKEY, "LegacyStop")
        settings.set_value(SettingsKey.SHARE_CLICK_X, "320")
        settings.set_value(SettingsKey.SHARE_CLICK_Y, "240")

        assert store.hotkey() == "Ctrl+Shift+S"
        assert store.is_configured() is True
        assert store.click_position() == (320, 240)

        settings.set_value(SettingsKey.SHARE_HOTKEY, "")

        assert store.hotkey() == "LegacyStart"
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

        settings.set_value(SettingsKey.ZOOM_ENABLED, True)
        settings.set_value(SettingsKey.ZOOM_SHOW_PARTICIPANTS, False)
        settings.set_value(SettingsKey.CAMERA_ENABLED, True)
        settings.set_value(SettingsKey.CAMERA_BACKEND, "dshow")
        settings.set_value(SettingsKey.CAMERA_DEVICE_NAME, "Cam")

        assert zoom.is_enabled() is True
        assert zoom.show_participants() is False
        assert camera.is_enabled() is True
        assert camera.backend() == "dshow"
        assert camera.device_name() == "Cam"
    finally:
        settings.clear()
