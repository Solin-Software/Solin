from __future__ import annotations

from datetime import date, datetime

import pytest

from solin.core.timer.models import CustomTimerSchedule, MeetingType, Section, TimerPart
from solin.core.timer.pacing import PacingStatus, pacing_snapshot
from solin.core.timer.pacing_models import PacingTransitions, PacingWindow
from solin.core.timer.schedule_factory import build_default_schedule
from solin.core.timer.state_machine import TimerStateMachine


_T0 = int(datetime(2026, 11, 25, 19, 0).timestamp())


def _custom(*durations: int) -> CustomTimerSchedule:
    schedule = CustomTimerSchedule.new("Program")
    schedule.entries.extend(TimerPart.new(None, str(index), seconds) for index, seconds in enumerate(durations))
    return schedule


@pytest.mark.parametrize("meeting_type", (MeetingType.MIDWEEK, MeetingType.WEEKEND))
def test_five_minute_meeting_opening_does_not_get_counted_as_a_delay(meeting_type):
    schedule = build_default_schedule(date(2026, 11, 23), meeting_type)
    schedule.pacing_window = PacingWindow("19:00", "20:45")
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert pacing_snapshot(schedule, _T0).projected_end_epoch == _T0 + 6300
    assert machine.start_part(schedule.parts[0].id, now=_T0 + 300)
    result = pacing_snapshot(schedule, _T0 + 300)
    assert result.projected_end_epoch == _T0 + 6300
    assert result.status is PacingStatus.ON_TIME
    assert machine.reset_all()
    assert machine.start_part(schedule.parts[0].id, now=_T0 + 345)
    late = pacing_snapshot(schedule, _T0 + 345)
    assert late.deviation_seconds == 45
    assert late.status is PacingStatus.BEHIND


def test_midweek_ministry_gap_accounts_for_counsel_and_changeover():
    schedule = build_default_schedule(date(2026, 11, 23), MeetingType.MIDWEEK)
    schedule.pacing_window = PacingWindow("19:00", "20:45")
    first = schedule.parts_in(Section.MINISTRY)[0]
    machine = TimerStateMachine()
    machine.set_schedule(schedule)
    assert machine.start_part(first.id, now=_T0 + 2000)
    assert machine.stop_part(first.id, now=_T0 + 2240)

    expected = pacing_snapshot(schedule, _T0 + 2240).projected_end_epoch
    assert pacing_snapshot(schedule, _T0 + 2320).projected_end_epoch == expected
    assert pacing_snapshot(schedule, _T0 + 2335).projected_end_epoch == expected + 15


def test_known_transition_estimates_can_differ_and_take_priority_over_the_reserve():
    schedule = _custom(240, 300)
    first, second = schedule.parts
    schedule.pacing_window = PacingWindow(
        "19:00", "19:17", 0, PacingTransitions(((first.id, 300), (second.id, 60)), closing_seconds=120),
    )
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert pacing_snapshot(schedule, _T0).projected_end_epoch == _T0 + 1020
    assert machine.start_part(first.id, now=_T0 + 300)
    assert machine.stop_part(first.id, now=_T0 + 540)
    assert pacing_snapshot(schedule, _T0 + 580).projected_end_epoch == _T0 + 1020
    assert pacing_snapshot(schedule, _T0 + 615).deviation_seconds == 15
    assert machine.start_part(second.id, now=_T0 + 615)
    assert machine.stop_part(second.id, now=_T0 + 915)
    assert pacing_snapshot(schedule, _T0 + 1200).projected_end_epoch == _T0 + 1035


