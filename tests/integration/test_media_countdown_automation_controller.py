from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QDateTime

from solin.controllers.media_countdown_automation_controller import (
    MediaCountdownAutomationController,
)
from solin.core.meetings.schedule import (
    MIDWEEK,
    WEEKEND,
    MeetingSchedule,
    MeetingSlot,
)
from solin.core.projection.application import ProjectionSession
from solin.core.timer.media_countdown_automation import (
    MediaCountdownAutomationConfig,
    MediaCountdownBlockingReason,
    MediaCountdownAutomationStatus,
)
from solin.core.timer.media_countdown_settings import MediaCountdownSettings
from solin.core.timer.models import MediaCountdownPresentation


class _Settings:
    def __init__(self, settings: MediaCountdownSettings) -> None:
        self.settings = settings

    def load(self) -> MediaCountdownSettings:
        return self.settings

    def set_enabled(self, enabled: bool) -> None:
        self.settings = replace(
            self.settings,
            automation=replace(self.settings.automation, enabled=bool(enabled)),
        )

    def set_lead_seconds(self, seconds: int) -> None:
        self.settings = replace(
            self.settings,
            automation=replace(self.settings.automation, lead_seconds=int(seconds)),
        )

    def set_presentation(self, presentation) -> None:
        self.settings = replace(
            self.settings,
            presentation=MediaCountdownPresentation(presentation),
        )

class _ScheduleSource:
    def __init__(self, schedule: MeetingSchedule) -> None:
        self.schedule = schedule

    def load(self) -> MeetingSchedule:
        return self.schedule


class _Protection:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.projection_states: list[bool] = []

    def allow_automatic_projection_change(self, *, projection_active: bool) -> bool:
        self.projection_states.append(projection_active)
        return self.allowed


class _Notifications:
    def __init__(self) -> None:
        self.information_messages: list[tuple[str, str]] = []
        self.warning_messages: list[tuple[str, str]] = []

    def information(self, message: str, *, dedupe_key: str) -> None:
        self.information_messages.append((message, dedupe_key))

    def warning(self, message: str, *, dedupe_key: str) -> None:
        self.warning_messages.append((message, dedupe_key))


def _now(hour: int, minute: int = 0, second: int = 0) -> datetime:
    tz = timezone(timedelta(hours=-3))
    return datetime(2026, 6, 8, hour, minute, second, tzinfo=tz)


def _schedule(*, meeting_hour: int = 19, meeting_minute: int = 30) -> MeetingSchedule:
    monday = _now(0).weekday()
    return MeetingSchedule(
        midweek=MeetingSlot(
            MIDWEEK,
            monday,
            meeting_hour * 60 + meeting_minute,
        ),
        weekend=MeetingSlot(WEEKEND),
    )


def _controller(
    *,
    now: list[datetime],
    enabled: bool = True,
    lead_seconds: int = 600,
    schedule: MeetingSchedule | None = None,
    target_available: bool = True,
    allowed: bool = True,
):
    settings = _Settings(
        MediaCountdownSettings(
            presentation=MediaCountdownPresentation.CIRCULAR,
            automation=MediaCountdownAutomationConfig(
                enabled=enabled,
                lead_seconds=lead_seconds,
            ),
        )
    )
    source = _ScheduleSource(schedule or _schedule())
    session = ProjectionSession()
    if target_available:
        session.projection_windows.append(object())
    protection = _Protection(allowed)
    notifications = _Notifications()
    controller = MediaCountdownAutomationController(
        settings=settings,
        schedule_source=source,
        projection_session=session,
        playback_protection=protection,
        notifications=notifications,
        now_provider=lambda: now[0],
    )
    return controller, settings, source, session, protection, notifications


def test_countdown_is_requested_inside_the_window_but_never_at_meeting_time() -> None:
    clock = [_now(19, 20)]
    controller, *_ = _controller(now=clock)
    requests: list[tuple[QDateTime, str, str]] = []
    controller.countdown_requested.connect(
        lambda target, presentation, occurrence_id: requests.append(
            (target, presentation, occurrence_id)
        )
    )

    controller.evaluate()

    assert len(requests) == 1
    target, presentation, occurrence_id = requests[0]
    assert target.toSecsSinceEpoch() == int(_now(19, 30).timestamp())
    assert presentation == MediaCountdownPresentation.CIRCULAR.value
    assert occurrence_id == "midweek:2026-06-08:19:30"

    clock[0] = _now(19, 30)
    controller.evaluate()

    assert len(requests) == 1
    assert controller.snapshot().status is MediaCountdownAutomationStatus.MISSED


