from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtTest import QSignalSpy

from solin.controllers.timer_engine import TimerEngine
from solin.controllers.timer_operations import TimerOperations
from solin.core.foundation.settings_store import SettingsStore
from solin.core.meetings.schedule import MeetingSchedule as ProfileMeetingSchedule
from solin.core.meetings.schedule import MeetingSlot
from solin.core.timer import models, state_machine
from solin.core.timer.application import AssignmentAutofillScope, DetectedPartTiming, TimerSession
from solin.core.timer.custom_schedule import TimerDurationBudgetError, TimerOperationError
from solin.core.timer.infrastructure import JsonCustomTimerRepository, QSettingsTimerRepository
from solin.core.timer.models import AssignmentSetup, CustomTimerSchedule, MeetingType, PartKind, PartState, Section
from solin.core.timer.pacing import PacingAvailability
from solin.core.timer.pacing_models import PacingMode
from solin.core.timer.schedule_factory import build_default_schedule


_WEEK = date(2026, 11, 23)


@pytest.fixture
def timer_runtime(tmp_path, monkeypatch, qt_object_owner):
    def settings(store):
        path = str(tmp_path / f"{store.namespace.organization}.ini")
        return QSettings(path, QSettings.Format.IniFormat)

    monkeypatch.setattr(SettingsStore, "_settings", settings)
    now = [10_000.0]
    monkeypatch.setattr(models, "_now", lambda: now[0])
    monkeypatch.setattr(state_machine, "_now", lambda: now[0])

    def open_profile(profile="one", *, meeting_schedule=None):
        settings_store = SettingsStore.for_namespace(profile, "Timer")
        repository = QSettingsTimerRepository(settings_store)
        custom = JsonCustomTimerRepository(tmp_path / profile / "timer_schedules")
        session = TimerSession(repository, _WEEK, custom, meeting_schedule=meeting_schedule)
        engine = TimerEngine(qt_object_owner)
        operations = TimerOperations(engine, session, parent=qt_object_owner)
        return operations, custom, settings_store

    return open_profile, now


def _edit(operations, schedule_id, action, **values):
    schedule = operations.get_custom_schedule(schedule_id)
    assert schedule is not None
    assert operations.edit_custom_schedule(schedule_id, schedule.revision, action, **values)
    return operations.get_custom_schedule(schedule_id)


def test_pacing_preference_follows_the_profile_and_reports_a_missing_window(timer_runtime):
    open_profile, now = timer_runtime
    operations, _, _ = open_profile()
    assert operations.pacing_state(now[0]).mode is PacingMode.OFF
    assert operations.set_pacing_mode(PacingMode.AUTOMATIC)
    assert operations.pacing_state(now[0]).availability is PacingAvailability.MISSING_WINDOW
    operations.navigate_week("next")
    assert operations.pacing_state(now[0]).mode is PacingMode.AUTOMATIC

    schedule_id = operations.create_custom_schedule("Training").id
    schedule = _edit(operations, schedule_id, "add_part", title="Talk", planned_seconds=120)
    assert operations.select_custom_schedule(schedule_id)
    state = operations.pacing_state(now[0])
    assert state.mode is PacingMode.AUTOMATIC
    assert state.availability is PacingAvailability.MISSING_WINDOW and state.projection is None
    assert operations.start_part(schedule.parts[0].id)
    assert operations.engine.snapshot(now[0]).planned_seconds == 120
    assert operations.stop_part()

    _edit(operations, schedule_id, "set_pacing_window", start_epoch=10_000, end_epoch=10_180)
    assert operations.pacing_state(now[0]).availability is PacingAvailability.AWAITING_RESET
    assert operations.reset_all()
    restored, _, _ = open_profile()
    assert restored.pacing_state(now[0]).mode is PacingMode.AUTOMATIC
    assert restored.pacing_state(now[0]).availability is PacingAvailability.READY
    assert restored.start_part(schedule.parts[0].id)
    assert restored.engine.snapshot(now[0]).planned_seconds == 180

    other_profile, _, _ = open_profile("other")
    assert other_profile.pacing_state(now[0]).mode is PacingMode.OFF


