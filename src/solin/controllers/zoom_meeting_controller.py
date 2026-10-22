"""Coordinate optional meeting triggers without coupling playback to automation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from solin.core.integrations.automation.settings import ZoomSettingsStore
from solin.core.integrations.automation.auto_key_actions import (
    EVENT_ZOOM_BEFORE_MEETING,
    EVENT_ZOOM_MEETING_STARTED,
    EVENT_ZOOM_AFTER_MEETING,
)
from solin.core.conference.provider import MeetingProvider, SequenceResult
from solin.core.jw.background_song_service import BackgroundSongService

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _MeetingRequest:
    event: str
    sequence: str
    start_song: bool


class ZoomMeetingController(QObject):
    link_changed = Signal(bool, bool)
    event_triggered = Signal(str)

    def __init__(
        self,
        zoom: MeetingProvider,
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
        self._pending: _MeetingRequest | None = None
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

    def request_before_meeting(self) -> bool:
        return self._request(EVENT_ZOOM_BEFORE_MEETING)

    def request_meeting_start(self) -> bool:
        return self._request(EVENT_ZOOM_MEETING_STARTED)

    def request_after_meeting(self) -> bool:
        return self._request(EVENT_ZOOM_AFTER_MEETING)

    def _request(self, event: str, *, scheduled: bool = False) -> bool:
        if self._pending is not None:
            return False
        starting = event == EVENT_ZOOM_MEETING_STARTED
        request = _MeetingRequest(
            event, "pre_meeting" if starting else "post_meeting", self.linked and not starting
        )
        # Arm the intent first: the service can report a worker failure synchronously.
        self._pending = request
        accepted = (
            self._zoom.request_pre_meeting() if starting else self._zoom.request_post_meeting()
        )
        if not accepted:
            if self._pending is request:
                self._pending = None
            return False
        if self._pending is request and starting and not scheduled and self.linked:
            self._song.stop(manual=True)
        return True

    def _on_scheduled_stop(self, _occurrence_id: str) -> None:
        if self.linked:
            # A missed trigger is not queued for an unrelated future meeting.
            # Preserve the scheduled fade instead of requesting another song stop.
            self._request(EVENT_ZOOM_MEETING_STARTED, scheduled=True)

    def _on_finished(self, result: SequenceResult) -> None:
        request = self._pending
        if request is not None and request.sequence == result.sequence:
            self._pending = None
        else:
            request = None
        if result.cancelled:
            return
        if not result.ok:
            log.warning("Incomplete Zoom meeting workflow: %s", result)
            self._on_error("partial_failure")
            return
        if request is not None:
            self.event_triggered.emit(request.event)
            if request.start_song and self.linked:
                if not self._song.is_playing and not self._song.playback_requested:
                    self._song.start_manual()

    def _on_error(self, _code: str) -> None:
        self._notifications.warning(
            self.tr("Some Zoom actions could not be completed. Check Zoom and try again."),
            dedupe_key="zoom-automation",
        )
