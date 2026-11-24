from __future__ import annotations

from datetime import date

import pytest

from solin.core.timer.models import CustomTimerSchedule, MeetingType, Section, TimerPart
from solin.core.timer.pacing import PacingStatus, pacing_snapshot
from solin.core.timer.pacing_models import PacingTransitions, PacingWindow
from solin.core.timer.schedule_factory import build_default_schedule
from solin.core.timer.state_machine import TimerStateMachine


def _custom(*durations: int) -> CustomTimerSchedule:
    schedule = CustomTimerSchedule.new("Program")
    schedule.entries.extend(TimerPart.new(None, str(index), seconds) for index, seconds in enumerate(durations))
    return schedule


@pytest.mark.parametrize("meeting_type", (MeetingType.MIDWEEK, MeetingType.WEEKEND))
def test_five_minute_meeting_opening_does_not_get_counted_as_a_delay(meeting_type):
    schedule = build_default_schedule(date(2026, 11, 23), meeting_type)
    schedule.pacing_window = PacingWindow(1000, 7300)
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert pacing_snapshot(schedule, 1000).projected_end_epoch == 7300
    assert machine.start_part(schedule.parts[0].id, now=1300)
    result = pacing_snapshot(schedule, 1300)
    assert result.projected_end_epoch == 7300
    assert result.status is PacingStatus.ON_TIME
    assert machine.reset_all()
    assert machine.start_part(schedule.parts[0].id, now=1345)
    late = pacing_snapshot(schedule, 1345)
    assert late.deviation_seconds == 45
    assert late.status is PacingStatus.BEHIND


def test_midweek_ministry_gap_accounts_for_counsel_and_changeover():
    schedule = build_default_schedule(date(2026, 11, 23), MeetingType.MIDWEEK)
    schedule.pacing_window = PacingWindow(1000, 7300)
    first = schedule.parts_in(Section.MINISTRY)[0]
    machine = TimerStateMachine()
    machine.set_schedule(schedule)
    assert machine.start_part(first.id, now=3000)
    assert machine.stop_part(first.id, now=3240)

    expected = pacing_snapshot(schedule, 3240).projected_end_epoch
    assert pacing_snapshot(schedule, 3320).projected_end_epoch == expected
    assert pacing_snapshot(schedule, 3335).projected_end_epoch == expected + 15


def test_known_transition_estimates_can_differ_and_take_priority_over_the_reserve():
    schedule = _custom(240, 300)
    first, second = schedule.parts
    schedule.pacing_window = PacingWindow(
        1000, 1990, 0, PacingTransitions(((first.id, 300), (second.id, 60)), closing_seconds=90),
    )
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert pacing_snapshot(schedule, 1000).projected_end_epoch == 1990
    assert machine.start_part(first.id, now=1300)
    assert machine.stop_part(first.id, now=1540)
    assert pacing_snapshot(schedule, 1580).projected_end_epoch == 1990
    assert pacing_snapshot(schedule, 1615).deviation_seconds == 15
    assert machine.start_part(second.id, now=1615)
    assert machine.stop_part(second.id, now=1915)
    assert pacing_snapshot(schedule, 2200).projected_end_epoch == 2005


def test_only_unspecified_transitions_share_the_remaining_reserve():
    schedule = _custom(120, 120)
    first, second = schedule.parts
    schedule.pacing_window = PacingWindow(
        1000, 1500, 260, PacingTransitions(((first.id, 120),), closing_seconds=20),
    )
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert machine.start_part(first.id, now=1120)
    assert machine.stop_part(first.id, now=1240)
    assert pacing_snapshot(schedule, 1350).projected_end_epoch == 1500
    assert pacing_snapshot(schedule, 1380).projected_end_epoch == 1520
    assert machine.start_part(second.id, now=1380)
    assert machine.stop_part(second.id, now=1500)
    assert pacing_snapshot(schedule, 1600).deviation_seconds == 20


def test_transition_consumes_estimate_then_actual_time_without_extra_parts():
    schedule = _custom(240, 300, 180)
    schedule.pacing_window = PacingWindow(1000, 1960, 240)
    first, second, third = schedule.parts
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert pacing_snapshot(schedule, 1000).projected_end_epoch == 1960
    assert machine.start_part(first.id, now=1060)
    assert machine.stop_part(first.id, now=1300)
    assert pacing_snapshot(schedule, 1340).projected_end_epoch == 1960
    assert pacing_snapshot(schedule, 1375).deviation_seconds == 15
    assert machine.start_part(second.id, now=1375)
    assert machine.adjust_elapsed(second.id, 5, now=1380)
    assert second.actual_started_epoch - first.actual_stopped_epoch == 75
    assert pacing_snapshot(schedule, 1380).deviation_seconds == 10
    assert machine.stop_part(second.id, now=1675)
    assert machine.start_part(third.id, now=1735)
    assert machine.stop_part(third.id, now=1915)

    restored = CustomTimerSchedule.from_dict(schedule.to_dict())
    result = pacing_snapshot(restored, 4000)
    assert result.complete and result.projected_end_epoch == 1975
    assert result.status is PacingStatus.ON_TIME
    assert restored.parts[1].actual_stopped_epoch == 1675


