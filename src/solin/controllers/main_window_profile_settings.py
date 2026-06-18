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
from ..core.projection.monitor_allocation import MonitorAllocationStore
from ..core.remote.notification_settings import NotificationSettingsStore
from ..core.windowing.settings import WindowGeometrySettingsStore


@dataclass(frozen=True, slots=True)
class MainWindowProfileSettings:
    media: MediaSettingsStore
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
