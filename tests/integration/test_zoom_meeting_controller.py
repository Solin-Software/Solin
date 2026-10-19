from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from solin.controllers.zoom_meeting_controller import ZoomMeetingController
from solin.core.integrations.automation.zoom.state import SequenceResult, StepFailure
from solin.widgets.zoom_panel import ZoomPanel


class Zoom(QObject):
    operation_finished = Signal(object)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.accept = True
        self.pre_requests = 0
        self.post_requests = 0

    def request_pre_meeting(self):
        self.pre_requests += 1
        return self.accept

    def request_post_meeting(self):
        self.post_requests += 1
        return self.accept


class Song(QObject):
    scheduled_stop_started = Signal(str)
    availability_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.is_enabled = True
        self.is_configured = True
        self.is_playing = False
        self.playback_requested = False
        self.starts = 0
        self.stops = []

    def start_manual(self):
        self.starts += 1

    def stop(self, *, manual=False):
        self.stops.append(manual)
        self.playback_requested = False


@pytest.fixture
def integration():
    zoom, song = Zoom(), Song()
    settings = SimpleNamespace(linked=True)
    settings.background_song_linked = lambda: settings.linked
    settings.set_background_song_linked = lambda value: setattr(settings, "linked", value)
    warnings = []
    notifications = SimpleNamespace(warning=lambda *args, **kwargs: warnings.append(args))
    controller = ZoomMeetingController(zoom, song, settings, notifications)
    yield SimpleNamespace(
        controller=controller, zoom=zoom, song=song, settings=settings, warnings=warnings
    )
    controller.deleteLater()
    zoom.deleteLater()
    song.deleteLater()


@pytest.mark.parametrize(
    ("enabled", "configured", "saved", "available", "linked"),
    [
        (True, True, True, True, True),
        (False, True, True, False, False),
        (True, False, True, True, True),
        (True, True, False, True, False),
    ],
)
def test_link_visibility_and_triggers_follow_current_configuration(
    integration,
    enabled,
    configured,
    saved,
    available,
    linked,
):
    ctx = integration
    ctx.song.is_enabled, ctx.song.is_configured, ctx.settings.linked = enabled, configured, saved
    states = []
    ctx.controller.link_changed.connect(lambda *args: states.append(args))
    ctx.controller.refresh_link()
    assert states == [(available, linked)]
    ctx.song.scheduled_stop_started.emit("meeting-1")
    assert ctx.zoom.pre_requests == int(linked)
    assert ctx.song.stops == []


@pytest.mark.parametrize(
    "enabled,linked,configured,accepted,should_stop",
    [
        (True, True, True, True, True),
        (True, True, False, True, True),
        (True, False, True, True, False),
        (False, True, True, True, False),
        (True, True, True, False, False),
    ],
)
def test_manual_meeting_start_stops_song_only_for_an_accepted_linked_request(
    integration, enabled, linked, configured, accepted, should_stop,
):
    ctx = integration
    ctx.song.is_enabled = enabled
    ctx.song.is_configured = configured
    ctx.settings.linked = linked
    ctx.zoom.accept = accepted
    ctx.song.playback_requested = True

    assert ctx.controller.request_pre_meeting() is accepted

    assert ctx.zoom.pre_requests == 1
    assert ctx.song.stops == ([True] if should_stop else [])
    assert ctx.song.is_enabled is enabled
    assert ctx.song.playback_requested is not should_stop


def test_song_can_be_started_again_before_or_after_a_manual_meeting_start(integration):
    ctx = integration
    assert ctx.controller.request_pre_meeting()
    ctx.zoom.operation_finished.emit(SequenceResult("pre_meeting"))
    assert ctx.song.stops == [True]
    assert ctx.song.starts == 0

    assert ctx.controller.request_post_meeting()
    ctx.zoom.operation_finished.emit(SequenceResult("post_meeting"))
    assert ctx.song.starts == 1


def test_post_meeting_starts_song_only_after_successful_accepted_operation(integration):
    ctx = integration
    assert ctx.controller.request_post_meeting()
    assert ctx.song.starts == 0
    ctx.zoom.operation_finished.emit(SequenceResult("post_meeting"))
    assert ctx.song.starts == 1
    ctx.zoom.operation_finished.emit(SequenceResult("post_meeting"))
    assert ctx.song.starts == 1


def test_manual_song_link_does_not_require_scheduled_days(integration):
    ctx = integration
    ctx.song.is_configured = False
    ctx.controller.set_linked(True)
    assert ctx.controller.link_available and ctx.controller.linked
    assert ctx.controller.request_post_meeting()
    ctx.zoom.operation_finished.emit(SequenceResult("post_meeting"))
    assert ctx.song.starts == 1
    assert ctx.zoom.pre_requests == 0


@pytest.mark.parametrize(
    "result",
    [
        SequenceResult("post_meeting", cancelled=True),
        SequenceResult("post_meeting", (StepFailure("leave_audio", "worker_timeout"),)),
    ],
)
def test_uncertain_or_cancelled_post_meeting_does_not_start_playback(integration, result):
    ctx = integration
    assert ctx.controller.request_post_meeting()
    ctx.zoom.operation_finished.emit(result)
    assert ctx.song.starts == 0
    assert len(ctx.warnings) == int(not result.cancelled)


def test_rejected_post_meeting_cannot_arm_a_future_song_start(integration):
    ctx = integration
    ctx.zoom.accept = False
    assert not ctx.controller.request_post_meeting()
    ctx.zoom.operation_finished.emit(SequenceResult("post_meeting"))
    assert ctx.song.starts == 0


@pytest.mark.parametrize("playing,requested", [(True, False), (False, True)])
def test_post_meeting_does_not_restart_playing_or_loading_song(integration, playing, requested):
    ctx = integration
    ctx.song.is_playing, ctx.song.playback_requested = playing, requested
    ctx.controller.request_post_meeting()
    ctx.zoom.operation_finished.emit(SequenceResult("post_meeting"))
    assert ctx.song.starts == 0


def test_disabling_link_during_operation_prevents_song_start(integration):
    ctx = integration
    ctx.controller.request_post_meeting()
    ctx.controller.set_linked(False)
    ctx.zoom.operation_finished.emit(SequenceResult("post_meeting"))
    assert ctx.song.starts == 0


def test_popup_disables_actions_without_meeting_and_during_operation():
    panel = ZoomPanel()
    try:
        assert not panel._pre.isEnabled()
        assert not panel._post.isEnabled()
        panel.set_connected(True)
        assert panel._pre.isEnabled() and panel._post.isEnabled()
        panel.set_busy(True)
        assert not panel._pre.isEnabled() and not panel._post.isEnabled()
        assert not panel._cancel.isHidden()
        panel.set_busy(False)
        panel.set_background_song_link(True, True)
        assert not panel._integration.isHidden() and panel._link.isChecked()
        panel.set_background_song_link(False, False)
        assert panel._integration.isHidden()
    finally:
        panel.deleteLater()