@pytest.mark.parametrize(("start", "shift"), [(1100, -1), (900, 1)])
def test_weighted_adjustment_is_applied_at_start_and_keeps_original_plan(start, shift):
    schedule = _custom(300, 300, 300)
    schedule.pacing_window = PacingWindow(1000, 1900, 0)
    schedule.automatic_pacing = True
    first, second, fixed = schedule.parts
    first.adjustment_weight, second.adjustment_weight = 100, 80
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert machine.start_part(first.id, now=start)
    assert machine.snapshot(now=start).remaining_seconds == 300 + shift * 56
    assert first.planned_seconds == second.effective_seconds == 300
    assert pacing_snapshot(schedule, start).adjusted_end_epoch == 1900
    second_start = start + first.effective_seconds
    assert machine.start_part(second.id, now=second_start)
    assert second.effective_seconds == 300 + shift * 44
    assert machine.start_part(fixed.id, now=second_start + second.effective_seconds)
    assert fixed.effective_seconds == 300


def test_saturation_redistributes_and_publishes_unrecoverable_delay():
    schedule = _custom(61, 120)
    schedule.pacing_window = PacingWindow(1000, 1181, 0)
    schedule.automatic_pacing = True
    first, second = schedule.parts
    first.adjustment_weight, second.adjustment_weight = 100, 80
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert machine.start_part(first.id, now=1090)
    assert first.effective_seconds == 60
    assert pacing_snapshot(schedule, 1090).residual_seconds == 29
    assert machine.start_part(second.id, now=1150)
    assert second.effective_seconds == 60
    assert pacing_snapshot(schedule, 1150).status is PacingStatus.ON_TIME


def test_extension_flag_leaves_an_early_run_at_original_duration():
    schedule = _custom(120)
    schedule.pacing_window = PacingWindow(1000, 1300, 0)
    schedule.automatic_pacing = True
    (part,) = schedule.parts
    part.adjustment_weight = 100
    machine = TimerStateMachine(allow_pacing_extension=False)
    machine.set_schedule(schedule)

    assert machine.start_part(part.id, now=1000)
    assert part.effective_seconds == 120
    result = pacing_snapshot(schedule, 1000, allow_extension=False)
    assert result.status is PacingStatus.AHEAD
    assert result.adjusted_end_epoch == result.projected_end_epoch


def test_only_remaining_meeting_candidate_can_use_full_capacity():
    schedule = build_default_schedule(date(2026, 11, 23), MeetingType.MIDWEEK)
    schedule.pacing_window = PacingWindow(1000, 7300)
    schedule.automatic_pacing = True
    study = schedule.parts_in(Section.LIVING)[-1]
    study.title_override = "Renamed study"
    concluding = schedule.parts_in(Section.CONCLUDING_COMMENTS)[0]
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert machine.start_part(study.id, now=7100)
    assert study.effective_seconds == 60
    assert pacing_snapshot(schedule, 7100).residual_seconds > 0
    assert machine.start_part(concluding.id, now=7160)
    assert concluding.effective_seconds == concluding.planned_seconds
    assert machine.stop_part(concluding.id, now=7340)
    assert pacing_snapshot(schedule, 8000).complete
    assert machine.start_part(study.id, now=8100)
    assert not pacing_snapshot(schedule, 8100).complete


def test_reset_brings_part_back_into_remaining_time_and_reopens_completion():
    schedule = _custom(100, 100)
    schedule.pacing_window = PacingWindow(1000, 1200, 0)
    first, second = schedule.parts
    machine = TimerStateMachine()
    machine.set_schedule(schedule)
    assert machine.start_part(first.id, now=1000)
    assert machine.start_part(second.id, now=1100)
    assert machine.stop_part(second.id, now=1200)
    assert pacing_snapshot(schedule, 1200).complete

    assert machine.reset_part(first.id)
    after_reset = pacing_snapshot(CustomTimerSchedule.from_dict(schedule.to_dict()), 1200)
    assert not after_reset.complete
    assert after_reset.projected_end_epoch == 1300