def test_only_unspecified_transitions_share_the_remaining_reserve():
    schedule = _custom(120, 120)
    first, second = schedule.parts
    schedule.pacing_window = PacingWindow(
        "19:00", "19:09", 300, PacingTransitions(((first.id, 120),), closing_seconds=60),
    )
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert machine.start_part(first.id, now=_T0 + 120)
    assert machine.stop_part(first.id, now=_T0 + 240)
    assert pacing_snapshot(schedule, _T0 + 350).projected_end_epoch == _T0 + 540
    assert pacing_snapshot(schedule, _T0 + 380).projected_end_epoch == _T0 + 560
    assert machine.start_part(second.id, now=_T0 + 380)
    assert machine.stop_part(second.id, now=_T0 + 500)
    assert pacing_snapshot(schedule, _T0 + 600).deviation_seconds == 20


def test_transition_consumes_estimate_then_actual_time_without_extra_parts():
    schedule = _custom(240, 300, 180)
    schedule.pacing_window = PacingWindow("19:00", "19:16", 240)
    first, second, third = schedule.parts
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert pacing_snapshot(schedule, _T0).projected_end_epoch == _T0 + 960
    assert machine.start_part(first.id, now=_T0 + 60)
    assert machine.stop_part(first.id, now=_T0 + 300)
    assert pacing_snapshot(schedule, _T0 + 340).projected_end_epoch == _T0 + 960
    assert pacing_snapshot(schedule, _T0 + 375).deviation_seconds == 15
    assert machine.start_part(second.id, now=_T0 + 375)
    assert machine.adjust_elapsed(second.id, 5, now=_T0 + 380)
    assert second.actual_started_epoch - first.actual_stopped_epoch == 75
    assert pacing_snapshot(schedule, _T0 + 380).deviation_seconds == 10
    assert machine.stop_part(second.id, now=_T0 + 675)
    assert machine.start_part(third.id, now=_T0 + 735)
    assert machine.stop_part(third.id, now=_T0 + 915)

    restored = CustomTimerSchedule.from_dict(schedule.to_dict())
    result = pacing_snapshot(restored, _T0 + 3000)
    assert result.complete and result.projected_end_epoch == _T0 + 975
    assert result.status is PacingStatus.ON_TIME
    assert restored.parts[1].actual_stopped_epoch == _T0 + 675


@pytest.mark.parametrize(("offset", "shift"), [(100, -1), (-100, 1)])
def test_weighted_adjustment_is_applied_at_start_and_keeps_original_plan(offset, shift):
    schedule = _custom(300, 300, 300)
    schedule.pacing_window = PacingWindow("19:00", "19:15", 0)
    first, second, fixed = schedule.parts
    first.adjustment_weight, second.adjustment_weight = 100, 80
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    start = _T0 + offset
    assert machine.start_part(first.id, now=start, automatic=True)
    assert machine.snapshot(now=start).remaining_seconds == 300 + shift * 56
    assert first.planned_seconds == second.effective_seconds == 300
    assert pacing_snapshot(schedule, start, automatic=True).adjusted_end_epoch == _T0 + 900
    second_start = start + first.effective_seconds
    assert machine.start_part(second.id, now=second_start, automatic=True)
    assert second.effective_seconds == 300 + shift * 44
    assert machine.start_part(fixed.id, now=second_start + second.effective_seconds + 60, automatic=True)
    assert fixed.effective_seconds == 300


def test_unmarked_custom_parts_share_adjustment_with_equal_weights_above_the_floor():
    schedule = _custom(120, 45, 135)
    schedule.pacing_window = PacingWindow("19:00", "19:05", 0)
    first, short, last = schedule.parts
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert machine.start_part(first.id, now=_T0 + 80, automatic=True)
    assert first.effective_seconds == 80
    assert machine.start_part(short.id, now=_T0 + 160, automatic=True)
    assert short.effective_seconds == 45
    assert machine.start_part(last.id, now=_T0 + 205, automatic=True)
    assert last.effective_seconds == 95
    assert pacing_snapshot(schedule, _T0 + 205, automatic=True).adjusted_end_epoch == _T0 + 300


