from __future__ import annotations

from dataclasses import dataclass

from ..core.ingest.watched_folder_settings import WatchedFolderSettingsStore
from ..core.integrations.automation.settings import (
    AutoKeySettingsStore,
    AutoShareSettingsStore,
    CameraSettingsStore,
    OBSSettingsStore,
    ZoomSettingsStore,
)
from ..core.jw.background_song_settings import BackgroundSongSettingsStore
from ..core.jw.yeartext_settings import YeartextSettingsStore
from ..core.media.settings import MediaSettingsStore, ProjectionPlaybackSettingsStore
from ..core.meetings.schedule_settings import MeetingScheduleSettingsStore
from ..core.network.browser_settings import BrowserSettingsStore
from ..core.projection.monitor_allocation import MonitorAllocationStore
from ..core.remote.notification_settings import NotificationSettingsStore
from ..core.remote_control.security import RemoteControlCredentialsStore
from ..core.remote_control.settings import RemoteControlSettingsStore
from ..core.foundation.settings_store import ProfileAppSettingsStore
from ..core.windowing.settings import WindowGeometrySettingsStore


@dataclass(frozen=True, slots=True)
class MainWindowProfileSettings:
    app: ProfileAppSettingsStore
    media: MediaSettingsStore
    browser: BrowserSettingsStore
    obs: OBSSettingsStore
    zoom: ZoomSettingsStore
    auto_share: AutoShareSettingsStore
    auto_key: AutoKeySettingsStore
    camera: CameraSettingsStore
    projection_playback: ProjectionPlaybackSettingsStore
    meeting_schedule: MeetingScheduleSettingsStore
    watched_folder: WatchedFolderSettingsStore
    yeartext: YeartextSettingsStore
    background_song: BackgroundSongSettingsStore
    monitor_allocation: MonitorAllocationStore
    window_geometry: WindowGeometrySettingsStore
    notification: NotificationSettingsStore
    remote_control: RemoteControlSettingsStore
    remote_control_credentials: RemoteControlCredentialsStore
