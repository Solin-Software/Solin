import pytest

from PySide6.QtCore import QDateTime

from solin.controllers.timer_projection_controller import (
    TimerProjectionContext,
    TimerProjectionController,
    TimerProjectionHandlers,
)
from solin.core.projection.application import ProjectionSession
from solin.core.timer.models import MediaCountdownPresentation


class _NavigationStub:
    def __init__(self):
        self.stopped = 0

    def stop_browser_tab_projection(self):
        self.stopped += 1


class _ServiceStub:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class _ProjectionBarStub:
    def __init__(self):
        self.playlists = []
        self.timers = []

    def set_playlist(self, playlist):
        self.playlists.append(playlist)

    def activate_timer(self, target_dt, presentation):
        self.timers.append((target_dt, presentation))

class _ProjectionIntegrationsStub:
    def __init__(self):
        self.statuses = []

    def update_status(self, *args, **kwargs):
        self.statuses.append((args, kwargs))


class _ProjectionWindowStub:
    def __init__(self):
        self.timers = []
        self.timer_updates = []
        self.blinks = []

    def show_timer(self, remaining, total, presentation):
        self.timers.append((remaining, total, presentation))

    def update_timer(self, remaining, total):
        self.timer_updates.append((remaining, total))

    def set_timer_blink(self, on):
        self.blinks.append(on)

class _ProtectionStub:
    def __init__(self):
        self.locked = False
        self.automatic_projection_states = []

    def allow_manual_projection_change(self, *, notify=True):
        return not self.locked

    def allow_automatic_projection_change(self, *, projection_active):
        self.automatic_projection_states.append(projection_active)
        return not self.locked


class _WindowStub:
    def __init__(self):
        self.projection_session = ProjectionSession()
        self.projection_session.set_tab_projection_active(True)
        self._navigation = _NavigationStub()
        self.media_ctrl = _ServiceStub()
        self._ndi_service = _ServiceStub()
        self.proj_bar = _ProjectionBarStub()
        self._projection_integrations = _ProjectionIntegrationsStub()
        self.projection_session.set_state({"type": "video"})
        self.windows = [_ProjectionWindowStub(), _ProjectionWindowStub()]
        self.playback_protection = _ProtectionStub()
        self.timer_blinks = []
        self.program_content = type(
            "ProgramContentStub",
            (),
            {"set_timer_blink": lambda _self, value: self.timer_blinks.append(value)},
        )()

    def _all_windows(self):
        return self.windows

    def tr(self, text):
        return text


def _controller(window):
    return TimerProjectionController(
        TimerProjectionContext(
            projection_session=window.projection_session,
            projection_bar=window.proj_bar,
            media_controller=window.media_ctrl,
            ndi_service=window._ndi_service,
            projection_windows=window._all_windows,
            playback_protection=window.playback_protection,
            program_content=window.program_content,
        ),
        TimerProjectionHandlers(
            stop_browser_tab_projection=(
                window._navigation.stop_browser_tab_projection
            ),
            update_projection_status=(
                window._projection_integrations.update_status
            ),
        ),
    )


def test_start_timer_stops_active_sources_and_broadcasts_timer():
    window = _WindowStub()
    controller = _controller(window)
    controller._remaining_seconds = lambda _target_dt: 42
    target_dt = QDateTime.currentDateTime().addSecs(60)

    controller.start_timer(target_dt, MediaCountdownPresentation.YEARLY_TEXT.value)

    assert window.projection_session.tab_projection_active is False
    assert window._navigation.stopped == 1
    assert window.media_ctrl.stopped == 1
    assert window._ndi_service.stopped == 1
    assert window.proj_bar.playlists == [[]]
    assert window.proj_bar.timers == [
        (target_dt, MediaCountdownPresentation.YEARLY_TEXT)
    ]
    assert [projection_window.timers for projection_window in window.windows] == [
        [(42, 42, MediaCountdownPresentation.YEARLY_TEXT)],
        [(42, 42, MediaCountdownPresentation.YEARLY_TEXT)],
    ]
    assert window.projection_session.state == {
        "type": "timer",
        "title": "Timer",
        "target_dt": target_dt,
        "total": 42,
        "presentation": MediaCountdownPresentation.YEARLY_TEXT.value,
        "origin": "manual",
    }
    args, kwargs = window._projection_integrations.statuses[0]
    assert args[0] is True
    assert args[1].startswith("Timer → ")
    assert kwargs == {"auto_keys_media": False}


