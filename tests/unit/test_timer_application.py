from __future__ import annotations

from datetime import date, timedelta

import pytest

from solin.core.timer.application import (
    AssignmentAutofillScope,
    DetectedPartTiming,
    TimerSession,
)
from solin.core.timer.custom_schedule import TimerOperationError
from solin.core.timer.models import (
    AssignmentSetup,
    ClockConfig,
    ClockMode,
    Direction,
    MeetingSchedule,
    MeetingType,
    PartKind,
    PartState,
    Section,
)
from solin.core.timer.pacing_models import PacingMode
from solin.core.timer.schedule_factory import build_default_schedule, configurable_parts_in
from tests._paths import REPO_ROOT


_WEEK = date(2026, 6, 1)


class _Repository:
    def __init__(self) -> None:
        self.clock_config = ClockConfig(
            mode=ClockMode.ANALOG,
            text_scale_pct=250,
            freeze_seconds=-10,
        )
        self.timer_visible = True
        self.pacing_mode = PacingMode.OFF
        self.last_meeting_type = MeetingType.MIDWEEK
        self.schedules: dict[tuple[date, MeetingType], MeetingSchedule] = {}
        self.saved_clock_configs: list[ClockConfig] = []
        self.saved_meeting_types: list[MeetingType] = []
        self.saved_schedules: list[MeetingSchedule] = []

    def load_clock_config(self) -> ClockConfig:
        return self.clock_config

    def save_clock_config(self, config: ClockConfig) -> None:
        self.saved_clock_configs.append(config)
        self.clock_config = config

    def load_pacing_mode(self) -> PacingMode:
        return self.pacing_mode

    def save_pacing_mode(self, mode: PacingMode) -> None:
        self.pacing_mode = mode

    def get_timer_visible(self) -> bool:
        return self.timer_visible

    def set_timer_visible(self, visible: bool) -> None:
        self.timer_visible = visible

    def load_last_meeting_type(self) -> MeetingType:
        return self.last_meeting_type

    def save_last_meeting_type(self, meeting_type: MeetingType) -> None:
        self.saved_meeting_types.append(meeting_type)
        self.last_meeting_type = meeting_type

    def load_schedule(
        self,
        week_monday: date,
        meeting_type: MeetingType,
    ) -> MeetingSchedule | None:
        return self.schedules.get((week_monday, meeting_type))

    def save_schedule(self, schedule: MeetingSchedule) -> None:
        self.saved_schedules.append(schedule)


def test_timer_session_loads_context_and_clamps_clock_config() -> None:
    repo = _Repository()
    persisted = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    repo.schedules[(_WEEK, MeetingType.MIDWEEK)] = persisted

    session = TimerSession(repo, current_week_monday=_WEEK)

    assert session.week_monday == _WEEK
    assert session.is_current_week
    assert session.meeting_type is MeetingType.MIDWEEK
    assert session.schedule is persisted
    assert session.clock_config.text_scale_pct == 100
    assert session.clock_config.freeze_seconds == 0


def test_timer_session_switches_week_and_meeting_type_through_repository() -> None:
    repo = _Repository()
    next_week = date(2026, 6, 8)
    weekend = build_default_schedule(next_week, MeetingType.WEEKEND)
    repo.schedules[(next_week, MeetingType.WEEKEND)] = weekend
    session = TimerSession(repo, current_week_monday=_WEEK)

    session.next_week()
    assert session.week_monday == next_week
    assert not session.is_current_week

    assert session.set_meeting_type(MeetingType.WEEKEND)
    assert session.meeting_type is MeetingType.WEEKEND
    assert session.schedule is weekend
    assert repo.saved_meeting_types == [MeetingType.WEEKEND]

    assert session.go_to_current_week(_WEEK) == -1
    assert session.week_monday == _WEEK
    assert session.is_current_week


def test_timer_session_persists_schedule_edits_only_for_idle_parts() -> None:
    repo = _Repository()
    session = TimerSession(repo, current_week_monday=_WEEK)
    part = session.schedule.parts_in(Section.MINISTRY)[0]
    original_seconds = part.planned_seconds

    assert session.adjust_part(part.id, 60)
    assert part.planned_seconds == original_seconds + 60
    assert repo.saved_schedules[-1] is session.schedule

    saved_count = len(repo.saved_schedules)
    part.state = PartState.RUNNING

    assert not session.set_part_seconds(part.id, 60)
    assert len(repo.saved_schedules) == saved_count


