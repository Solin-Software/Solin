"""Coordinate optional meeting triggers without coupling playback to automation."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal

from solin.core.integrations.automation.settings import ZoomSettingsStore
from solin.core.integrations.automation.zoom.service import ZoomService
from solin.core.integrations.automation.zoom.state import SequenceResult
from solin.core.jw.background_song_service import BackgroundSongService

log = logging.getLogger(__name__)


class ZoomMeetingController(QObject):
    link_changed = Signal(bool, bool)

    def __init__(
        self,
        zoom: ZoomService,
        song: BackgroundSongService,
        settings: ZoomSettingsStore,
        notifications,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._zoom = zoom
        self._song = song
        self._settings = settings
        self._notifications = notifications
        self._post_song_requested = False
        song.scheduled_stop_started.connect(self._on_scheduled_stop)
        song.availability_changed.connect(self.refresh_link)
        zoom.operation_finished.connect(self._on_finished)
        zoom.error.connect(self._on_error)

    @property
    def link_available(self) -> bool:
        return self._song.is_enabled

    @property
    def linked(self) -> bool:
        return self.link_available and self._settings.background_song_linked()

    def refresh_link(self, *_args) -> None:
        self.link_changed.emit(self.link_available, self.linked)

    def set_linked(self, linked: bool) -> None:
        if self.link_available:
            self._settings.set_background_song_linked(linked)
        self.refresh_link()

    def request_pre_meeting(self) -> bool:
        if not self._zoom.request_pre_meeting():
            return False
        if self.linked:
            self._song.stop(manual=True)
        return True

    def request_post_meeting(self) -> bool:
        if not self._zoom.request_post_meeting():
            return False
        self._post_song_requested = self.linked
        return True

    def _on_scheduled_stop(self, _occurrence_id: str) -> None:
        if self.linked:
            # A missed trigger is not queued for an unrelated future meeting.
            # Preserve the scheduled fade instead of requesting another song stop.
            self._zoom.request_pre_meeting()

    def _on_finished(self, result: SequenceResult) -> None:
        start_song = result.sequence == "post_meeting" and self._post_song_requested
        self._post_song_requested = False
        if result.cancelled:
            return
        if start_song and result.ok and self.linked:
            if not self._song.is_playing and not self._song.playback_requested:
                self._song.start_manual()
        if not result.ok:
            log.warning("Incomplete Zoom meeting workflow: %s", result)
            self._on_error("partial_failure")

    def _on_error(self, _code: str) -> None:
        self._notifications.warning(
            self.tr("Some Zoom actions could not be completed. Check Zoom and try again."),
            dedupe_key="zoom-automation",
        )