def test_changing_pacing_mode_while_running_only_affects_future_targets(timer_runtime):
    open_profile, now = timer_runtime
    operations, _, _ = open_profile()
    schedule_id = operations.create_custom_schedule("Training").id
    _edit(operations, schedule_id, "add_part", title="Talk", planned_seconds=120)
    schedule = _edit(operations, schedule_id, "add_part", title="Practice", planned_seconds=120)
    first, second = schedule.parts
    _edit(operations, schedule_id, "set_pacing_window", start_epoch=10_000, end_epoch=10_240)
    assert operations.select_custom_schedule(schedule_id)
    assert operations.set_pacing_mode(PacingMode.AUTOMATIC)
    now[0] += 80
    assert operations.start_part(first.id)
    assert operations.engine.snapshot(now[0]).planned_seconds == 80
    assert operations.set_pacing_mode(PacingMode.MONITOR)
    assert operations.engine.snapshot(now[0]).planned_seconds == 80
    assert operations.pacing_state(now[0]).projection.available_shorten_seconds == 0
    now[0] += 80
    assert operations.start_part(second.id)
    assert operations.engine.snapshot(now[0]).planned_seconds == 120
    assert operations.session.schedule.parts[0].effective_seconds == 80


def test_custom_budget_edits_are_atomic_and_running_window_survives_reopen(timer_runtime):
    open_profile, now = timer_runtime
    operations, repository, _ = open_profile()
    schedule_id = operations.create_custom_schedule("Timing").id
    schedule = _edit(operations, schedule_id, "add_part", title="Talk", planned_seconds=120)
    part_id = schedule.parts[0].id
    schedule = _edit(
        operations, schedule_id, "set_pacing_window", start_epoch=10_000, end_epoch=10_180,
    )
    assert operations.set_pacing_mode(PacingMode.AUTOMATIC)

    for action, values in (
        ("set_pacing_window", {"start_epoch": 10_000, "end_epoch": 10_119}),
        ("set_part_seconds", {"part_id": part_id, "planned_seconds": 181}),
        ("add_part", {"title": "Too long", "planned_seconds": 61}),
    ):
        with pytest.raises(TimerDurationBudgetError) as error:
            operations.edit_custom_schedule(schedule_id, schedule.revision, action, **values)
        assert error.value.requested_seconds > error.value.available_seconds
        assert repository.load_schedule(schedule_id) == schedule

    assert operations.select_custom_schedule(schedule_id)
    assert operations.start_part(part_id)
    now[0] += 45
    restored, _, _ = open_profile()
    assert restored.engine.snapshot(now=now[0]).remaining_seconds == 135
    assert restored.pacing_state(now[0]).projection.end_epoch == 10_180
    assert restored.stop_part(part_id)
    changed = _edit(
        restored, schedule_id, "set_pacing_window", start_epoch=20_000, end_epoch=20_120,
    )
    assert restored.pacing_state(now[0]).projection.end_epoch == 10_180
    duplicate = restored.duplicate_custom_schedule(schedule_id, "Copy", changed.revision)
    assert duplicate.pacing_window == changed.pacing_window and duplicate.pacing_run is None
    assert restored.reset_all()
    assert restored.pacing_state(now[0]).projection.end_epoch == 20_120


def test_adding_a_part_reopens_the_forecast_of_a_completed_schedule(timer_runtime):
    open_profile, now = timer_runtime
    operations, _, _ = open_profile()
    schedule_id = operations.create_custom_schedule("Training").id
    schedule = _edit(operations, schedule_id, "add_part", title="Talk", planned_seconds=60)
    _edit(operations, schedule_id, "set_pacing_window", start_epoch=10_000, end_epoch=10_120)
    assert operations.select_custom_schedule(schedule_id)
    assert operations.start_part(schedule.parts[0].id)
    now[0] += 60
    assert operations.stop_part()
    assert operations.pacing_state(now[0]).projection.complete

    _edit(operations, schedule_id, "add_part", title="Questions", planned_seconds=60)
    restored, _, _ = open_profile()
    forecast = restored.pacing_state(now[0]).projection
    assert not forecast.complete
    assert forecast.projected_end_epoch == 10_120