def test_startup_inside_window_starts_immediately_and_acceptance_becomes_active() -> None:
    clock = [_now(19, 27)]
    controller, _, _, session, _, notifications = _controller(now=clock)

    def accept(target: QDateTime, presentation: str, occurrence_id: str) -> None:
        session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })

    controller.countdown_requested.connect(accept)

    controller.evaluate()

    assert controller.snapshot().status is MediaCountdownAutomationStatus.ACTIVE
    assert controller.snapshot().active_automatic is True
    assert controller.snapshot().active_countdown is True
    assert controller.snapshot().active_origin == "automatic"
    assert controller.snapshot().active_target is not None
    assert session.state["occurrence_id"] == "midweek:2026-06-08:19:30"
    assert len(notifications.information_messages) == 1

    session.update_state(remaining=299)
    session.update_state(remaining=298)

    assert len(notifications.information_messages) == 1

    clock[0] = _now(19, 31)
    controller.evaluate()
    session.update_state(remaining=0)

    assert controller.snapshot().next_occurrence is not None
    assert controller.snapshot().next_occurrence.starts_at == _now(19, 30)


def test_changing_shared_presentation_does_not_replace_active_countdown() -> None:
    clock = [_now(19, 27)]
    controller, settings, _, session, _, _ = _controller(now=clock)

    def accept(target: QDateTime, presentation: str, occurrence_id: str) -> None:
        session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })

    controller.countdown_requested.connect(accept)
    controller.evaluate()

    controller.set_presentation(MediaCountdownPresentation.YEARLY_TEXT)

    assert session.state["presentation"] == MediaCountdownPresentation.CIRCULAR.value
    assert settings.settings.presentation is MediaCountdownPresentation.YEARLY_TEXT
    assert controller.snapshot().status is MediaCountdownAutomationStatus.ACTIVE


def test_active_projection_waits_regardless_of_playback_protection_setting() -> None:
    clock = [_now(19, 25)]
    controller, _, _, session, protection, notifications = _controller(
        now=clock,
        allowed=False,
    )
    session.set_state({"type": "image"})
    requests: list[QDateTime] = []
    controller.countdown_requested.connect(
        lambda target, _presentation, _occurrence: requests.append(target)
    )

    controller.evaluate()

    assert requests == []
    assert protection.projection_states == []
    assert controller.snapshot().status is (
        MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
    )

    clock[0] = _now(19, 29, 59)
    protection.allowed = True
    controller.evaluate()

    assert requests == []

    clock[0] = _now(19, 30)
    protection.allowed = False
    controller.evaluate()

    assert requests == []
    assert len(notifications.warning_messages) == 1
    assert notifications.warning_messages[0][0] == (
        "The countdown did not start because the media window remained in use."
    )


def test_countdown_starts_when_preexisting_operator_projection_closes() -> None:
    clock = [_now(19, 25)]
    controller, _, _, session, protection, _ = _controller(now=clock)
    session.set_state({"type": "image"})
    requests: list[str] = []

    def accept(target: QDateTime, presentation: str, occurrence_id: str) -> None:
        requests.append(occurrence_id)
        session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })

    controller.countdown_requested.connect(accept)
    controller.start()

    assert requests == []
    assert session.state_type == "image"
    assert protection.projection_states == []

    session.reset_state()

    assert len(requests) == 1
    assert session.state_type == "timer"
    assert controller.snapshot().status is MediaCountdownAutomationStatus.ACTIVE
    controller.shutdown()


def test_closing_automatic_countdown_suppresses_only_until_app_restart() -> None:
    clock = [_now(19, 25)]
    controller, settings, source, session, _, _ = _controller(now=clock)

    def accept(target: QDateTime, presentation: str, occurrence_id: str) -> None:
        session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })

    controller.countdown_requested.connect(accept)
    controller.evaluate()
    session.reset_state()

    assert controller.snapshot().status is MediaCountdownAutomationStatus.SUPPRESSED

    restarted = MediaCountdownAutomationController(
        settings=settings,
        schedule_source=source,
        projection_session=session,
        playback_protection=_Protection(),
        now_provider=lambda: clock[0],
    )
    restarted_requests: list[QDateTime] = []
    restarted.countdown_requested.connect(
        lambda target, _presentation, _occurrence: restarted_requests.append(target)
    )
    restarted.evaluate()

    assert len(restarted_requests) == 1
    assert restarted_requests[0].toSecsSinceEpoch() == int(_now(19, 30).timestamp())


