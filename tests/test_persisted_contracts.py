from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from solin.core.foundation.constants import (
    DISPLAY_APP_NAME,
    IS_DEV,
    PLAYLIST_EXTS,
    QSETTINGS_APP_APP,
    QSETTINGS_GLOBAL_APP,
    QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
    QSETTINGS_MONITORS_APP,
    QSETTINGS_NOTIFICATIONS_APP,
    QSETTINGS_ORG_NAME,
    QSETTINGS_PREFS_APP,
    QSETTINGS_PROFILE_ORG_PREFIX,
    QSETTINGS_PROFILE_SCOPED_APPS,
    QSETTINGS_TIMER_APP,
    QT_APPLICATION_NAME,
    QT_ORGANIZATION_NAME,
)
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.playlists import storage as playlist_storage
from solin.core.playlists.schema import SCHEMA_VERSION, create_jwlplaylist_schema
from solin.core.profiles import manager as profile_manager
from solin.widgets.playlist.items import new_playlist_item


EXPECTED_SETTINGS_KEYS = {
    "INSTALL_ID": "install_id",
    "PENDING_PATCH_CLEANUP": "pending_patch_cleanup",
    "LAST_ACTIVE_PROFILE": "last_active_profile",
    "BOOTSTRAP_LANGUAGE": "bootstrap_language",
    "APP_LANGUAGE": "language",
    "MEDIA_LANGUAGE_CODE": "media_language_code",
    "LEGACY_JW_LANGUAGE": "jw_language",
    "WINDOW_WIDTH": "size/width",
    "WINDOW_HEIGHT": "size/height",
    "PLAYBACK_LOOP": "loop",
    "PLAYBACK_ORDER": "playback_order",
    "PLAYBACK_SPEED": "speed",
    "PLAYBACK_VOLUME": "volume",
    "AUTO_DOWNLOAD_ON_PLAY": "auto_download_on_play",
    "MEETINGS_AUTO_DOWNLOAD": "meetings_auto_download",
    "SJJM_ANNOUNCE_MODE": "sjjm_announce_mode",
    "START_VIDEOS_PAUSED": "start_videos_paused",
    "WATCHED_FOLDER_PATH": "watched_folder/path",
    "YEARLY_QUOTE": "yearly_quote",
    "YEARLY_REFERENCE": "yearly_ref",
    "MEETING_MIDWEEK_DAY": "meeting_schedule/midweek_day",
    "MEETING_MIDWEEK_TIME": "meeting_schedule/midweek_time",
    "MEETING_WEEKEND_DAY": "meeting_schedule/weekend_day",
    "MEETING_WEEKEND_TIME": "meeting_schedule/weekend_time",
    "BACKGROUND_SONG_ENABLED": "background_song/enabled",
    "BACKGROUND_SONG_VOLUME": "background_song/volume",
    "BACKGROUND_SONG_FADE_SECONDS": "background_song/fade_seconds",
    "BACKGROUND_SONG_STOP_BEFORE_SECONDS": "background_song/stop_before_seconds",
    "CAMERA_ENABLED": "camera/enabled",
    "CAMERA_BACKEND": "camera/backend",
    "CAMERA_DEVICE_NAME": "camera/device_name",
    "OBS_ENABLED": "obs/enabled",
    "OBS_PORT": "obs/port",
    "OBS_PASSWORD": "obs/password",
    "OBS_DEFAULT_SCENE": "obs/default_scene",
    "OBS_MEDIA_WINDOW_SCENE": "obs/media_window_scene",
    "OBS_NDI_ENABLED": "obs/ndi_enabled",
    "OBS_NDI_SOURCE": "obs/ndi_source",
    "SHARE_ENABLED": "share/enabled",
    "SHARE_HOTKEY": "share/hotkey",
    "SHARE_START_HOTKEY": "share/start_hotkey",
    "SHARE_STOP_HOTKEY": "share/stop_hotkey",
    "SHARE_CLICK_X": "share/click_x",
    "SHARE_CLICK_Y": "share/click_y",
    "ZOOM_ENABLED": "zoom/enabled",
    "ZOOM_SHOW_PARTICIPANTS": "zoom/show_participants",
    "AUTO_KEYS_ENABLED": "auto_keys/enabled",
    "AUTO_KEYS_ACTIONS": "auto_keys/actions",
    "TIMER_CLOCK_CONFIG": "clock_config",
    "TIMER_VISIBLE": "timer_visible",
    "TIMER_LAST_MEETING_TYPE": "last_meeting_type",
    "MONITOR_ALLOCATION": "allocation",
    "NOTIFICATIONS_SEEN_IDS": "seen_ids",
}


