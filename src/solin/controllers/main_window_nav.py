"""Canonical navigation text sources for the main window chrome."""

from __future__ import annotations

from PySide6.QtCore import QT_TRANSLATE_NOOP

MAIN_WINDOW_TR_CONTEXT = "MainWindow"

SIDEBAR_TITLE_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Solin")
SIDEBAR_SUBTITLE_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Audio & Video")
SWITCH_PROFILE_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Switch profile")
COLLAPSE_SIDEBAR_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Collapse sidebar")
EXPAND_SIDEBAR_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Expand sidebar")

NAV_LABELS = (
    ("nav_songs_btn", QT_TRANSLATE_NOOP("MainWindow", "Songs")),
    ("nav_meetings_btn", QT_TRANSLATE_NOOP("MainWindow", "Meetings")),
    ("nav_browser_btn", QT_TRANSLATE_NOOP("MainWindow", "Browser")),
    ("nav_clips_btn", QT_TRANSLATE_NOOP("MainWindow", "Original Songs")),
    ("nav_timer_btn", QT_TRANSLATE_NOOP("MainWindow", "Timer")),
    ("nav_theme_btn", QT_TRANSLATE_NOOP("MainWindow", "Talk theme")),
    ("nav_settings_btn", QT_TRANSLATE_NOOP("MainWindow", "Settings")),
    ("nav_playlist_btn", QT_TRANSLATE_NOOP("MainWindow", "Playlists")),
    ("nav_cache_btn", QT_TRANSLATE_NOOP("MainWindow", "Saved Media")),
    ("nav_wifi_btn", QT_TRANSLATE_NOOP("MainWindow", "Receive via Wi-Fi")),
    ("nav_scenes_btn", QT_TRANSLATE_NOOP("MainWindow", "Scenes")),
)
