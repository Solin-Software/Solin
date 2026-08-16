"""Canonical QSettings keys used across Solin."""

from __future__ import annotations

from typing import Final


class SettingsKey:
    INSTALL_ID: Final = "install_id"
    PENDING_PATCH_CLEANUP: Final = "pending_patch_cleanup"
    LAST_ACTIVE_PROFILE: Final = "last_active_profile"
    BOOTSTRAP_LANGUAGE: Final = "bootstrap_language"
    APP_LANGUAGE: Final = "language"
    APP_THEME: Final = "theme"
    REMOTE_CONTROL_ENABLED: Final = "remote_control/enabled"
    REMOTE_CONTROL_NETWORK_SELECTION: Final = "remote_control/network_selection"
    REMOTE_CONTROL_CREDENTIALS: Final = "remote_control/credentials"
    REMOTE_CONTROL_ONBOARDING_SEEN: Final = "remote_control/onboarding_seen"
    MEDIA_LANGUAGE_CODE: Final = "media_language_code"
    LEGACY_JW_LANGUAGE: Final = "jw_language"

    WINDOW_GEOMETRY: Final = "geometry"
    LEGACY_WINDOW_WIDTH: Final = "size/width"
    LEGACY_WINDOW_HEIGHT: Final = "size/height"
    SIDEBAR_COLLAPSED: Final = "sidebar/collapsed"

    PLAYBACK_LOOP: Final = "loop"
    PLAYBACK_ORDER: Final = "playback_order"
    PLAYBACK_SPEED: Final = "speed"
    PLAYBACK_VOLUME: Final = "volume"
    IMAGE_MATCH_PROJECTION_ASPECT: Final = "image_projection/match_projection_aspect"
    IMAGE_CONSTRAIN_TO_FRAME: Final = "image_projection/constrain_to_frame"
    BROWSER_ZOOM_FACTOR: Final = "browser/zoom_factor"

    AUTO_DOWNLOAD_ON_PLAY: Final = "auto_download_on_play"
    MEETINGS_AUTO_DOWNLOAD: Final = "meetings_auto_download"
    SJJM_ANNOUNCE_MODE: Final = "sjjm_announce_mode"
    START_VIDEOS_PAUSED: Final = "start_videos_paused"
    PLAYBACK_PROTECTION_ENABLED: Final = "playback_protection_enabled"
    TALK_THEME_CUSTOM_COLORS: Final = "talk_theme/custom_colors"
    TALK_THEME_FOLLOW_OUTPUT_ASPECT: Final = "talk_theme/follow_output_aspect"
    LEGACY_TALK_THEME_CUSTOM_TEXT_COLORS: Final = "talk_theme/custom_text_colors"
    WATCHED_FOLDER_PATH: Final = "watched_folder/path"
    YEARLY_QUOTE: Final = "yearly_quote"
    YEARLY_REFERENCE: Final = "yearly_ref"

    MEETING_MIDWEEK_DAY: Final = "meeting_schedule/midweek_day"
    MEETING_MIDWEEK_TIME: Final = "meeting_schedule/midweek_time"
    MEETING_WEEKEND_DAY: Final = "meeting_schedule/weekend_day"
    MEETING_WEEKEND_TIME: Final = "meeting_schedule/weekend_time"

    BACKGROUND_SONG_ENABLED: Final = "background_song/enabled"
    BACKGROUND_SONG_VOLUME: Final = "background_song/volume"
    BACKGROUND_SONG_FADE_SECONDS: Final = "background_song/fade_seconds"
    BACKGROUND_SONG_STOP_BEFORE_SECONDS: Final = "background_song/stop_before_seconds"

    MEDIA_COUNTDOWN_AUTOMATIC_ENABLED: Final = "media_countdown/automatic_enabled"
    MEDIA_COUNTDOWN_LEAD_SECONDS: Final = "media_countdown/lead_seconds"
    MEDIA_COUNTDOWN_PRESENTATION: Final = "media_countdown/presentation"

    CAMERA_ENABLED: Final = "camera/enabled"
    CAMERA_BACKEND: Final = "camera/backend"
    CAMERA_DEVICE_NAME: Final = "camera/device_name"

    OBS_ENABLED: Final = "obs/enabled"
    OBS_PORT: Final = "obs/port"
    OBS_PASSWORD: Final = "obs/password"
    OBS_DEFAULT_SCENE: Final = "obs/default_scene"
    OBS_MEDIA_WINDOW_SCENE: Final = "obs/media_window_scene"
    OBS_NDI_ENABLED: Final = "obs/ndi_enabled"
    OBS_NDI_SOURCE: Final = "obs/ndi_source"

    SHARE_ENABLED: Final = "share/enabled"
    SHARE_HOTKEY: Final = "share/hotkey"
    SHARE_START_HOTKEY: Final = "share/start_hotkey"
    SHARE_STOP_HOTKEY: Final = "share/stop_hotkey"
    SHARE_CLICK_X: Final = "share/click_x"
    SHARE_CLICK_Y: Final = "share/click_y"

    ZOOM_ENABLED: Final = "zoom/enabled"
    ZOOM_SHOW_PARTICIPANTS: Final = "zoom/show_participants"

    AUTO_KEYS_ENABLED: Final = "auto_keys/enabled"
    AUTO_KEYS_ACTIONS: Final = "auto_keys/actions"

    TIMER_CLOCK_CONFIG: Final = "clock_config"
    TIMER_VISIBLE: Final = "timer_visible"
    TIMER_LAST_MEETING_TYPE: Final = "last_meeting_type"

    MONITOR_ALLOCATION: Final = "allocation"
    NOTIFICATIONS_SEEN_IDS: Final = "seen_ids"