def test_timer_session_updates_clock_and_visibility_through_repository() -> None:
    repo = _Repository()
    session = TimerSession(repo, current_week_monday=_WEEK)

    assert session.update_clock("unknown", "ignored") is None
    assert repo.saved_clock_configs == []

    config = session.update_clock("direction", Direction.UP.value)
    assert config is not None
    assert config.direction is Direction.UP
    assert repo.saved_clock_configs == [config]

    assert session.timer_visible() is True
    session.set_timer_visible(False)
    assert session.timer_visible() is False



def _detected_timings() -> list[DetectedPartTiming]:
    return [
        DetectedPartTiming(section, kind, seconds)
        for section, kind, durations in [
            (Section.OPENING_COMMENTS, PartKind.STANDARD, (120,)),
            (Section.TREASURES, PartKind.STANDARD, (660, 540, 240)),
            (Section.MINISTRY, PartKind.STANDARD, (180, 240, 300, None)),
            (Section.LIVING, PartKind.STANDARD, (900,)),
            (Section.LIVING, PartKind.CBS, (1500,)),
            (Section.CONCLUDING_COMMENTS, PartKind.STANDARD, (240,)),
        ]
        for seconds in durations
    ]


@pytest.mark.parametrize("scope", list(AssignmentAutofillScope))
def test_autofill_applies_only_enabled_parts_and_persists_once(scope) -> None:
    repo = _Repository()
    session = TimerSession(repo, _WEEK)
    before = session.schedule.to_dict()
    ministry_ids = [part.id for part in session.schedule.parts_in(Section.MINISTRY)]

    applied = session.apply_detected_assignments(_WEEK, _detected_timings(), scope)

    if scope is AssignmentAutofillScope.OFF:
        assert not applied
        assert session.schedule.to_dict() == before
        assert repo.saved_schedules == []
        return

    assert applied
    schedule = session.schedule
    assert repo.saved_schedules == [schedule]
    assert schedule.assignment_setup is AssignmentSetup.DETECTED
    assert not session.can_autofill_assignments(_WEEK)
    ministry = schedule.parts_in(Section.MINISTRY)
    assert [p.id for p in ministry[:3]] == ministry_ids
    assert [p.planned_seconds for p in ministry] == [180, 240, 300, 240]
    assert [p.planned_seconds for p in configurable_parts_in(schedule, Section.LIVING)] == [900]
    assert all("Part" in p.title for p in ministry)
    extended = scope is AssignmentAutofillScope.ALL_MIDWEEK_DURATIONS
    assert schedule.parts_in(Section.LIVING)[-1].planned_seconds == (1500 if extended else 1800)
    assert schedule.parts_in(Section.TREASURES)[0].planned_seconds == (660 if extended else 600)
    assert schedule.parts[0].planned_seconds == (120 if extended else 60)
    assert schedule.parts[-1].planned_seconds == (240 if extended else 180)


@pytest.mark.parametrize("change", ["none", "duration", "count", "live"])
def test_legacy_autofill_checks_schedule_values_and_timing_without_ids(change) -> None:
    repo = _Repository()
    original = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    if change == "duration":
        original.parts[0].planned_seconds += 60
    elif change == "count":
        original.parts.pop(4)
    elif change == "live":
        original.parts[0].state = PartState.STOPPED
        original.parts[0].accumulated_seconds = 32
    legacy = original.to_dict()
    legacy.pop("assignment_setup")
    repo.schedules[(_WEEK, MeetingType.MIDWEEK)] = MeetingSchedule.from_dict(legacy)

    session = TimerSession(repo, _WEEK)

    assert session.can_autofill_assignments(_WEEK) is (change == "none")


