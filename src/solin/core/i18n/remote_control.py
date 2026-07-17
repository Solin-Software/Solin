"""Qt-backed localization catalog for the remote-control web interface."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from types import MappingProxyType
from typing import Final, Mapping

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from ..foundation.resources import application_resource_path
from ..remote_control.contracts import JsonObject


log = logging.getLogger(__name__)

REMOTE_CONTROL_CONTEXT: Final = "_RemoteControlWeb"
_PLACEHOLDER: Final = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")

# Reuse established Solin vocabulary while dedicated web translations are
# completed in each locale. Contexts are explicit because Qt translations are
# context-sensitive; arbitrary cross-context lookup would be unpredictable.
_SHARED_TRANSLATION_CONTEXTS: Final = {
    "Audio": "CacheManagerWidget",
    "Browser": "MainWindow",
    "Choose a meeting": "MediaDestinationDialog",
    "Choose a playlist": "MediaDestinationDialog",
    "Connecting…": "OBSConnectionStatus",
    "Image": "PlaylistPanel",
    "Linked folder": "PlaylistEditView",
    "Loading": "JWMediaCatalogBridge",
    "Marker": "_PlaylistEditView",
    "Media": "SettingsWidget",
    "Meetings": "MainWindow",
    "Midweek meeting": "MeetingScheduleSectionMixin",
    "Next": "ProjectionBar",
    "Next week": "AdvancedTimerPage",
    "PROJECTION": "ScreensSectionMixin",
    "Pause": "MediaTrimDialog",
    "Play": "MediaCard",
    "Playlist": "NameDialog",
    "Playlists": "MainWindow",
    "Previous": "ProjectionBar",
    "Remote control": "RemoteControlSectionMixin",
    "Section": "MediaPlacement",
    "Stop": "BackgroundSongPopup",
    "This week": "MediaDestinationDialog",
    "Try again": "MediaDestinationDialog",
    "Unavailable": "_PubCard",
    "Username": "RemoteControlSectionMixin",
    "Video": "PlaylistPanel",
    "Volume": "ProjectionBar",
    "Watchtower Study": "_PubCard",
    "Weekend meeting": "MeetingScheduleSectionMixin",
}

# Literal extraction markers for lupdate. The English JSON is shared with the
# browser; a contract test keeps both source sets synchronized.
REMOTE_CONTROL_TRANSLATION_SOURCES = (
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin Remote"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Remote control"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Skip to content"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Install Solin"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Install app"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Sign out"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Library"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin content"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "JavaScript required"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enable JavaScript to use Solin remote control."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Connecting to Solin…"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "REMOTE CONTROL"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb", "Sign in to control this Solin projection on the local network."
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb", "Solin is unreachable. Check the network and try again."
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Username"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Password"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Show password"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Hide password"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Sign in"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Signing in…"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Protected connection limited to your local network"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Set up this device"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "SECURE SETUP"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Set up Solin Remote"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Back to sign in"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Trust this Solin once, then install the web app for quick, secure access on your local network.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Setup information is unavailable. Keep Solin open and check this device's network.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Trust this Solin"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "The certificate protects your password and remote commands. Compare the short code below with the one shown in Solin before installing it.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Verification code"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Show full SHA-256 fingerprint"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Download Solin certificate"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Android"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Open Settings and search for “CA certificate”.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Choose Install a certificate › CA certificate, select solin-remote-root.cer from your downloads, and confirm with your device lock.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Approve Android's security notice only after the verification code matches Solin.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Menu names can vary by manufacturer. Opening the downloaded file directly may only redirect you to Settings.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "iPhone or iPad"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Download the certificate in Safari, open Settings, and tap Profile Downloaded. If it is not shown, open General › VPN & Device Management.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb", "Tap Install and follow the confirmation prompts."
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Open General › About › Certificate Trust Settings and enable full trust for Solin Remote Root.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Install the downloaded file as a trusted certificate authority for this device.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Reopen the secure address"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Fully close and reopen the browser. The privacy warning should no longer appear.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "If Chrome still shows the warning after the CA certificate is installed, restart Android once and reopen the address.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "On iPhone or iPad, return to Safari after enabling full trust and reopen the address.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Install the app"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Install Solin Remote to open it without the browser address bar.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Install Solin Remote"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "In Chrome, open the ⋮ menu, choose Add to Home screen, then choose Install rather than Add shortcut.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "In Safari, tap Share › Add to Home Screen, enable Open as Web App, then tap Add.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Use your browser menu and choose Install app or Add to Home Screen.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Continue to sign in"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "This device was disconnected in Solin. Sign in again to reconnect.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "All remote devices were disconnected in Solin. Sign in again to reconnect.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "A new sign-in replaced this session."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enter your username and password."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Signed in. Remote control connected."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Could not sign in."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Incorrect username or password."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Too many attempts. Wait a moment and try again."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Your session expired. Sign in again."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Your session expired."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Connecting…"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Synchronizing…"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Connected"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Reconnecting…"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Offline"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Reconnecting to Solin…"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb", "Controls will be enabled as soon as the connection returns."
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Try now"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Connection failed."),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb", "The library will be updated when the connection returns."
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Meetings"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Playlists"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "MEETINGS"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "PLAYLISTS"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Choose a meeting"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Choose a playlist"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Refresh library"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Back to the list"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Could not load"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Try refreshing the library."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Try again"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "No meetings available"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "No playlists available"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb", "Meetings added to Solin will appear here automatically."
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb", "Playlists added to Solin will appear here automatically."
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Upcoming weeks"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Previous weeks"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Other meetings"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Unidentified week"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "This week"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "No meeting is available this week."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Untitled"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "The complete structure will appear here without changing Solin content.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "This collection is empty"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Changes made in Solin will appear here automatically."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} meeting"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} meetings"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} playlist"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} playlists"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} item"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} items"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} media item"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} media items"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Video"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Audio"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Image"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Document"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Browser"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Announcement"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Screen"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Media"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Unavailable"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Temporary Solin content"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Play {title}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{title}, playing"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Section"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Subsection"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Group"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Marker"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Meeting"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Linked folder"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Playlist"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Next week"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Last week"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "In {count} weeks"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{count} weeks ago"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Midweek meeting"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Weekend meeting"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Memorial"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Life and Ministry"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Watchtower Study"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "PROJECTION"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "NOW PLAYING"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Nothing playing"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "No media is being projected"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Untitled media"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "The media could not be played."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Playback controls"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Previous"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Play"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Next"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Pause"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Resume"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Media position"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{position} of {duration}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Mute"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Restore volume"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Volume"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Stop"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Loading"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin was installed on this device."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "The local session was ended on this device."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "The command was not completed."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin state changed. Updating…"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "This control is protected in Solin."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "This media is no longer available."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Could not send the command."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Could not confirm your session."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin took too long to respond."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Could not connect to Solin."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin sent an invalid response."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin sent an incomplete response."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "This action was not authorized."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Too many attempts. Wait a moment."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin could not complete the request."),
)


@lru_cache(maxsize=1)
def remote_control_message_sources() -> Mapping[str, str]:
    path = application_resource_path("remote_control", "messages.en.json")
    with path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Remote-control message catalog must be a non-empty object")
    messages: dict[str, str] = {}
    for key, source in payload.items():
        if not isinstance(key, str) or not key or not isinstance(source, str) or not source:
            raise ValueError("Remote-control message keys and sources must be non-empty strings")
        messages[key] = source
    return MappingProxyType(messages)


def remote_control_localization(locale_code: str) -> JsonObject:
    """Build an immutable-at-source, JSON-safe localization snapshot."""

    messages: dict[str, str] = {}
    for key, source in remote_control_message_sources().items():
        translated = QCoreApplication.translate(REMOTE_CONTROL_CONTEXT, source) or source
        if translated == source and (shared_context := _SHARED_TRANSLATION_CONTEXTS.get(source)):
            translated = QCoreApplication.translate(shared_context, source) or source
        if set(_PLACEHOLDER.findall(translated)) != set(_PLACEHOLDER.findall(source)):
            log.warning("Ignoring invalid remote-control translation for %s", key)
            translated = source
        messages[key] = translated
    return {
        "locale": str(locale_code or "en").replace("_", "-"),
        "messages": messages,
    }


__all__ = [
    "REMOTE_CONTROL_CONTEXT",
    "REMOTE_CONTROL_TRANSLATION_SOURCES",
    "remote_control_localization",
    "remote_control_message_sources",
]