def test_meeting_window_uses_profile_slot_for_untimed_weeks_and_freezes_on_start(timer_runtime):
    open_profile, now = timer_runtime
    time_text = ["19:30"]

    def load_settings():
        return ProfileMeetingSchedule(
            MeetingSlot.from_values("midweek", 2, time_text[0]),
            MeetingSlot.from_values("weekend", 6, "10:00"),
        )

    operations, _, settings = open_profile(meeting_schedule=load_settings)
    schedule = operations.session.schedule
    start = datetime(2026, 11, 25, 19, 30)
    assert schedule.pacing_window.start_epoch == start.timestamp()
    assert schedule.pacing_window.end_epoch == (start + timedelta(minutes=105)).timestamp()
    original = schedule.pacing_window

    operations.navigate_week("previous")
    assert operations.session.schedule.pacing_window is None
    operations.navigate_week("next")
    part = operations.session.schedule.parts[0]
    assert operations.rename_part(part.id, "Welcome")
    assert operations.set_part_seconds(part.id, 120)
    time_text[0] = "20:00"
    assert operations.refresh_meeting_window()
    updated = operations.session.schedule.pacing_window
    assert updated.start_epoch == original.start_epoch + 1800
    with pytest.raises(TimerDurationBudgetError):
        operations.set_part_seconds(part.id, 6300)
    assert part.planned_seconds == 120

    now[0] = updated.start_epoch + 60
    assert operations.start_part(part.id)
    now[0] += 60
    assert operations.stop_part(part.id)
    assert operations.reset_part(part.id)
    time_text[0] = "20:30"
    assert not operations.refresh_meeting_window()
    repository = QSettingsTimerRepository(settings)
    assert repository.load_schedule(_WEEK, MeetingType.MIDWEEK).pacing_window == updated
    operations.navigate_week("next")
    assert operations.session.schedule.pacing_window.start_epoch == updated.start_epoch + 7 * 86400 + 1800


def test_existing_timing_and_historical_over_budget_plan_are_preserved(timer_runtime):
    open_profile, _ = timer_runtime
    _, _, settings = open_profile()
    existing = build_default_schedule(_WEEK, MeetingType.MIDWEEK)
    first, second = existing.parts[:2]
    first.state = PartState.STOPPED
    first.first_started_epoch = 1000
    first.accumulated_seconds = 120
    first.run_target_seconds = 90
    second.planned_seconds = 7000
    settings.set_value(
        f"schedule/{_WEEK.isoformat()}/{existing.meeting_type.value}",
        json.dumps({**existing.to_dict(), "automatic_pacing": True}),
    )

    def load_settings():
        return ProfileMeetingSchedule(MeetingSlot.from_values("midweek", 2, "19:30"), MeetingSlot("weekend"))

    operations, _, _ = open_profile(meeting_schedule=load_settings)
    assert operations.pacing_state().mode is PacingMode.OFF
    assert operations.pacing_state().projection is None
    restored_part = operations.session.schedule.parts[0]
    assert restored_part.accumulated_seconds == 120
    assert restored_part.effective_seconds == 90
    assert operations.set_part_seconds(second.id, 6900)
    with pytest.raises(TimerDurationBudgetError):
        operations.set_part_seconds(second.id, 7000)
    assert operations.set_part_seconds(second.id, 600)
    assert operations.session.schedule.pacing_window is None


def test_custom_window_can_cross_midnight(timer_runtime):
    open_profile, _ = timer_runtime
    operations, _, _ = open_profile()
    schedule = operations.create_custom_schedule("Midnight")
    start = int(datetime(2026, 11, 24, 23, 55, tzinfo=UTC).timestamp())
    end = int(datetime(2026, 11, 25, 0, 5, tzinfo=UTC).timestamp())
    schedule = _edit(
        operations, schedule.id, "set_pacing_window", start_epoch=start, end_epoch=end,
    )
    assert schedule.pacing_window.duration_seconds == 600
    schedule = _edit(operations, schedule.id, "add_part", title="Part", planned_seconds=600)
    assert operations.select_custom_schedule(schedule.id)
    assert operations.pacing_state(start).projection.projected_end_epoch == end