def test_autofill_is_ineligible_in_the_past_or_for_weekend() -> None:
    repo = _Repository()
    session = TimerSession(repo, _WEEK)
    assert session.can_autofill_assignments(_WEEK)
    session.previous_week()
    assert not session.can_autofill_assignments(_WEEK)
    session.next_week()
    session.next_week()
    assert session.can_autofill_assignments(_WEEK)
    assert not session.can_autofill_assignments(_WEEK + timedelta(weeks=2))
    assert session.set_meeting_type(MeetingType.WEEKEND)
    assert not session.can_autofill_assignments(_WEEK)


def test_unknown_part_count_does_not_partially_apply_or_prevent_retry() -> None:
    repo = _Repository()
    session = TimerSession(repo, _WEEK)
    before = session.schedule.to_dict()
    timings = [timing for timing in _detected_timings() if timing.section is not Section.LIVING]

    assert not session.apply_detected_assignments(_WEEK, timings, AssignmentAutofillScope.VARIABLE_SECTIONS)
    assert session.schedule.to_dict() == before
    assert repo.saved_schedules == []
    assert session.can_autofill_assignments(_WEEK)


def test_manual_edits_protect_week_even_after_values_are_restored() -> None:
    repo = _Repository()
    session = TimerSession(repo, _WEEK)
    part = session.schedule.parts[0]
    planned = part.planned_seconds
    assert not session.set_part_seconds(part.id, planned)
    assert session.can_autofill_assignments(_WEEK)
    assert session.set_part_seconds(part.id, planned + 60)
    assert session.set_part_seconds(part.id, planned)
    persisted = MeetingSchedule.from_dict(session.schedule.to_dict())
    repo.schedules[(_WEEK, MeetingType.MIDWEEK)] = persisted

    restored = TimerSession(repo, _WEEK)

    assert not restored.can_autofill_assignments(_WEEK)


def test_timer_source_boundaries_keep_application_pure_and_widget_injected() -> None:
    application_source = (
        REPO_ROOT / "src" / "solin" / "core" / "timer" / "application.py"
    ).read_text(encoding="utf-8")
    widget_source = (
        REPO_ROOT / "src" / "solin" / "widgets" / "timer_widget.py"
    ).read_text(encoding="utf-8")
    bridge_source = (
        REPO_ROOT / "src" / "solin" / "ui" / "qml" / "timer_bridge.py"
    ).read_text(encoding="utf-8")

    assert "PySide6" not in application_source
    assert "ProfileSettings" not in application_source
    assert "SettingsStore" not in application_source
    assert "TimerBridge(" not in widget_source
    assert "QFileDialog" not in bridge_source
    assert "QMessageBox" not in bridge_source
    assert "QStandardPaths" not in bridge_source


def test_meeting_part_edit_is_atomic_and_increments_revision_once(monkeypatch) -> None:
    repo = _Repository()
    session = TimerSession(repo, current_week_monday=_WEEK)
    part = next(p for p in session.schedule.parts if p.planned_seconds >= 120)
    original = (part.title_override, part.planned_seconds)
    revision = session.meeting_revision
    for invalid in (0, 86400, 80000):
        with pytest.raises(TimerOperationError):
            session.edit_part(part.id, "Invalid edit", invalid)
        assert (part.title_override, part.planned_seconds) == original
        assert session.meeting_revision == revision
        assert repo.saved_schedules == []

    def failed_write(_schedule):
        raise OSError("storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "save_schedule", failed_write)
        with pytest.raises(OSError):
            session.edit_part(part.id, "Failed edit", original[1] - 60)
    assert (part.title_override, part.planned_seconds) == original
    assert session.meeting_revision == revision
    assert session.edit_part(part.id, "New title", original[1] - 60)
    assert (part.title_override, part.planned_seconds) == ("New title", original[1] - 60)
    assert session.meeting_revision == revision + 1
    assert len(repo.saved_schedules) == 1
    with monkeypatch.context() as patch:
        patch.setattr(repo, "save_schedule", failed_write)
        with pytest.raises(OSError):
            session.edit_part(part.id, None, original[1], restore_title=True)
    assert (part.title_override, part.planned_seconds) == ("New title", original[1] - 60)
    assert session.meeting_revision == revision + 1
    assert session.edit_part(part.id, None, original[1], restore_title=True)
    assert (part.title_override, part.planned_seconds) == (None, original[1])
