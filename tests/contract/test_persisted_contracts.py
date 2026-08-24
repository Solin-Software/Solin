from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT as PROJECT_ROOT
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
from solin.core.foundation import runtime_paths as runtime_paths_module
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import (
    GlobalSettingsStore,
    ProfileAppSettingsStore,
    SettingsStore,
)
from solin.core.playlists import storage as playlist_storage
from solin.core.playlists.schema import SCHEMA_VERSION, create_jwlplaylist_schema
from solin.core.profiles import infrastructure as profile_infrastructure
from solin.core.profiles.application import ProfileRegistryLoadError
from solin.core.profiles import models as profile_models
from solin.core.profiles import settings as profile_settings_module
from solin.core.profiles.settings import ProfileSettings
from solin.core.playlists.items import create_playlist_item


EXPECTED_SETTINGS_KEYS = {
    "INSTALL_ID": "install_id",
    "PENDING_PATCH_CLEANUP": "pending_patch_cleanup",
    "LAST_ACTIVE_PROFILE": "last_active_profile",
    "BOOTSTRAP_LANGUAGE": "bootstrap_language",
    "APP_LANGUAGE": "language",
    "APP_THEME": "theme",
    "REMOTE_CONTROL_ENABLED": "remote_control/enabled",
    "REMOTE_CONTROL_NETWORK_SELECTION": "remote_control/network_selection",
    "REMOTE_CONTROL_CREDENTIALS": "remote_control/credentials",
    "REMOTE_CONTROL_ONBOARDING_SEEN": "remote_control/onboarding_seen",
    "MEDIA_LANGUAGE_CODE": "media_language_code",
    "LEGACY_JW_LANGUAGE": "jw_language",
    "WINDOW_GEOMETRY": "geometry",
    "LEGACY_WINDOW_WIDTH": "size/width",
    "LEGACY_WINDOW_HEIGHT": "size/height",
    "SIDEBAR_COLLAPSED": "sidebar/collapsed",
    "PLAYBACK_LOOP": "loop",
    "PLAYBACK_ORDER": "playback_order",
    "PLAYBACK_SPEED": "speed",
    "PLAYBACK_VOLUME": "volume",
    "IMAGE_MATCH_PROJECTION_ASPECT": "image_projection/match_projection_aspect",
    "IMAGE_CONSTRAIN_TO_FRAME": "image_projection/constrain_to_frame",
    "BROWSER_ZOOM_FACTOR": "browser/zoom_factor",
    "AUTO_DOWNLOAD_ON_PLAY": "auto_download_on_play",
    "MEETINGS_AUTO_DOWNLOAD": "meetings_auto_download",
    "SJJM_ANNOUNCE_MODE": "sjjm_announce_mode",
    "START_VIDEOS_PAUSED": "start_videos_paused",
    "PLAYBACK_PROTECTION_ENABLED": "playback_protection_enabled",
    "TALK_THEME_CUSTOM_COLORS": "talk_theme/custom_colors",
    "TALK_THEME_FOLLOW_OUTPUT_ASPECT": "talk_theme/follow_output_aspect",
    "LEGACY_TALK_THEME_CUSTOM_TEXT_COLORS": "talk_theme/custom_text_colors",
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
    "MEDIA_COUNTDOWN_AUTOMATIC_ENABLED": "media_countdown/automatic_enabled",
    "MEDIA_COUNTDOWN_LEAD_SECONDS": "media_countdown/lead_seconds",
    "MEDIA_COUNTDOWN_PRESENTATION": "media_countdown/presentation",
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


def _isolated_global_settings() -> GlobalSettingsStore:
    organization = f"SolinTest_{uuid.uuid4().hex}"
    return GlobalSettingsStore(SettingsStore.for_namespace(organization, QSETTINGS_GLOBAL_APP))


def _profile_app_settings_for(profile_id: str) -> ProfileAppSettingsStore:
    return ProfileSettings.for_profile_id(profile_id).app_settings()


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
    assert f"{QSETTINGS_PROFILE_ORG_PREFIX}main_hall" == (f"{expected_qt_identity}_main_hall")


def test_windows_installer_qsettings_cleanup_tracks_current_namespaces() -> None:
    setup_iss = (PROJECT_ROOT / "packaging" / "windows" / "installer" / "setup.iss").read_text(
        encoding="utf-8"
    )
    current_apps = {
        QSETTINGS_PREFS_APP,
        QSETTINGS_APP_APP,
        QSETTINGS_GLOBAL_APP,
        QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
        QSETTINGS_TIMER_APP,
        QSETTINGS_MONITORS_APP,
        QSETTINGS_NOTIFICATIONS_APP,
    }

    for app_name in current_apps:
        assert f"Software\\Solin\\{app_name}" in setup_iss
    assert "RegGetSubkeyNames(HKCU, 'Software', Names)" in setup_iss
    assert "Copy(KeyName, 1, 6) = 'Solin_'" in setup_iss


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
    registry = profile_infrastructure.JsonProfileRegistry(tmp_path / "profiles.json")
    profile = profile_models.ProfileInfo(
        id="main_hall",
        name="Main Hall",
        created_at=1_700_000_000.0,
    )
    registry.save([profile])
    manager = profile_infrastructure.create_local_profile_service(
        tmp_path,
        cache_dir,
        global_settings=_isolated_global_settings(),
        profile_app_settings_for=_profile_app_settings_for,
    )
    manager.set_active("main_hall")
    runtime = profile_infrastructure.ProfileRuntimeContextFactory(
        tmp_path,
        cache_dir,
        profile_settings_for=ProfileSettings.for_profile_id,
    ).create("main_hall")

    assert profile_models.profile_slug(" Main Hall - East ") == "main_hall_east"
    assert profile_models.unique_profile_slug("main_hall", {"main_hall"}) == "main_hall_2"
    assert registry.path == tmp_path / "profiles.json"
    assert runtime.paths.profile_dir == tmp_path / "profiles" / "main_hall"
    assert runtime.paths == ProfilePaths.from_roots(
        data_dir=tmp_path,
        cache_dir=cache_dir,
        profile_id="main_hall",
    )
    assert runtime.settings == ProfileSettings.for_profile_id("main_hall")
    assert runtime.paths.playlists_file == (tmp_path / "profiles" / "main_hall" / "playlists.json")
    assert runtime.paths.meeting_trees_file == (
        tmp_path / "profiles" / "main_hall" / "meeting_trees.json"
    )
    assert runtime.paths.pending_deletions_file == (
        tmp_path / "profiles" / "main_hall" / "pending_cleanup.json"
    )
    assert "pending_del_file" not in runtime_paths_module.RuntimePaths.__dataclass_fields__
    assert runtime.paths.images_dir == tmp_path / "profiles" / "main_hall" / "images"
    assert runtime.paths.embedded_dir == tmp_path / "profiles" / "main_hall" / "embedded"
    assert runtime.paths.talk_theme_file == (
        tmp_path / "profiles" / "main_hall" / "talk_theme.json"
    )
    assert runtime.paths.talk_theme_assets_dir == (
        tmp_path / "profiles" / "main_hall" / "talk_theme_assets"
    )
    assert runtime.paths.scenes_file == (tmp_path / "profiles" / "main_hall" / "scenes.json")
    assert runtime.paths.scenes_runtime_file == (
        tmp_path / "profiles" / "main_hall" / "scenes_runtime.json"
    )
    assert runtime.paths.scenes_assets_dir == (
        tmp_path / "profiles" / "main_hall" / "scenes_assets"
    )
    assert runtime.paths.profile_cache_dir == cache_dir / "profiles" / "main_hall"
    assert runtime.paths.thumb_cache_dir == (cache_dir / "profiles" / "main_hall" / "thumbs")
    assert runtime.paths.meeting_thumb_cache_dir == (
        cache_dir / "profiles" / "main_hall" / "meeting_thumbs"
    )
    assert runtime.paths.pdf_pages_dir == (cache_dir / "profiles" / "main_hall" / "pdf_pages")
    assert runtime.paths.pptx_pages_dir == (cache_dir / "profiles" / "main_hall" / "pptx_pages")
    assert runtime.paths.docx_pages_dir == (cache_dir / "profiles" / "main_hall" / "docx_pages")
    assert runtime.paths.native_webview_data_dir == (
        tmp_path / "NativeWebView" / "sessions" / "solin_session_main_hall"
    )
    assert runtime.paths.native_webview_data_root == tmp_path / "NativeWebView"
    assert runtime.paths.native_webview_cache_dir == (
        cache_dir / "NativeWebView" / "sessions" / "solin_session_main_hall"
    )

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


def test_profile_services_are_isolated_instances(tmp_path: Path) -> None:
    first = profile_infrastructure.create_local_profile_service(
        tmp_path / "first",
        global_settings=_isolated_global_settings(),
        profile_app_settings_for=_profile_app_settings_for,
    )
    second = profile_infrastructure.create_local_profile_service(
        tmp_path / "second",
        global_settings=_isolated_global_settings(),
        profile_app_settings_for=_profile_app_settings_for,
    )

    created = first.create_profile("Main Hall")

    assert created.id == "main_hall"
    assert [profile.id for profile in first.profiles] == ["main_hall"]
    assert second.profiles == []
    assert first is not second


def test_corrupt_profile_registry_is_not_treated_as_first_run(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "profiles.json"
    registry_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ProfileRegistryLoadError):
        profile_infrastructure.create_local_profile_service(
            tmp_path,
            global_settings=_isolated_global_settings(),
            profile_app_settings_for=_profile_app_settings_for,
        )

    assert registry_path.read_text(encoding="utf-8") == "{broken"


def test_legacy_file_copy_preserves_sources_until_cleanup(tmp_path: Path) -> None:
    (tmp_path / "playlists.json").write_text('{"playlists": []}', encoding="utf-8")
    images = tmp_path / "images"
    images.mkdir()
    (images / "logo.png").write_bytes(b"image")
    storage = profile_infrastructure.LocalProfileStorage(tmp_path)

    storage.copy_legacy_data("main_hall")

    assert (tmp_path / "playlists.json").is_file()
    assert (images / "logo.png").is_file()
    assert (tmp_path / "profiles" / "main_hall" / "playlists.json").is_file()
    assert (tmp_path / "profiles" / "main_hall" / "images" / "logo.png").is_file()

    storage.cleanup_legacy_data()

    assert not (tmp_path / "playlists.json").exists()
    assert not images.exists()


def test_profile_runtime_context_requires_an_explicit_valid_profile_id(
    tmp_path: Path,
) -> None:
    runtime = profile_infrastructure.ProfileRuntimeContextFactory(
        tmp_path,
        profile_settings_for=ProfileSettings.for_profile_id,
    )

    with pytest.raises(ValueError, match="cannot be empty"):
        runtime.create("")


def test_profile_service_has_no_singleton_or_service_locator() -> None:
    source = profile_infrastructure.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")

    assert "_instance" not in text
    assert "def __new__(" not in text
    assert "\ndef get()" not in text


def test_profile_storage_paths_are_not_mutable_globals() -> None:
    runtime_paths_source = Path(runtime_paths_module.__file__).read_text(encoding="utf-8")
    manager_source = Path(profile_infrastructure.__file__).read_text(encoding="utf-8")

    assert not Path(runtime_paths_module.__file__).with_name("paths.py").exists()
    assert "IMAGES_DIR" not in runtime_paths_source
    assert "EMBEDDED_DIR" not in runtime_paths_source
    assert "from_legacy_globals" not in runtime_paths_source
    assert "_redirect_global_paths" not in manager_source


def test_profile_settings_namespace_is_immutable_and_explicit() -> None:
    settings = ProfileSettings.for_profile_id("main_hall")
    source = Path(profile_settings_module.__file__).read_text(encoding="utf-8")

    assert settings.organization == f"{QSETTINGS_PROFILE_ORG_PREFIX}main_hall"
    assert "_ORG" not in vars(profile_settings_module)
    assert "def set_org(" not in source
    assert "def current_org(" not in source
    assert "def prefs(" not in source


def test_production_code_has_no_active_profile_settings_module_alias() -> None:
    offenders = [
        path
        for path in Path("src/solin").rglob("*.py")
        if "from solin.core.profiles import settings as" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_profile_settings_namespaces_are_isolated() -> None:
    first = ProfileSettings.for_profile_id("settings_isolation_first")
    second = ProfileSettings.for_profile_id("settings_isolation_second")
    first_store = first.app_settings()
    second_store = second.app_settings()
    first_store.settings.clear()
    second_store.settings.clear()
    try:
        first_store.set_app_language("pt_BR")

        assert first_store.app_language() == "pt_BR"
        assert second_store.app_language() == ""
    finally:
        first_store.settings.clear()
        second_store.settings.clear()


def test_internal_playlist_file_and_item_schema_are_stable(
    tmp_path: Path,
) -> None:
    playlists_file = tmp_path / "profiles" / "main_hall" / "playlists.json"
    storage_paths = playlist_storage.PlaylistStoragePaths(
        playlists_file=playlists_file,
        pending_deletions_file=tmp_path / "pending.json",
    )
    repository = playlist_storage.PlaylistRepository.from_paths(storage_paths)

    item = create_playlist_item("Welcome", "C:/media/welcome.mp4")
    playlist = {
        "id": "playlist-1",
        "name": "Main Meeting",
        "items": [item],
        "sections": [],
        "markers": [],
    }

    repository.save([playlist])

    assert json.loads(playlists_file.read_text(encoding="utf-8")) == {
        "version": 1,
        "playlists": [playlist],
    }
    assert repository.load() == [playlist]
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
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
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