def test_custom_transition_estimates_follow_part_ids_and_survive_duplication(timer_runtime):
    open_profile, _ = timer_runtime
    operations, _, _ = open_profile()
    schedule = operations.create_custom_schedule("Transitions")
    for title in ("Introduction", "Discussion"):
        schedule = _edit(operations, schedule.id, "add_part", title=title, planned_seconds=100)
    first, second = schedule.parts
    schedule = _edit(
        operations, schedule.id, "set_pacing_window", start_epoch=10_000, end_epoch=10_500,
        untracked_seconds=300,
    )
    schedule = _edit(
        operations, schedule.id, "set_pacing_transition", before_part_id=first.id, estimated_seconds=120,
    )
    schedule = _edit(
        operations, schedule.id, "set_pacing_transition", before_part_id=None, estimated_seconds=80,
    )
    schedule = _edit(operations, schedule.id, "move_part", part_id=second.id, before_id=first.id)

    reopened, _, _ = open_profile()
    restored = reopened.get_custom_schedule(schedule.id)
    assert restored == schedule
    assert dict(restored.pacing_window.transitions.before_part_seconds) == {first.id: 120}
    copy = reopened.duplicate_custom_schedule(restored.id, "Copy", restored.revision)
    assert dict(copy.pacing_window.transitions.before_part_seconds) == {copy.parts[1].id: 120}
    assert copy.pacing_window.transitions.closing_seconds == 80

    edited = _edit(reopened, restored.id, "delete_part", part_id=first.id)
    assert edited.pacing_window.transitions.before_part_seconds == ()


def test_duration_edits_must_fit_frozen_window_until_reset(timer_runtime):
    open_profile, _ = timer_runtime
    operations, _, _ = open_profile()
    schedule = operations.create_custom_schedule("Two runs")
    schedule = _edit(operations, schedule.id, "add_part", title="Opening", planned_seconds=100)
    schedule = _edit(operations, schedule.id, "add_part", title="Second", planned_seconds=100)
    first, second = schedule.parts
    schedule = _edit(
        operations, schedule.id, "set_pacing_window", start_epoch=10_000, end_epoch=10_200,
    )
    assert operations.select_custom_schedule(schedule.id)
    assert operations.start_part(first.id)
    assert operations.stop_part(first.id)
    schedule = _edit(
        operations, schedule.id, "set_pacing_window", start_epoch=10_000, end_epoch=10_900,
    )
    with pytest.raises(TimerDurationBudgetError) as error:
        operations.set_part_seconds(second.id, 800)
    assert error.value.available_seconds == 200
    assert operations.get_custom_schedule(schedule.id) == schedule
    assert operations.reset_all()
    assert operations.set_part_seconds(second.id, 800)


def test_opening_unreadable_meeting_does_not_replace_persisted_record(timer_runtime):
    open_profile, _ = timer_runtime
    _, _, settings = open_profile()
    key = f"schedule/{_WEEK.isoformat()}/midweek"
    unreadable = '{"parts": ["recoverable"'
    settings.set_value(key, unreadable)

    def load_settings():
        return ProfileMeetingSchedule(MeetingSlot.from_values("midweek", 2, "19:30"), MeetingSlot("weekend"))

    operations, _, _ = open_profile(meeting_schedule=load_settings)
    assert settings.string(key) == unreadable
    assert not operations.refresh_meeting_window()
    assert operations.pacing_state().projection is None
    assert settings.string(key) == unreadable


def test_over_budget_workbook_application_keeps_blank_meeting_unchanged(timer_runtime):
    open_profile, _ = timer_runtime
    operations, _, settings = open_profile()
    original = operations.session.schedule
    timings = (
        *(DetectedPartTiming(Section.MINISTRY, PartKind.STANDARD, 1200) for _ in range(3)),
        *(DetectedPartTiming(Section.LIVING, PartKind.STANDARD, 1200) for _ in range(2)),
    )
    changes = QSignalSpy(operations.changed)

    assert not operations.apply_detected_assignments(_WEEK, timings, AssignmentAutofillScope.VARIABLE_SECTIONS)
    assert operations.session.schedule is original
    assert original.assignment_setup is AssignmentSetup.DEFAULT
    assert QSettingsTimerRepository(settings).load_schedule(_WEEK, MeetingType.MIDWEEK) is None
    assert not changes.count()


