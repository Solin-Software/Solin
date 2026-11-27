"""Qt-backed localization catalogs for Solin's remote-control web interfaces."""

from __future__ import annotations

from collections import Counter
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
from types import MappingProxyType
from typing import Final, Mapping

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication

from ..foundation.resources import application_resource_path
from ..remote_control.validation import JsonObject


log = logging.getLogger(__name__)

REMOTE_CONTROL_CONTEXT: Final = "_RemoteControlWeb"
_PLACEHOLDER: Final = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")

# Literal extraction markers for lupdate. The English JSON catalogs are shared
# with the browsers; contract tests keep both source sets synchronized.
REMOTE_CONTROL_TRANSLATION_SOURCES = (
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin Media"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Media control"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Skip to content"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Install Solin Media"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Install app"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Sign out"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Library"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin content"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "JavaScript required"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enable JavaScript to use Solin Media."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Connecting to Solin…"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "MEDIA CONTROL"),
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
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Set up Solin Media"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Back to sign in"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Trust this Solin once, then install Solin Media for quick, secure access on your local network.",
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
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Tap Install and follow the confirmation prompts."),
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
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Install Solin Media on this device for quick access from your home screen.",
    ),
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
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Signed in. Media control connected."),
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
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin Media was installed on this device."),
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
    # Timer PWA sources
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin Timer"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Meeting timer"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Install Solin Timer"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enable JavaScript to use Solin Timer."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Tap anywhere to hide or show controls"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Control parts"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enter fullscreen"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Exit fullscreen"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Expand"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Expand timer"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Wall clock"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "{title}: {time}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Raised hands"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Hand raised"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Show all"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Close raised hands list"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "TIMER CONTROL"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Sign in to start, stop, and adjust meeting parts from this device.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Return to fullscreen timer"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Signed in. Timer control connected."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Set up Solin Timer"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Back to timer"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Trust this Solin once, then install Solin Timer for fullscreen display and quick control on your local network.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Install Solin Timer on this device for standalone fullscreen access from your home screen.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Open Solin Timer"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "This device was disconnected in Solin. Returned to fullscreen view.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "All remote devices were disconnected in Solin. Returned to fullscreen view.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Live updates and controls will resume automatically.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "CURRENT TIME"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "RUNNING NOW"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "FINISHED"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "No meeting part running"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Over time"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Back to top"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "−1 s"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "+1 s"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Subtract 1 second"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Add 1 second"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Midweek"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Weekend"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Previous week"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Back to this week"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Not the current week"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "No parts in this schedule"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Empty section"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Parts"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Fewer parts in {section}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "More parts in {section}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Start"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Reset"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Started {time}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Duration {duration}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "−1 min"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "+1 min"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Subtract 1 minute from {title}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Add 1 minute to {title}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Duration (MM:SS or minutes)"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Cancel"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Enter a valid duration between 01:00 and 23:59:59.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Solin Timer was installed on this device."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Signed out on this device."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Schedule updated. Try again."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "This timer action is disabled."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "This action or timer part is no longer available."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Schedules"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Schedule type"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Choose a schedule"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "No saved schedules"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Loading schedules…"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Could not load schedules."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "New schedule"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Schedule actions"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Schedule timing and pacing"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Edit part"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Projected finish {finish}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Edit section"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Start next part"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Rename"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Duplicate"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Delete"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Save"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Name"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enter a name."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Copy of {title}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Delete “{title}” and its recorded times?"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Reset results"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Reset all recorded times for “{title}”?"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Restore default title"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "SELECTED PART"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Restart"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Adjust"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Up next"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Planned {duration}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Target {target} · Planned {planned}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Elapsed progress"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Pacing"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Off"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Monitor only"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Monitor and adjust"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "On schedule"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Ahead by {duration}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Behind by {duration}"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Set start and end times to see a forecast."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Set schedule time"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Edit schedule"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Set schedule times and pacing in the Solin app."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Advanced options"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Weight (0–100): higher weights receive more adjustment. A weight of 0 keeps a part fixed when another part has a positive weight. If all weights are 0, eligible parts adjust equally.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Parts allowed to adjust automatically"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "In Automatic mode, only checked parts are shortened or extended to finish on time. Unchecked parts keep their planned duration.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "In Automatic mode, checked parts can be shortened or extended to finish on time. With none checked, all parts of 1 min or more adjust equally.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Under 1 min"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Clear time"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Auto"),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "The new schedule window applies after results are reset.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "No forecast available."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "This action is still in progress."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Check the values and try again."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "The parts and estimates exceed the schedule window."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Close"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Add part"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Add section"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Duration (MM:SS)"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Move to"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Outside sections"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Remove part"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Remove section"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Icon"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Color"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Custom hex"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Use a 6-digit hex color like #41b6c4."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Section preview"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Default"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "None"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Start time"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "End time"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Untracked time (seconds)"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Closing time (seconds)"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Before this part (seconds)"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Adjustment weight (0–100)"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enter a name for each part and section."),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enter a duration between 00:01 and 23:59:59."),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Enter both start and end times, with different values.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Enter a nonnegative whole number of seconds."),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Enter start and end times or clear the time estimates.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Enter a weight between 0 and 100. Parts shorter than one minute cannot be newly selected.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "Could not save the schedule. Your changes are still here.",
    ),
    QT_TRANSLATE_NOOP(
        "_RemoteControlWeb",
        "This schedule changed. Close and reopen to work with the current version.",
    ),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Discard your unsaved changes?"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Treasures"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Ministry"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Living"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Public talk"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Watchtower"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Book"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "People"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Microphone"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Music"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Calendar"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Clock"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Check"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Teal"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Gold"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Crimson"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Amber"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Indigo"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Blue"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Emerald"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Violet"),
    QT_TRANSLATE_NOOP("_RemoteControlWeb", "Neutral"),
)