def test_timer_projection_is_rejected_without_side_effects_when_locked():
    window = _WindowStub()
    window.playback_protection.locked = True
    controller = _controller(window)
    target_dt = QDateTime.currentDateTime().addSecs(60)

    controller.start_timer(target_dt, MediaCountdownPresentation.YEARLY_TEXT.value)

    assert window.projection_session.tab_projection_active is True
    assert window.projection_session.state == {"type": "video"}
    assert window._navigation.stopped == 0
    assert window.media_ctrl.stopped == 0
    assert window.proj_bar.timers == []


def test_automatic_timer_revalidates_policy_and_records_occurrence_origin():
    window = _WindowStub()
    window.projection_session.set_tab_projection_active(False)
    window.projection_session.reset_state()
    controller = _controller(window)
    controller._remaining_seconds = lambda _target_dt: 300
    target_dt = QDateTime.currentDateTime().addSecs(300)

    started = controller.start_automatic_timer(
        target_dt,
        MediaCountdownPresentation.CIRCULAR.value,
        "midweek:2026-06-08:19:30",
    )

    assert started is True
    assert window.playback_protection.automatic_projection_states == [False]
    assert window.projection_session.state == {
        "type": "timer",
        "title": "Timer",
        "target_dt": target_dt,
        "total": 300,
        "presentation": MediaCountdownPresentation.CIRCULAR.value,
        "origin": "automatic_countdown",
        "occurrence_id": "midweek:2026-06-08:19:30",
    }


def test_automatic_timer_never_replaces_an_active_operator_projection():
    window = _WindowStub()
    controller = _controller(window)
    target_dt = QDateTime.currentDateTime().addSecs(60)

    started = controller.start_automatic_timer(
        target_dt,
        MediaCountdownPresentation.CIRCULAR.value,
        "midweek:2026-06-08:19:30",
    )

    assert started is False
    assert window.playback_protection.automatic_projection_states == []
    assert window.projection_session.state == {"type": "video"}
    assert window.projection_session.tab_projection_active is True
    assert window.media_ctrl.stopped == 0
    assert window.proj_bar.timers == []


def test_automatic_timer_has_no_side_effects_when_policy_changes_before_start():
    window = _WindowStub()
    window.projection_session.set_tab_projection_active(False)
    window.projection_session.reset_state()
    window.playback_protection.locked = True
    controller = _controller(window)
    target_dt = QDateTime.currentDateTime().addSecs(60)

    started = controller.start_automatic_timer(
        target_dt,
        MediaCountdownPresentation.CIRCULAR.value,
        "midweek:2026-06-08:19:30",
    )

    assert started is False
    assert window.playback_protection.automatic_projection_states == [False]
    assert window.projection_session.state == {"type": "idle"}
    assert window.media_ctrl.stopped == 0
    assert window.proj_bar.timers == []


def test_automatic_timer_rejects_a_target_that_has_already_been_reached():
    window = _WindowStub()
    controller = _controller(window)
    target_dt = QDateTime.currentDateTime().addSecs(-1)

    started = controller.start_automatic_timer(
        target_dt,
        MediaCountdownPresentation.CIRCULAR.value,
        "midweek:2026-06-08:19:30",
    )

    assert started is False
    assert window.playback_protection.automatic_projection_states == []
    assert window.projection_session.state == {"type": "video"}
    assert window.proj_bar.timers == []


@pytest.mark.parametrize("program_content_enabled", [True, False])
def test_timer_update_and_blink_are_broadcast_to_all_projection_windows(program_content_enabled):
    window = _WindowStub()
    if not program_content_enabled:
        window.program_content = None
    controller = _controller(window)
    window.projection_session.set_state({"type": "timer", "title": "Timer"})

    controller.on_timer_update_proj(7, 30)
    controller.on_timer_blink_proj(True)

    assert [projection_window.timer_updates for projection_window in window.windows] == [
        [(7, 30)],
        [(7, 30)],
    ]
    assert [projection_window.blinks for projection_window in window.windows] == [
        [True],
        [True],
    ]
    assert window.timer_blinks == ([True] if program_content_enabled else [])
    assert window.projection_session.state == {
        "type": "timer",
        "title": "Timer",
        "remaining": 7,
        "total": 30,
    }


def test_timer_projection_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