def test_schedule_drag_moves_and_renames_persist_in_execution_order(timer_runtime):
    open_profile, _ = timer_runtime
    operations, _, _ = open_profile()
    schedule_id = operations.create_custom_schedule("Treinamento").id
    schedule = _edit(operations, schedule_id, "add_section", title="Abertura")
    opening = schedule.entries[0].id
    schedule = _edit(operations, schedule_id, "add_section", title="Atividades")
    activities = schedule.entries[1].id
    schedule = _edit(operations, schedule_id, "add_part", title="Intervalo", planned_seconds=20)
    break_id = schedule.parts[0].id
    schedule = _edit(
        operations, schedule_id, "add_part", title="Introdução", planned_seconds=120,
        section_id=opening,
    )
    intro = schedule.parts[0].id

    with pytest.raises(TimerOperationError) as blocked:
        operations.edit_custom_schedule(schedule_id, schedule.revision, "delete_section", section_id=opening)
    assert blocked.value.code == "blocked"
    with pytest.raises(TimerOperationError) as missing_anchor:
        operations.edit_custom_schedule(
            schedule_id, schedule.revision, "move_part", part_id=intro,
            section_id=activities, before_id=break_id,
        )
    assert missing_anchor.value.code == "not_found"
    assert operations.get_custom_schedule(schedule_id) == schedule

    _edit(operations, schedule_id, "move_part", part_id=break_id, section_id=opening, before_id=intro)
    _edit(operations, schedule_id, "move_part", part_id=intro, section_id=activities)
    _edit(operations, schedule_id, "move_section", section_id=activities, before_id=opening)
    _edit(operations, schedule_id, "rename_section", section_id=activities, title="Prática")
    _edit(operations, schedule_id, "rename_part", part_id=intro, title="Primeiro exercício")
    schedule = _edit(operations, schedule_id, "rename_schedule", title="    Turma A  ")
    assert operations.select_custom_schedule(schedule_id)

    restored, _, _ = open_profile()
    assert restored.session.schedule == schedule
    assert schedule.title == "Turma A"
    assert [(part.id, part.section_id) for part in schedule.parts] == [
        (intro, activities), (break_id, opening),
    ]
    assert schedule.entries[0].title == "Prática"
    assert schedule.parts[0].title == "Primeiro exercício"


def test_timed_results_survive_restart_and_duplication_starts_clean(timer_runtime):
    open_profile, now = timer_runtime
    operations, _, _ = open_profile()
    schedule_id = operations.create_custom_schedule("Turma A").id
    schedule = _edit(operations, schedule_id, "add_part", title="Intervalo", planned_seconds=10)
    part_id = schedule.parts[0].id
    assert operations.select_custom_schedule(schedule_id)
    assert operations.start_part(part_id)
    now[0] += 12

    restored, _, _ = open_profile()
    assert restored.engine.snapshot().active_part_id == part_id
    assert restored.engine.snapshot().overrun
    with pytest.raises(TimerOperationError) as blocked_switch:
        restored.select_meeting_schedule()
    assert blocked_switch.value.code == "blocked"
    running = restored.get_custom_schedule(schedule_id)
    with pytest.raises(TimerOperationError) as blocked_edit:
        restored.edit_custom_schedule(schedule_id, running.revision, "delete_part", part_id=part_id)
    assert blocked_edit.value.code == "blocked"
    assert restored.stop_part(part_id)
    assert restored.engine.snapshot().elapsed_seconds == 12

    persisted, _, _ = open_profile()
    original = persisted.get_custom_schedule(schedule_id)
    assert original.parts[0].state is PartState.STOPPED
    assert original.parts[0].accumulated_seconds == 12
    duplicate = persisted.duplicate_custom_schedule(schedule_id, "Turma B", original.revision)
    assert duplicate.id != original.id
    assert duplicate.parts[0].id != part_id
    assert duplicate.parts[0].planned_seconds == 10
    assert duplicate.parts[0].state is PartState.IDLE
    assert persisted.get_custom_schedule(schedule_id) == original
    assert persisted.reset_all()
    reset = persisted.get_custom_schedule(schedule_id)
    assert reset.parts[0].state is PartState.IDLE
    assert reset.parts[0].first_started_epoch is None
    assert reset.parts[0].accumulated_seconds == 0
    assert reset.parts[0].planned_seconds == 10
    assert persisted.select_meeting_schedule()
    reopened, _, _ = open_profile()
    assert not isinstance(reopened.session.schedule, CustomTimerSchedule)

    another_profile, _, _ = open_profile("other")
    assert another_profile.list_custom_schedules() == ()
    assert another_profile.get_custom_schedule(schedule_id) is None