def _load_message_catalog(path: Path) -> Mapping[str, str]:
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


@lru_cache(maxsize=1)
def remote_media_message_sources() -> Mapping[str, str]:
    return _load_message_catalog(
        application_resource_path("remote_control", "media", "messages.en.json")
    )


@lru_cache(maxsize=1)
def remote_timer_message_sources() -> Mapping[str, str]:
    return _load_message_catalog(
        application_resource_path("remote_control", "timer", "messages.en.json")
    )


@lru_cache(maxsize=1)
def remote_all_message_sources() -> tuple[str, ...]:
    ordered: dict[str, None] = {}
    for catalog in (remote_media_message_sources(), remote_timer_message_sources()):
        for source in catalog.values():
            ordered[source] = None
    return tuple(ordered)


def _build_localization(locale_code: str, sources: Mapping[str, str]) -> JsonObject:
    messages: JsonObject = {}
    for key, source in sources.items():
        translated = QCoreApplication.translate(REMOTE_CONTROL_CONTEXT, source) or source
        if not _valid_remote_translation(source, translated):
            log.warning("Ignoring invalid remote-control translation for %s", key)
            translated = source
        messages[key] = translated
    return {
        "locale": str(locale_code or "en").replace("_", "-"),
        "messages": messages,
    }


def remote_media_localization(locale_code: str) -> JsonObject:
    """Build an immutable-at-source, JSON-safe localization snapshot for Solin Media."""

    return _build_localization(locale_code, remote_media_message_sources())


def remote_timer_localization(locale_code: str) -> JsonObject:
    """Build an immutable-at-source, JSON-safe localization snapshot for Solin Timer."""

    return _build_localization(locale_code, remote_timer_message_sources())


def _valid_remote_translation(source: str, translated: str) -> bool:
    placeholders_match = Counter(_PLACEHOLDER.findall(source)) == Counter(
        _PLACEHOLDER.findall(translated)
    )
    has_unstructured_plural_variants = "|" in translated and "|" not in source
    return placeholders_match and not has_unstructured_plural_variants


__all__ = [
    "REMOTE_CONTROL_CONTEXT",
    "REMOTE_CONTROL_TRANSLATION_SOURCES",
    "remote_all_message_sources",
    "remote_media_localization",
    "remote_media_message_sources",
    "remote_timer_localization",
    "remote_timer_message_sources",
]