def test_projected_media_temporarily_displaces_and_then_restores_countdown() -> None:
    clock = [_now(19, 25)]
    controller, _, _, session, _, _ = _controller(now=clock)
    requests: list[str] = []

    def accept(target: QDateTime, presentation: str, occurrence_id: str) -> None:
        requests.append(occurrence_id)
        session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })

    controller.countdown_requested.connect(accept)
    controller.start()

    assert len(requests) == 1
    assert controller.snapshot().status is MediaCountdownAutomationStatus.ACTIVE

    session.set_state({"type": "image"})

    assert len(requests) == 1
    assert session.state_type == "image"
    assert controller.snapshot().status is (
        MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
    )
    assert controller.snapshot().blocking_reason is MediaCountdownBlockingReason.IN_USE

    session.set_state({"type": "video"})

    assert len(requests) == 1
    assert session.state_type == "video"

    session.reset_state()

    assert len(requests) == 2
    assert session.state_type == "timer"
    assert controller.snapshot().status is MediaCountdownAutomationStatus.ACTIVE
    controller.shutdown()


def test_displaced_countdown_never_returns_after_the_meeting_starts() -> None:
    clock = [_now(19, 25)]
    controller, _, _, session, _, _ = _controller(now=clock)
    requests: list[str] = []

    def accept(target: QDateTime, presentation: str, occurrence_id: str) -> None:
        requests.append(occurrence_id)
        session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })

    controller.countdown_requested.connect(accept)
    controller.start()
    session.set_state({"type": "video"})

    clock[0] = _now(19, 30)
    controller.evaluate()
    session.reset_state()

    assert len(requests) == 1
    assert session.state_type == "idle"
    assert controller.snapshot().status is MediaCountdownAutomationStatus.MISSED
    controller.shutdown()


def test_schedule_change_does_not_let_automation_overwrite_displacing_media() -> None:
    clock = [_now(19, 25)]
    controller, _, source, session, _, _ = _controller(now=clock)
    targets: list[QDateTime] = []

    def accept(target: QDateTime, presentation: str, occurrence_id: str) -> None:
        targets.append(target)
        session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })

    controller.countdown_requested.connect(accept)
    controller.start()
    session.set_state({"type": "image"})

    source.schedule = _schedule(meeting_minute=35)
    controller.reload_schedule()

    assert len(targets) == 1
    assert session.state_type == "image"
    assert controller.snapshot().status is (
        MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
    )

    session.reset_state()

    assert len(targets) == 2
    assert targets[-1].toSecsSinceEpoch() == int(_now(19, 35).timestamp())
    assert session.state_type == "timer"
    controller.shutdown()


def test_manual_restart_overrides_the_current_session_suppression() -> None:
    clock = [_now(19, 25)]
    controller, _, _, session, _, _ = _controller(now=clock)

    def accept(target: QDateTime, presentation: str, occurrence_id: str) -> None:
        session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })

    controller.countdown_requested.connect(accept)
    controller.evaluate()
    target = session.state["target_dt"]
    session.reset_state()

    assert controller.snapshot().status is MediaCountdownAutomationStatus.SUPPRESSED

    session.set_state({
        "type": "timer",
        "target_dt": target,
        "presentation": MediaCountdownPresentation.CIRCULAR.value,
        "origin": "manual",
    })
    controller.evaluate()

    assert controller.snapshot().status is MediaCountdownAutomationStatus.ACTIVE
    assert controller.snapshot().active_automatic is False


def test_matching_manual_countdown_is_not_replaced_and_its_close_is_suppressed() -> None:
    clock = [_now(19, 25)]
    controller, _, _, session, protection, _ = _controller(
        now=clock,
        allowed=False,
    )
    controller.evaluate()
    target = QDateTime.fromSecsSinceEpoch(int(_now(19, 30).timestamp()))

    session.set_state({
        "type": "timer",
        "target_dt": target,
        "presentation": MediaCountdownPresentation.CIRCULAR.value,
        "origin": "manual",
    })

    assert controller.snapshot().status is MediaCountdownAutomationStatus.ACTIVE
    assert controller.snapshot().active_automatic is False

    protection.allowed = True
    controller.evaluate()
    session.reset_state()

    assert controller.snapshot().status is MediaCountdownAutomationStatus.SUPPRESSED


