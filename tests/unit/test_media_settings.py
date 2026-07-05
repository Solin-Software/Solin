import uuid

from solin.core.foundation.constants import ORDER_NEXT, ORDER_OFF, QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.jw.background_song_settings import (
    DEFAULT_BACKGROUND_SONG_FADE_SECONDS,
    DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS,
    DEFAULT_BACKGROUND_SONG_VOLUME,
    BackgroundSongSettingsStore,
)
from solin.core.media.settings import MediaSettingsStore, ProjectionPlaybackSettingsStore
from solin.core.profiles.settings import ProfileSettings


def _settings() -> SettingsStore:
    profile_settings = ProfileSettings.for_profile_id(
        f"media_settings_{uuid.uuid4().hex}"
    )
    return SettingsStore.for_namespace(
        profile_settings.organization,
        QSETTINGS_PREFS_APP,
    )


def test_media_settings_reads_defaults_and_persists_flags():
    settings = _settings()
    store = MediaSettingsStore(settings)
    settings.clear()
    try:
        assert store.auto_download_on_play() is False
        assert store.meetings_auto_download() is False
        assert store.sjjm_announce_mode() is False
        assert store.start_videos_paused() is False
        assert store.playback_protection_enabled() is True

        store.set_auto_download_on_play(True)
        store.set_meetings_auto_download(True)
        store.set_sjjm_announce_mode(True)
        store.set_start_videos_paused(True)
        store.set_playback_protection_enabled(False)

        assert store.auto_download_on_play() is True
        assert store.meetings_auto_download() is True
        assert store.sjjm_announce_mode() is True
        assert store.start_videos_paused() is True
        assert store.playback_protection_enabled() is False
    finally:
        settings.clear()


def test_projection_playback_settings_validate_and_clamp_values():
    settings = _settings()
    store = ProjectionPlaybackSettingsStore(settings)
    settings.clear()
    try:
        assert store.loop_enabled() is False
        assert store.playback_order() == ORDER_OFF
        assert store.speed() == 1.0
        assert store.volume() == 0.80
        assert store.image_match_projection_aspect() is False
        assert store.image_constrain_to_frame() is False

        store.set_loop_enabled(True)
        store.set_playback_order(ORDER_NEXT)
        store.set_speed(8.0)
        store.set_volume(-1.0)
        store.set_image_match_projection_aspect(True)
        store.set_image_constrain_to_frame(True)

        assert store.loop_enabled() is True
        assert store.playback_order() == ORDER_NEXT
        assert store.speed() == 4.0
        assert store.volume() == 0.0
        assert store.image_match_projection_aspect() is True
        assert store.image_constrain_to_frame() is True

        settings.set_value(SettingsKey.PLAYBACK_ORDER, "bad")
        settings.set_value(SettingsKey.PLAYBACK_SPEED, "bad")
        settings.set_value(SettingsKey.PLAYBACK_VOLUME, "bad")

        assert store.playback_order() == ORDER_OFF
        assert store.speed() == 1.0
        assert store.volume() == 0.80
    finally:
        settings.clear()


def test_background_song_settings_clamps_timing_and_volume_values():
    settings = _settings()
    store = BackgroundSongSettingsStore(settings)
    settings.clear()
    try:
        assert store.is_enabled() is False
        assert store.volume_percent() == DEFAULT_BACKGROUND_SONG_VOLUME
        assert store.fade_seconds() == DEFAULT_BACKGROUND_SONG_FADE_SECONDS
        assert store.stop_before_seconds() == DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS

        store.set_enabled(True)
        store.set_volume_percent(150)
        store.set_fade_seconds(80)
        store.set_stop_before_seconds(500)

        assert store.is_enabled() is True
        assert store.volume_percent() == 100
        assert store.fade_seconds() == 30
        assert store.stop_before_seconds() == 300

        settings.set_value(SettingsKey.BACKGROUND_SONG_VOLUME, "bad")
        settings.set_value(SettingsKey.BACKGROUND_SONG_FADE_SECONDS, "bad")
        settings.set_value(SettingsKey.BACKGROUND_SONG_STOP_BEFORE_SECONDS, "bad")

        assert store.volume_percent() == DEFAULT_BACKGROUND_SONG_VOLUME
        assert store.fade_seconds() == DEFAULT_BACKGROUND_SONG_FADE_SECONDS
        assert store.stop_before_seconds() == DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS
    finally:
        settings.clear()
