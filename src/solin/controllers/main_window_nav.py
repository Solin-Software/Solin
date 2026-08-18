"""Canonical navigation text sources for the main window chrome."""

from __future__ import annotations

from enum import IntEnum

from PySide6.QtCore import QT_TRANSLATE_NOOP

MAIN_WINDOW_TR_CONTEXT = "MainWindow"

SIDEBAR_TITLE_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Solin")
SIDEBAR_SUBTITLE_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Audio & Video")
SWITCH_PROFILE_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Switch profile")
COLLAPSE_SIDEBAR_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Collapse sidebar")
EXPAND_SIDEBAR_SOURCE = QT_TRANSLATE_NOOP("MainWindow", "Expand sidebar")


class MainPage(IntEnum):
    LIBRARY = 0
    MEETINGS = 1
    BROWSER = 2
    TIMER = 3
    TALK_THEME = 4
    SETTINGS = 5
    PLAYLISTS = 6
    WIFI = 7


NAV_ITEMS = (
    ("nav_library_btn", QT_TRANSLATE_NOOP("MainWindow", "Library"), MainPage.LIBRARY),
    ("nav_meetings_btn", QT_TRANSLATE_NOOP("MainWindow", "Meetings"), MainPage.MEETINGS),
    ("nav_browser_btn", QT_TRANSLATE_NOOP("MainWindow", "Browser"), MainPage.BROWSER),
    ("nav_timer_btn", QT_TRANSLATE_NOOP("MainWindow", "Timer"), MainPage.TIMER),
    ("nav_theme_btn", QT_TRANSLATE_NOOP("MainWindow", "Talk theme"), MainPage.TALK_THEME),
    ("nav_settings_btn", QT_TRANSLATE_NOOP("MainWindow", "Settings"), MainPage.SETTINGS),
    ("nav_playlist_btn", QT_TRANSLATE_NOOP("MainWindow", "Playlists"), MainPage.PLAYLISTS),
    ("nav_wifi_btn", QT_TRANSLATE_NOOP("MainWindow", "Receive via Wi-Fi"), MainPage.WIFI),
)

NAV_LABELS = tuple((attribute, label) for attribute, label, _page in NAV_ITEMS)