def test_manual_takeover_of_same_target_is_not_stopped_by_schedule_reload() -> None:
    clock = [_now(19, 25)]
    controller, _, source, session, _, _ = _controller(now=clock)
    controller.countdown_requested.connect(
        lambda target, presentation, occurrence_id: session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })
    )
    controller.evaluate()
    target = session.state["target_dt"]
    session.set_state({
        "type": "timer",
        "target_dt": target,
        "presentation": MediaCountdownPresentation.CIRCULAR.value,
        "origin": "manual",
    })
    stops: list[bool] = []
    controller.automatic_stop_requested.connect(lambda: stops.append(True))

    source.schedule = MeetingSchedule(MeetingSlot(MIDWEEK), MeetingSlot(WEEKEND))
    controller.reload_schedule()

    assert stops == []
    assert session.state["origin"] == "manual"


def test_removing_the_active_meeting_stops_only_the_automatic_countdown() -> None:
    clock = [_now(19, 25)]
    controller, _, source, session, _, _ = _controller(now=clock)
    controller.countdown_requested.connect(
        lambda target, presentation, occurrence_id: session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })
    )
    controller.automatic_stop_requested.connect(session.reset_state)
    controller.evaluate()

    source.schedule = MeetingSchedule(MeetingSlot(MIDWEEK), MeetingSlot(WEEKEND))
    controller.reload_schedule()

    assert session.state_type == "idle"
    assert controller.snapshot().status is (
        MediaCountdownAutomationStatus.SCHEDULE_REQUIRED
    )


def test_disabling_automation_does_not_stop_an_active_countdown() -> None:
    clock = [_now(19, 25)]
    controller, _, _, session, _, _ = _controller(now=clock)
    controller.countdown_requested.connect(
        lambda target, presentation, occurrence_id: session.set_state({
            "type": "timer",
            "target_dt": target,
            "presentation": presentation,
            "origin": "automatic_countdown",
            "occurrence_id": occurrence_id,
        })
    )
    stop_requests: list[bool] = []
    controller.automatic_stop_requested.connect(lambda: stop_requests.append(True))
    controller.evaluate()

    controller.set_enabled(False)

    assert stop_requests == []
    assert session.state["type"] == "timer"
    assert controller.snapshot().status is MediaCountdownAutomationStatus.DISABLED


def test_missing_schedule_and_projection_target_are_distinct_operator_states() -> None:
    clock = [_now(19, 25)]
    unconfigured = MeetingSchedule(MeetingSlot(MIDWEEK), MeetingSlot(WEEKEND))
    no_schedule, *_ = _controller(now=clock, schedule=unconfigured)

    no_schedule.evaluate()

    assert no_schedule.snapshot().status is (
        MediaCountdownAutomationStatus.SCHEDULE_REQUIRED
    )

    no_target, *_ = _controller(now=clock, target_available=False)
    no_target.evaluate()

    assert no_target.snapshot().status is (
        MediaCountdownAutomationStatus.WAITING_FOR_PROJECTION
    )
    assert no_target.snapshot().has_projection_target is False
    assert no_target.snapshot().blocking_reason is (
        MediaCountdownBlockingReason.NO_WINDOW
    )


def test_missed_notification_distinguishes_missing_window_from_policy_block() -> None:
    clock = [_now(19, 29)]
    no_target, _, _, _, _, no_target_notifications = _controller(
        now=clock,
        target_available=False,
    )
    no_target.evaluate()
    clock[0] = _now(19, 30)
    no_target.evaluate()

    assert no_target_notifications.warning_messages[0][0] == (
        "The countdown did not start because no media window was available."
    )

    clock[0] = _now(19, 29)
    policy_blocked, _, _, _, _, policy_notifications = _controller(
        now=clock,
        allowed=False,
    )
    policy_blocked.evaluate()
    assert policy_blocked.snapshot().blocking_reason is (
        MediaCountdownBlockingReason.AUTOMATION_UNAVAILABLE
    )
    clock[0] = _now(19, 30)
    policy_blocked.evaluate()

    assert policy_notifications.warning_messages[0][0] == (
        "The countdown did not start because automatic projection remained unavailable."
    )


def test_manual_target_prefers_todays_future_meeting_then_next_full_hour() -> None:
    clock = [_now(19, 0)]
    controller, *_ = _controller(now=clock)

    assert controller.suggested_manual_target() == (19, 30, True)

    clock[0] = _now(20, 10)

    assert controller.suggested_manual_target() == (21, 0, False)

    clock[0] = _now(23, 40)

    assert controller.suggested_manual_target() == (0, 0, False)
