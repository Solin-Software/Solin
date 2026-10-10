"""
constants.py — Solin
====================
Single source of truth for shared static project constants.
Import them from here instead of redefining them in each module.

Data/cache paths:
    Runtime paths belong to RuntimePaths/ProfilePaths and must be
    injected by the bootstrap. Constants in this module must remain pure.

Usage:
    from solin.core.foundation.constants import APP_VERSION, QSETTINGS_ORG_NAME, ...
"""

from __future__ import annotations

import sys

from solin.version import VERSION

IS_DEV: bool = "__compiled__" not in globals()
LIBOBS_SIDECAR_ARGUMENT: str = "--scene-engine-sidecar"

# Qt / QSettings identity
# Use a separate development namespace to keep registry entries and data
# in QStandardPaths, caches, and profiles separate from the production installation.
DISPLAY_APP_NAME: str = "Solin"
QT_ORGANIZATION_NAME: str = "SolinDev" if IS_DEV else "Solin"
QT_APPLICATION_NAME: str = "SolinDev" if IS_DEV else "Solin"

# QSettings uses (organization, application). Keep the "application" names as
# stable internal identifiers, and isolate development/production by organization.
QSETTINGS_ORG_NAME: str = QT_ORGANIZATION_NAME
QSETTINGS_PROFILE_ORG_PREFIX: str = f"{QSETTINGS_ORG_NAME}_"
QSETTINGS_PREFS_APP: str = "ProjectionPrefs"
QSETTINGS_APP_APP: str = "App"
QSETTINGS_GLOBAL_APP: str = "GlobalApp"
QSETTINGS_MAIN_WINDOW_GEOMETRY_APP: str = "MainWindowGeometry"
QSETTINGS_TIMER_APP: str = "Timer"
QSETTINGS_MONITORS_APP: str = "Monitors"
QSETTINGS_NOTIFICATIONS_APP: str = "Notifications"
QSETTINGS_PROFILE_SCOPED_APPS: tuple[str, ...] = (
    QSETTINGS_PREFS_APP,
    QSETTINGS_APP_APP,
    QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
    QSETTINGS_TIMER_APP,
    QSETTINGS_MONITORS_APP,
    QSETTINGS_NOTIFICATIONS_APP,
)

# Application version
# Public CalVer; native package metadata is derived by core.releases.version.
APP_VERSION: str = VERSION

# Stable identifier sent to Solin APIs. Do not pass raw sys.platform values to the
# backend: "win32"/"darwin" are Python names, not product names.
if sys.platform == "win32":
    APP_PLATFORM: str = "windows"
elif sys.platform == "darwin":
    APP_PLATFORM: str = "macos"
elif sys.platform.startswith("linux"):
    APP_PLATFORM: str = "linux"
else:
    APP_PLATFORM: str = sys.platform

# The complete native scenes feature is currently qualified only on Windows.
# This capability is the single source of truth for exposing its UI, starting
# its engine, and selecting the legacy Qt camera workflow on other platforms.
NATIVE_SCENES_SUPPORTED: bool = APP_PLATFORM == "windows"

# Temporary streaming files (cache OFF)
# Required prefix for every temporary file and lock file created by Solin.
# Startup cleanup of orphaned files filters EXCLUSIVELY by this prefix,
# avoiding interference with other applications' lock files.
TEMP_STREAM_PREFIX: str = "Solin_stream_"

# ── IPC ────────────────────────────────────────────────────────────────────────
IPC_SERVER_NAME: str = "SolinDev_IPC_v1" if IS_DEV else "Solin_IPC_v1"
IPC_TIMEOUT_MS: int = 800

# Remote notifications
# JSON notification endpoint URL.
if IS_DEV:
    NOTIFICATION_API_URL: str = "http://localhost:5000/v1/notifications"
else:
    NOTIFICATION_API_URL: str = "https://solinav.vercel.app/v1/notifications"
# Delay (ms) between showing the main window and checking notifications.
# Ensure the UI is visible and responsive before fetching.
NOTIFICATION_CHECK_DELAY_MS: int = 1500

# Playback order
ORDER_OFF: str = "off"
ORDER_NEXT: str = "next"
ORDER_RANDOM: str = "random"

# Projection features
# If True, enable interactive zoom/pan when projecting a live browser tab.
# If False (the default), disable this feature for live projections.
ALLOW_ZOOM_PAN_ON_LIVE_TAB: bool = False

# Return after a media scene
# Shared contract for native Scenes and the OBS integration. If True
# (the default), the previous base scene remains the destination when media ends. If
# False, automation resets the base scene to the configured default when
# entering a new media session. An explicit operator override can still
# select a different return destination for the current session.
MEMORIZE_PRE_MEDIA_SCENE: bool = True

JWL_PLAYLIST_EXTS: frozenset[str] = frozenset({".jwlplaylist"})
SOLIN_PLAYLIST_EXTS: frozenset[str] = frozenset({".solinplaylist"})
# Legacy dispatch remains JWL-only. Native packages are accepted exclusively by
# their dedicated top-level import/open flow and must not enter flat JWL ingest.
PLAYLIST_EXTS: frozenset[str] = JWL_PLAYLIST_EXTS
PDF_EXTS: frozenset[str] = frozenset({".pdf"})
JWPUB_EXTS: frozenset[str] = frozenset({".jwpub"})
PPTX_EXTS: frozenset[str] = frozenset({".pptx", ".ppt", ".odp"})
DOCX_EXTS: frozenset[str] = frozenset({".docx", ".doc", ".odt", ".rtf"})

# Image quality
THUMB_JPEG_QUALITY: int = 85  # 0–100; used whenever a thumbnail is saved

# API cache
CACHE_TTL_DAYS: int = 10

# JW video quality (GETPUBMEDIALINKS)
# Preferred resolution for all videos fetched from the JW.org API.
# Used in jw/media_api.py (sjjm/sjj songs and osg clips).
VIDEO_PREFERRED_QUALITY: str = "720p"

# Fallback direction when the preferred resolution is unavailable:
# "below" → try lower resolutions first, then higher ones (conservative default)
# "above" → try higher resolutions first, then lower ones
VIDEO_QUALITY_FALLBACK_DIR: str = "below"

# Canonical order of known quality levels, from highest to lowest.
# Used by pick_quality() in jw/media_api.py.
VIDEO_QUALITY_ORDER: tuple[str, ...] = (
    "1080p",
    "720p",
    "480p",
    "360p",
    "240p",
    "180p",
)

# Delay (ms) after showing the main window before checking updates (after notifications).
UPDATE_CHECK_DELAY_MS: int = 5000