def test_saturation_redistributes_and_publishes_unrecoverable_delay():
    schedule = _custom(61, 119)
    schedule.pacing_window = PacingWindow("19:00", "19:03", 0)
    first, second = schedule.parts
    first.adjustment_weight, second.adjustment_weight = 100, 80
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert machine.start_part(first.id, now=_T0 + 90, automatic=True)
    assert first.effective_seconds == 60
    assert pacing_snapshot(schedule, _T0 + 90, automatic=True).residual_seconds == 30
    assert machine.start_part(second.id, now=_T0 + 150, automatic=True)
    assert second.effective_seconds == 60
    assert pacing_snapshot(schedule, _T0 + 150).status is PacingStatus.ON_TIME


def test_extension_flag_leaves_an_early_run_at_original_duration():
    schedule = _custom(120)
    schedule.pacing_window = PacingWindow("19:00", "19:05", 0)
    (part,) = schedule.parts
    part.adjustment_weight = 100
    machine = TimerStateMachine(allow_pacing_extension=False)
    machine.set_schedule(schedule)

    assert machine.start_part(part.id, now=_T0, automatic=True)
    assert part.effective_seconds == 120
    result = pacing_snapshot(schedule, _T0, automatic=True, allow_extension=False)
    assert result.status is PacingStatus.AHEAD
    assert result.adjusted_end_epoch == result.projected_end_epoch


def test_only_remaining_meeting_candidate_can_use_full_capacity():
    schedule = build_default_schedule(date(2026, 11, 23), MeetingType.MIDWEEK)
    schedule.pacing_window = PacingWindow("19:00", "20:45")
    study = schedule.parts_in(Section.LIVING)[-1]
    study.title_override = "Renamed study"
    concluding = schedule.parts_in(Section.CONCLUDING_COMMENTS)[0]
    machine = TimerStateMachine()
    machine.set_schedule(schedule)

    assert machine.start_part(study.id, now=_T0 + 6100, automatic=True)
    assert study.effective_seconds == 60
    assert pacing_snapshot(schedule, _T0 + 6100, automatic=True).residual_seconds > 0
    assert machine.start_part(concluding.id, now=_T0 + 6160, automatic=True)
    assert concluding.effective_seconds == concluding.planned_seconds
    assert machine.stop_part(concluding.id, now=_T0 + 6340)
    assert pacing_snapshot(schedule, _T0 + 7000).complete
    assert machine.start_part(study.id, now=_T0 + 7100, automatic=True)
    assert not pacing_snapshot(schedule, _T0 + 7100).complete


def test_reset_brings_part_back_into_remaining_time_and_reopens_completion():
    schedule = _custom(100, 100)
    schedule.pacing_window = PacingWindow("19:00", "19:04", 0)
    first, second = schedule.parts
    machine = TimerStateMachine()
    machine.set_schedule(schedule)
    assert machine.start_part(first.id, now=_T0)
    assert machine.start_part(second.id, now=_T0 + 100)
    assert machine.stop_part(second.id, now=_T0 + 200)
    assert pacing_snapshot(schedule, _T0 + 200).complete

    assert machine.reset_part(first.id)
    after_reset = pacing_snapshot(CustomTimerSchedule.from_dict(schedule.to_dict()), _T0 + 200)
    assert not after_reset.complete
    assert after_reset.projected_end_epoch == _T0 + 300


def test_unstarted_window_resolves_to_today_and_migrates_legacy_epochs():
    schedule = _custom(300)
    schedule.pacing_window = PacingWindow("19:30", "20:00")
    tuesday = int(datetime(2026, 11, 24, 19, 30).timestamp())
    thursday = int(datetime(2026, 11, 26, 19, 30).timestamp())
    assert pacing_snapshot(schedule, tuesday).end_epoch == tuesday + 1800
    assert pacing_snapshot(schedule, thursday).end_epoch == thursday + 1800

    legacy = PacingWindow.from_dict(
        {"start_epoch": tuesday, "end_epoch": tuesday + 1800, "untracked_seconds": 60}
    )
    assert legacy == PacingWindow("19:30", "20:00", 60)
