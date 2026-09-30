"""Settings information architecture and searchable, value-free metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    kind: str = "toggle"
    description: str = ""
    options: str = ""
    action: str = ""
    secondary_action: str = ""
    secondary_label: str = ""
    visible_when: str = ""
    enabled_when: str = ""
    description_key: str = ""


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    domain: str
    rows: tuple[Setting, ...]
    collapsible: bool = False
    status_key: str = ""
    visible_when: str = ""
    children: tuple["Group", ...] = ()
    feedback_key: str = ""
    error_key: str = ""


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    description: str
    icon: str
    groups: tuple[Group, ...]


SECTIONS = (
    Section("appearance", QT_TRANSLATE_NOOP("SettingsWidget", "Appearance and languages"),
            QT_TRANSLATE_NOOP("SettingsWidget", "Theme and languages"), "appearance", (
        Group("appearance", QT_TRANSLATE_NOOP("SettingsWidget", "Appearance"), "general", (
            Setting("themeId", QT_TRANSLATE_NOOP("SettingsWidget", "Theme"), "select", options="themeOptions"),
        )),
        Group("languages", QT_TRANSLATE_NOOP("SettingsWidget", "Language"), "general", (
            Setting("interfaceLanguage", QT_TRANSLATE_NOOP("SettingsWidget", "Interface"), "select", options="interfaceLanguageOptions"),
            Setting("mediaLanguage", QT_TRANSLATE_NOOP("SettingsWidget", "JW Media"), "select", options="mediaLanguageOptions"),
            Setting("mediaLanguagesError", QT_TRANSLATE_NOOP("SettingsWidget", "Update languages"), "action", action="refreshMediaLanguages", visible_when="mediaLanguagesError"),
        )),
    )),
    Section("media", QT_TRANSLATE_NOOP("SettingsWidget", "Media and files"),
            QT_TRANSLATE_NOOP("SettingsWidget", "Playback, downloads and folders"), "media", (
        Group("playback", QT_TRANSLATE_NOOP("SettingsWidget", "Playback"), "general", (
            Setting("startVideosPaused", QT_TRANSLATE_NOOP("SettingsWidget", "Start videos paused"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Videos open paused so you can start them manually.")),
            Setting("playbackProtection", QT_TRANSLATE_NOOP("SettingsWidget", "Playback protection"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Prevents media changes and seeking while audio or video is playing. Pause first to make changes.")),
            Setting("songAnnouncement", QT_TRANSLATE_NOOP("SettingsWidget", "Song Announcement Mode"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Song starts muted for title display. Press play to start.")),
        )),
        Group("downloads", QT_TRANSLATE_NOOP("SettingsWidget", "Downloads"), "general", (
            Setting("autoDownloadOnPlay", QT_TRANSLATE_NOOP("SettingsWidget", "Auto-download on play"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Downloads the playing media for offline use.")),
        )),
        Group("files", QT_TRANSLATE_NOOP("SettingsWidget", "Folders"), "general", (
            Setting("watchedFolder", QT_TRANSLATE_NOOP("SettingsWidget", "Link Folder"), "folder", description=QT_TRANSLATE_NOOP("SettingsWidget", "Sync folder (Dropbox, OneDrive, etc.) shown as playlists."), action="chooseFolder", secondary_action="clearFolder", secondary_label=QT_TRANSLATE_NOOP("SettingsWidget", "Clear")),
        )),
    )),
    Section("meetings", QT_TRANSLATE_NOOP("SettingsWidget", "Meetings"),
            QT_TRANSLATE_NOOP("SettingsWidget", "Schedule and preparation"), "meetings", (
        Group("schedule", QT_TRANSLATE_NOOP("SettingsWidget", "Meeting schedule"), "general", (
            Setting(
                "congregationName",
                QT_TRANSLATE_NOOP("SettingsWidget", "Fill in from jw.org"),
                "congregation",
                description=QT_TRANSLATE_NOOP(
                    "SettingsWidget",
                    "Search your congregation to fill the days and times below.",
                ),
                description_key="congregationStatusText",
            ),
            Setting("midweekDay", QT_TRANSLATE_NOOP("SettingsWidget", "Midweek meeting"), "select", options="dayOptions"),
            Setting(
                "midweekTime",
                QT_TRANSLATE_NOOP("SettingsWidget", "Time"),
                "time",
                enabled_when="midweekTimeEnabled",
            ),
            Setting("weekendDay", QT_TRANSLATE_NOOP("SettingsWidget", "Weekend meeting"), "select", options="dayOptions"),
            Setting(
                "weekendTime",
                QT_TRANSLATE_NOOP("SettingsWidget", "Time"),
                "time",
                enabled_when="weekendTimeEnabled",
            ),
        )),
        Group("preparation", QT_TRANSLATE_NOOP("SettingsWidget", "Preparation"), "general", (
            Setting("meetingsAutoDownload", QT_TRANSLATE_NOOP("SettingsWidget", "Auto-download weekly study"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Downloads this week’s and next week’s meeting media.")),
            Setting("backgroundSongEnabled", QT_TRANSLATE_NOOP("SettingsWidget", "Automatic background song"), description_key="backgroundSongDescription"),
        )),
    )),
    Section("projection", QT_TRANSLATE_NOOP("SettingsWidget", "Projection"),
            QT_TRANSLATE_NOOP("SettingsWidget", "Screens and annual text"), "projection", (
        Group("screens", QT_TRANSLATE_NOOP("SettingsWidget", "Screens"), "general", (
            Setting("screens", QT_TRANSLATE_NOOP("SettingsWidget", "Screens"), "screens"),
        )),
        Group("yeartext", QT_TRANSLATE_NOOP("SettingsWidget", "Annual Text"), "general", (
            Setting("yeartextStatusText", QT_TRANSLATE_NOOP("SettingsWidget", "Update"), "action", description=QT_TRANSLATE_NOOP("SettingsWidget", "Text shown on the projection screen when idle."), action="refreshYeartext"),
            Setting("yeartextQuote", QT_TRANSLATE_NOOP("SettingsWidget", "Scripture:"), "multiline"),
            Setting("yeartextReference", QT_TRANSLATE_NOOP("SettingsWidget", "Bible reference:"), "input"),
            Setting("yeartextDirty", QT_TRANSLATE_NOOP("SettingsWidget", "Save changes"), "action", action="saveYeartext", secondary_action="cancelYeartext", secondary_label=QT_TRANSLATE_NOOP("SettingsWidget", "Cancel"), enabled_when="yeartextDirty"),
        )),
    )),
    Section("integrations", QT_TRANSLATE_NOOP("SettingsWidget", "Integrations"),
            QT_TRANSLATE_NOOP("SettingsWidget", "OBS, Zoom and camera"), "integrations", (
        Group("obs", QT_TRANSLATE_NOOP("SettingsWidget", "OBS Studio"), "integrations", (
            Setting("obsEnabled", QT_TRANSLATE_NOOP("SettingsWidget", "OBS Studio"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Automatically switches scenes during projection")),
            Setting("obsPort", QT_TRANSLATE_NOOP("SettingsWidget", "WebSocket Port"), "input", enabled_when="obsEnabled"),
            Setting("obsPassword", QT_TRANSLATE_NOOP("SettingsWidget", "Password (optional)"), "password", enabled_when="obsEnabled"),
            Setting("obsDefaultScene", QT_TRANSLATE_NOOP("SettingsWidget", "Default scene (idle)"), "select", options="obsScenes", enabled_when="obsConnected"),
            Setting("obsMediaScene", QT_TRANSLATE_NOOP("SettingsWidget", "Media window scene"), "select", options="obsScenes", enabled_when="obsMediaSceneEnabled"),
        ), True, "obsStatus", children=(
            Group("ndi", QT_TRANSLATE_NOOP("SettingsWidget", "Program stream (NDI)"), "integrations", (
                Setting("ndiEnabled", QT_TRANSLATE_NOOP("SettingsWidget", "Program stream (NDI)"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Receive the DistroAV/NDI output from OBS as a live projection.")),
                Setting("ndiSource", QT_TRANSLATE_NOOP("SettingsWidget", "Available NDI sources"), "select", options="ndiSources", enabled_when="ndiEnabled"),
                Setting("ndiStatus", QT_TRANSLATE_NOOP("SettingsWidget", "Find sources"), "action", action="findNdiSources", enabled_when="ndiEnabled"),
            ), True, "ndiStatus"),
        ), feedback_key="obsConnectionFeedback", error_key="obsConnectionError"),
        Group("zoom", QT_TRANSLATE_NOOP("SettingsWidget", "Zoom Meetings"), "integrations", (
            Setting("zoomEnabled", QT_TRANSLATE_NOOP("SettingsWidget", "Zoom Meetings"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Audio controls and attendance count during meetings.")),
        ), True, visible_when="zoomAvailable"),
        Group("camera", QT_TRANSLATE_NOOP("SettingsWidget", "Camera"), "integrations", (
            Setting("cameraEnabled", QT_TRANSLATE_NOOP("SettingsWidget", "Camera"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Shows a camera button in the live tools toolbar.")),
        ), visible_when="cameraAvailable"),
    )),
    Section("automations", QT_TRANSLATE_NOOP("SettingsWidget", "Automations"),
            QT_TRANSLATE_NOOP("SettingsWidget", "Sharing and shortcuts"), "automations", (
        Group("share", QT_TRANSLATE_NOOP("SettingsWidget", "Auto Screen Share"), "integrations", (
            Setting("autoShareEnabled", QT_TRANSLATE_NOOP("SettingsWidget", "Auto Screen Share"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Automatically shares screen via hotkeys when projecting media.")),
            Setting("shareHotkey", QT_TRANSLATE_NOOP("SettingsWidget", "Share hotkey"), "action", action="editShareHotkey"),
            Setting("shareTargetStatus", QT_TRANSLATE_NOOP("SettingsWidget", "Share target"), "action", action="configureShareTarget"),
            Setting("accessibilityStatus", QT_TRANSLATE_NOOP("SettingsWidget", "Accessibility permission"), "action", action="openAccessibility", visible_when="accessibilityRequired"),
        ), True, "shareTargetStatus"),
        Group("shortcuts", QT_TRANSLATE_NOOP("SettingsWidget", "Automatic Shortcuts"), "integrations", (
            Setting("autoKeysEnabled", QT_TRANSLATE_NOOP("SettingsWidget", "Automatic Shortcuts"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Sends keyboard shortcuts when visual media changes state.")),
            Setting("shortcuts", QT_TRANSLATE_NOOP("SettingsWidget", "Add shortcut"), "shortcuts", action="addShortcut"),
        ), True),
    )),
    Section("remote", QT_TRANSLATE_NOOP("SettingsWidget", "Remote access"),
            QT_TRANSLATE_NOOP("SettingsWidget", "Control over your local network"), "remote", (
        Group("remote", QT_TRANSLATE_NOOP("SettingsWidget", "Remote control"), "remote", (
            Setting("enabled", QT_TRANSLATE_NOOP("SettingsWidget", "Remote control"), description=QT_TRANSLATE_NOOP("SettingsWidget", "Control Solin securely from another device on this local network.")),
            Setting("network", QT_TRANSLATE_NOOP("SettingsWidget", "Network interface"), "select", options="networkOptions"),
            Setting("endpoint", QT_TRANSLATE_NOOP("SettingsWidget", "Copy address"), "action", action="copyAddress", enabled_when="endpoint"),
        ), status_key="statusMessage"),
        Group("credentials", QT_TRANSLATE_NOOP("SettingsWidget", "Access credentials"), "remote", (
            Setting("username", QT_TRANSLATE_NOOP("SettingsWidget", "Username"), "input"),
            Setting("password", QT_TRANSLATE_NOOP("SettingsWidget", "New password"), "password", description_key="passwordHint"),
            Setting("passwordConfirmation", QT_TRANSLATE_NOOP("SettingsWidget", "Confirm password"), "password"),
            Setting("saveCredentials", QT_TRANSLATE_NOOP("SettingsWidget", "Save credentials"), "action", action="saveCredentials", secondary_action="cancelCredentials", secondary_label=QT_TRANSLATE_NOOP("SettingsWidget", "Cancel")),
        ), collapsible=True, status_key="credentialsSummary"),
        Group("devices", QT_TRANSLATE_NOOP("SettingsWidget", "Devices"), "remote", (
            Setting("setupEnabled", QT_TRANSLATE_NOOP("SettingsWidget", "Set up a device"), "action", action="openSetup", enabled_when="setupEnabled"),
        )),
    )),
    Section("about", QT_TRANSLATE_NOOP("SettingsWidget", "About"),
            QT_TRANSLATE_NOOP("SettingsWidget", "Version and information"), "about", ()),
)


def translated_catalogue(capabilities: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    def tr(source: str) -> str:
        return QCoreApplication.translate("SettingsWidget", source) if source else ""

    def translate_group(group: Group) -> dict[str, Any] | None:
        state = capabilities[group.domain]
        if group.visible_when and not state.get(group.visible_when):
            return None
        rows = [{
            "key": row.key, "label": tr(row.label), "kind": row.kind,
            "description": tr(row.description), "options": row.options,
            "action": row.action, "secondaryAction": row.secondary_action,
            "secondaryLabel": tr(row.secondary_label),
            "visibleWhen": row.visible_when, "enabledWhen": row.enabled_when,
            "descriptionKey": row.description_key,
        } for row in group.rows]
        children = [translated for child in group.children
                    if (translated := translate_group(child)) is not None]
        return {"id": group.key, "title": tr(group.title), "domain": group.domain,
                "rows": rows, "children": children, "collapsible": group.collapsible,
                "statusKey": group.status_key, "feedbackKey": group.feedback_key,
                "errorKey": group.error_key}

    result = []
    for section in SECTIONS:
        groups = [translated for group in section.groups
                  if (translated := translate_group(group)) is not None]
        result.append({"id": section.key, "title": tr(section.title),
                       "description": tr(section.description), "icon": section.icon,
                       "groups": groups})
    return result