def test_qsettings_identity_and_namespaces_are_stable() -> None:
    expected_qt_identity = "SolinDev" if IS_DEV else "Solin"

    assert DISPLAY_APP_NAME == "Solin"
    assert QT_ORGANIZATION_NAME == expected_qt_identity
    assert QT_APPLICATION_NAME == expected_qt_identity
    assert QSETTINGS_ORG_NAME == expected_qt_identity
    assert QSETTINGS_PROFILE_ORG_PREFIX == f"{expected_qt_identity}_"
    assert QSETTINGS_PROFILE_SCOPED_APPS == (
        "ProjectionPrefs",
        "App",
        "MainWindowGeometry",
        "Timer",
        "Monitors",
        "Notifications",
    )
    assert QSETTINGS_PROFILE_SCOPED_APPS == (
        QSETTINGS_PREFS_APP,
        QSETTINGS_APP_APP,
        QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
        QSETTINGS_TIMER_APP,
        QSETTINGS_MONITORS_APP,
        QSETTINGS_NOTIFICATIONS_APP,
    )
    assert QSETTINGS_GLOBAL_APP == "GlobalApp"
    assert QSETTINGS_GLOBAL_APP not in QSETTINGS_PROFILE_SCOPED_APPS
    assert f"{QSETTINGS_PROFILE_ORG_PREFIX}main_hall" == (
        f"{expected_qt_identity}_main_hall"
    )


def test_settings_key_names_and_values_are_stable() -> None:
    actual = {
        name: value
        for name, value in vars(SettingsKey).items()
        if name.isupper() and isinstance(value, str)
    }

    assert actual == EXPECTED_SETTINGS_KEYS


def test_profile_registry_and_directory_layout_are_stable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr("solin.core.foundation.paths.CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(profile_manager.ProfileManager, "_instance", None)
    manager = profile_manager.ProfileManager()
    manager._data_dir = str(tmp_path)
    manager._active_id = "main_hall"
    manager._profiles = [
        profile_manager.ProfileInfo(
            id="main_hall",
            name="Main Hall",
            created_at=1_700_000_000.0,
        )
    ]

    assert profile_manager._normalize_slug(" Main Hall - East ") == "main_hall_east"
    assert profile_manager._unique_slug("main_hall", ["main_hall"]) == "main_hall_2"
    assert manager._profiles_file() == tmp_path / "profiles.json"
    assert manager.profile_dir() == tmp_path / "profiles" / "main_hall"
    assert manager.paths_for() == ProfilePaths.from_roots(
        data_dir=tmp_path,
        cache_dir=cache_dir,
        profile_id="main_hall",
    )
    assert Path(manager.playlists_file()) == (
        tmp_path / "profiles" / "main_hall" / "playlists.json"
    )
    assert Path(manager.meeting_trees_file()) == (
        tmp_path / "profiles" / "main_hall" / "meeting_trees.json"
    )
    assert Path(manager.images_dir()) == tmp_path / "profiles" / "main_hall" / "images"
    assert Path(manager.embedded_dir()) == tmp_path / "profiles" / "main_hall" / "embedded"
    assert manager.paths_for().native_webview_data_dir == (
        tmp_path / "NativeWebView" / "sessions" / "solin_session_main_hall"
    )
    assert manager.paths_for().native_webview_cache_dir == (
        cache_dir / "NativeWebView" / "sessions" / "solin_session_main_hall"
    )

    manager._save_profiles()

    assert json.loads((tmp_path / "profiles.json").read_text(encoding="utf-8")) == {
        "profiles": [
            {
                "id": "main_hall",
                "name": "Main Hall",
                "created_at": 1_700_000_000.0,
            }
        ]
    }
    assert list(tmp_path.glob(".*.tmp")) == []


def test_internal_playlist_file_and_item_schema_are_stable(
    tmp_path: Path,
) -> None:
    playlists_file = tmp_path / "profiles" / "main_hall" / "playlists.json"
    storage_paths = playlist_storage.PlaylistStoragePaths(
        playlists_file=playlists_file,
        pending_deletions_file=tmp_path / "pending.json",
    )

    item = new_playlist_item("Welcome", "C:/media/welcome.mp4")
    playlist = {
        "id": "playlist-1",
        "name": "Main Meeting",
        "items": [item],
        "sections": [],
        "markers": [],
    }

    playlist_storage.save_playlists([playlist], storage_paths)

    assert json.loads(playlists_file.read_text(encoding="utf-8")) == {
        "playlists": [playlist]
    }
    assert playlist_storage.load_playlists(storage_paths) == [playlist]
    assert set(item) == {
        "id",
        "title",
        "url",
        "type",
        "auto_title",
        "key_symbol",
        "track",
        "issue_tag",
        "doc_id",
        "meps_language",
    }
    assert uuid.UUID(item["id"]).version == 4
    assert item == {
        "id": item["id"],
        "title": "Welcome",
        "url": "C:/media/welcome.mp4",
        "type": "video",
        "auto_title": False,
        "key_symbol": None,
        "track": None,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 0,
    }


def test_jwlplaylist_extension_and_sqlite_schema_are_stable() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        create_jwlplaylist_schema(connection)
        user_version = connection.execute("PRAGMA user_version").fetchone()
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    finally:
        connection.close()

    assert PLAYLIST_EXTS == frozenset({".jwlplaylist"})
    assert SCHEMA_VERSION == 14
    assert user_version == (14,)
    assert {
        "PlaylistItem",
        "IndependentMedia",
        "PlaylistItemIndependentMediaMap",
        "Location",
        "PlaylistItemLocationMap",
        "Tag",
        "TagMap",
        "PlaylistItemMarker",
    } <= table_names
