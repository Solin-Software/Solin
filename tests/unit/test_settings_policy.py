from __future__ import annotations

import ast
from pathlib import Path

from solin.core.foundation.constants import (
    QSETTINGS_APP_APP,
    QSETTINGS_GLOBAL_APP,
    QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
    QSETTINGS_MONITORS_APP,
    QSETTINGS_NOTIFICATIONS_APP,
    QSETTINGS_PREFS_APP,
    QSETTINGS_PROFILE_ORG_PREFIX,
    QSETTINGS_PROFILE_SCOPED_APPS,
    QSETTINGS_TIMER_APP,
)
from solin.core.foundation import settings_store
from solin.core.foundation.settings_store import ProfileAppSettingsStore
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.profiles import infrastructure as profile_infrastructure
from solin.core.profiles import models as profile_models
from solin.core.profiles.settings import ProfileSettings


EXPECTED_SETTINGS_KEYS = {
    "INSTALL_ID": "install_id",
    "PENDING_PATCH_CLEANUP": "pending_patch_cleanup",
    "LAST_ACTIVE_PROFILE": "last_active_profile",
    "BOOTSTRAP_LANGUAGE": "bootstrap_language",
    "APP_LANGUAGE": "language",
    "APP_THEME": "theme",
    "TOOLBAR_HOVER_POPUPS": "toolbar/hover_popups",
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
    "REMOTE_CONTROL_ENABLED": "remote_control/enabled",
    "REMOTE_CONTROL_NETWORK_SELECTION": "remote_control/network_selection",
    "REMOTE_CONTROL_CREDENTIALS": "remote_control/credentials",
    "REMOTE_CONTROL_ONBOARDING_SEEN": "remote_control/onboarding_seen",
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


def test_settings_key_values_are_stable():
    actual = {
        name: value
        for name, value in vars(SettingsKey).items()
        if name.isupper() and isinstance(value, str)
    }

    assert actual == EXPECTED_SETTINGS_KEYS


def test_profile_scoped_settings_apps_are_complete():
    assert QSETTINGS_PROFILE_SCOPED_APPS == (
        QSETTINGS_PREFS_APP,
        QSETTINGS_APP_APP,
        QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
        QSETTINGS_TIMER_APP,
        QSETTINGS_MONITORS_APP,
        QSETTINGS_NOTIFICATIONS_APP,
    )
    assert QSETTINGS_GLOBAL_APP not in QSETTINGS_PROFILE_SCOPED_APPS


def test_deleting_profile_clears_every_profile_scoped_settings_app(monkeypatch, tmp_path):
    cleared: list[tuple[str, str]] = []

    class FakeSettings:
        def __init__(self, organization: str, application: str):
            self.organization = organization
            self.application = application

        def clear(self) -> None:
            cleared.append((self.organization, self.application))

        def allKeys(self):
            return []

        def sync(self) -> None:
            pass

        def value(self, _key, default=None, _type=None):
            return default

        def setValue(self, _key, _value) -> None:
            pass

    monkeypatch.setattr(settings_store, "QSettings", FakeSettings)

    registry = profile_infrastructure.JsonProfileRegistry(tmp_path / "profiles.json")
    registry.save(
        [
            profile_models.ProfileInfo("kept", "Kept"),
            profile_models.ProfileInfo("removed", "Removed"),
        ]
    )
    manager = profile_infrastructure.create_local_profile_service(
        tmp_path,
        global_settings=settings_store.GlobalSettingsStore.create(),
        profile_app_settings_for=_profile_app_settings_for,
    )
    manager.set_active("kept")

    assert manager.delete_profile("removed") is True

    profile_org = f"{QSETTINGS_PROFILE_ORG_PREFIX}removed"
    assert cleared == [(profile_org, application) for application in QSETTINGS_PROFILE_SCOPED_APPS]


def _profile_app_settings_for(profile_id: str) -> ProfileAppSettingsStore:
    return ProfileSettings.for_profile_id(profile_id).app_settings()


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_settings_receiver(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        name = node.id.lower().strip("_")
        return (
            name in {"s", "s2", "s3", "gs", "prefs", "settings", "src_s", "dst_s"}
            or name.endswith("prefs")
            or name.endswith("settings")
        )
    if isinstance(node, ast.Attribute):
        name = node.attr.lower().strip("_")
        return name in {"prefs", "settings"} or name.endswith("prefs")
    if isinstance(node, ast.Call):
        return _call_name(node.func) in {"prefs", "QSettings"}
    return False


def _local_settings_aliases(tree: ast.AST) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if not isinstance(target, ast.Name) or value is None:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            aliases.add(target.id)
        elif isinstance(value, ast.Attribute) and _call_name(value.value) == "SettingsKey":
            aliases.add(target.id)
        elif isinstance(value, ast.Name) and value.id.startswith("QSETTINGS_"):
            aliases.add(target.id)
    return aliases


def test_qsettings_accesses_use_canonical_keys_and_namespaces():
    paths = [Path("main.py"), *sorted(Path("src/solin").rglob("*.py"))]
    offenders: list[str] = []

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        local_aliases = _local_settings_aliases(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            call_name = _call_name(node.func)
            if call_name == "prefs" and node.args:
                namespace = node.args[0]
                if isinstance(namespace, ast.Constant) and isinstance(namespace.value, str):
                    offenders.append(f"{path}:{node.lineno}: literal prefs namespace")
                elif isinstance(namespace, ast.Name) and namespace.id in local_aliases:
                    offenders.append(f"{path}:{node.lineno}: local prefs namespace alias")

            if call_name == "QSettings" and len(node.args) >= 2:
                namespace = node.args[1]
                if isinstance(namespace, ast.Constant) and isinstance(namespace.value, str):
                    offenders.append(f"{path}:{node.lineno}: literal QSettings namespace")
                elif isinstance(namespace, ast.Name) and namespace.id in local_aliases:
                    offenders.append(f"{path}:{node.lineno}: local QSettings namespace alias")

            if not isinstance(node.func, ast.Attribute) or not node.args:
                continue
            if node.func.attr not in {"value", "setValue", "remove"}:
                continue
            if not _is_settings_receiver(node.func.value):
                continue

            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                offenders.append(f"{path}:{node.lineno}: literal settings key")
            elif isinstance(key, ast.Name) and key.id in local_aliases:
                offenders.append(f"{path}:{node.lineno}: local settings key alias")

    assert offenders == []