def test_failed_commit_rolls_back_runtime_and_does_not_publish(timer_runtime, monkeypatch):
    open_profile, now = timer_runtime
    operations, repository, _ = open_profile()
    schedule_id = operations.create_custom_schedule("Demonstração").id
    _edit(operations, schedule_id, "add_part", title="Primeira parte", planned_seconds=40)
    schedule = _edit(operations, schedule_id, "add_part", title="Segunda parte", planned_seconds=50)
    first, second = schedule.parts
    operations.select_custom_schedule(schedule_id)
    operations.start_part(first.id)
    now[0] += 8
    committed = operations.get_custom_schedule(schedule_id)
    changes = QSignalSpy(operations.changed)

    def failed_write(_schedule):
        raise OSError("storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "save_schedule", failed_write)
        with pytest.raises(OSError, match="storage unavailable"):
            operations.start_part(second.id)
        assert not changes.count()
        assert operations.get_custom_schedule(schedule_id) == committed
        assert repository.load_schedule(schedule_id) == committed
        assert operations.engine.snapshot().active_part_id == first.id
        assert operations.engine.snapshot().elapsed_seconds == 8

    assert operations.stop_part(first.id)
    assert changes.count() == 1
    assert operations.engine.snapshot().elapsed_seconds == 8


def test_stale_edit_and_invalid_duration_leave_custom_schedule_unchanged(timer_runtime):
    open_profile, _ = timer_runtime
    operations, _, _ = open_profile()
    schedule = operations.create_custom_schedule("Escola")
    edited = _edit(operations, schedule.id, "add_part", title="Pergunta", planned_seconds=1)
    with pytest.raises(TimerOperationError) as stale:
        operations.edit_custom_schedule(schedule.id, schedule.revision, "rename_schedule", title="Outro")
    assert stale.value.code == "schedule_stale"
    with pytest.raises(TimerOperationError) as invalid:
        operations.edit_custom_schedule(
            schedule.id, edited.revision, "add_part", title="Inválida", planned_seconds=86_400,
        )
    assert invalid.value.code == "invalid"
    assert operations.get_custom_schedule(schedule.id) == edited


def test_legacy_week_keeps_progress_and_renames_without_changing_canonical_part(timer_runtime):
    open_profile, now = timer_runtime
    first_runtime, _, settings = open_profile()
    meeting_type = first_runtime.session.meeting_type
    legacy = build_default_schedule(_WEEK, meeting_type)
    part = legacy.parts[1]
    part.state = PartState.RUNNING
    part.started_at_epoch = now[0] - 20
    part.first_started_epoch = part.started_at_epoch
    data = legacy.to_dict()
    del data["assignment_setup"]
    for row in data["parts"]:
        del row["title_override"]
    settings.set_value(f"schedule/{_WEEK.isoformat()}/{meeting_type.value}", json.dumps(data))

    operations, _, _ = open_profile()
    assert operations.engine.snapshot().elapsed_seconds == 20
    assert operations.rename_part(part.id, "Abertura da escola")
    assert operations.engine.snapshot().active_part_title == "Abertura da escola"
    assert operations.session.schedule.assignment_setup is AssignmentSetup.MANUAL
    assert not operations.apply_detected_assignments(
        _WEEK, (), AssignmentAutofillScope.VARIABLE_SECTIONS,
    )
    restored, _, _ = open_profile()
    restored_part = restored.session.schedule.part_by_id(part.id)
    assert restored_part.title == part.title
    assert restored_part.section == part.section
    assert restored_part.first_started_epoch == part.first_started_epoch
    assert [child.id for child in restored.session.schedule.parts] == [child.id for child in legacy.parts]
    assert restored.rename_part(part.id, None)
    assert restored.engine.snapshot().active_part_title != "Abertura da escola"


def test_meeting_count_cannot_drop_the_running_part(timer_runtime):
    open_profile, now = timer_runtime
    operations, _, _ = open_profile()
    part = operations.session.schedule.parts_in(Section.MINISTRY)[-1]
    assert operations.start_part(part.id)
    now[0] += 20

    with pytest.raises(TimerOperationError) as blocked:
        operations.set_section_count(Section.MINISTRY, 1)
    assert blocked.value.code == "blocked"
    assert operations.engine.snapshot().active_part_id == part.id
    assert operations.engine.snapshot().elapsed_seconds == 20
    restored, _, _ = open_profile()
    assert restored.engine.snapshot().active_part_id == part.id


def test_switching_context_never_leaves_a_running_meeting_behind(timer_runtime):
    open_profile, now = timer_runtime
    operations, _, _ = open_profile()
    meeting_part = operations.session.schedule.parts[0]
    custom_id = operations.create_custom_schedule("Escola").id
    assert operations.start_part(meeting_part.id)
    now[0] += 8

    for change in (
        lambda: operations.navigate_week("next"),
        lambda: operations.set_meeting_type(MeetingType.WEEKEND),
        lambda: operations.select_custom_schedule(custom_id),
    ):
        with pytest.raises(TimerOperationError) as blocked:
            change()
        assert blocked.value.code == "blocked"
        assert operations.session.week_monday == _WEEK
        assert operations.engine.snapshot().active_part_id == meeting_part.id
    assert operations.stop_part(meeting_part.id)
    assert operations.select_custom_schedule(custom_id)
    assert operations.select_meeting_schedule()
    assert operations.session.schedule.parts[0].state is PartState.STOPPED


def test_failed_restore_does_not_replace_an_explicitly_used_meeting_on_next_launch(
    timer_runtime, monkeypatch,
):
    open_profile, _ = timer_runtime
    operations, _, _ = open_profile()
    custom = operations.create_custom_schedule("Treinamento")
    operations.select_custom_schedule(custom.id)

    def unreadable_schedule(_repository, _schedule_id):
        raise PermissionError("profile storage temporarily unreadable")

    with monkeypatch.context() as patch:
        patch.setattr(JsonCustomTimerRepository, "load_schedule", unreadable_schedule)
        fallback, repository, _ = open_profile()
    assert repository.load_selection() == custom.id
    assert not isinstance(fallback.session.schedule, CustomTimerSchedule)

    part_id = fallback.session.schedule.parts[0].id
    assert fallback.start_part(part_id)
    assert repository.load_selection() is None
    restored, _, _ = open_profile()
    assert restored.engine.snapshot().active_part_id == part_id
    assert restored.get_custom_schedule(custom.id) == custom


def test_failed_selection_write_keeps_custom_schedule_active(timer_runtime, monkeypatch):
    open_profile, _ = timer_runtime
    operations, repository, _ = open_profile()
    custom = operations.create_custom_schedule("Treinamento")
    operations.select_custom_schedule(custom.id)
    changes = QSignalSpy(operations.changed)

    def failed_write(_selected_id):
        raise OSError("selection cannot be saved")

    monkeypatch.setattr(repository, "save_selection", failed_write)
    with pytest.raises(OSError):
        operations.select_meeting_schedule()
    assert operations.session.schedule == operations.engine.schedule == custom
    assert repository.load_selection() == custom.id
    assert changes.count() == 0


def test_restarting_a_stopped_part_replaces_its_recorded_start(timer_runtime):
    open_profile, now = timer_runtime
    operations, _, _ = open_profile()
    custom = operations.create_custom_schedule("Treinamento")
    custom = _edit(operations, custom.id, "add_part", title="Intervalo", planned_seconds=10)
    operations.select_custom_schedule(custom.id)
    part_id = custom.parts[0].id
    assert operations.start_part(part_id)
    now[0] += 6
    assert operations.stop_part(part_id)

    now[0] += 30
    restarted_at = now[0]
    assert operations.start_part(part_id)
    now[0] += 8
    assert operations.stop_part(part_id)
    restored, _, _ = open_profile()
    part = restored.session.schedule.part_by_id(part_id)
    assert part.first_started_epoch == restarted_at
    assert part.accumulated_seconds == 8
