from __future__ import annotations

import json
from datetime import date

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtTest import QSignalSpy

from solin.controllers.timer_engine import TimerEngine
from solin.controllers.timer_operations import TimerOperations
from solin.core.foundation.settings_store import SettingsStore
from solin.core.timer import models, state_machine
from solin.core.timer.application import AssignmentAutofillScope, TimerSession
from solin.core.timer.custom_schedule import TimerOperationError
from solin.core.timer.infrastructure import JsonCustomTimerRepository, QSettingsTimerRepository
from solin.core.timer.models import AssignmentSetup, CustomTimerSchedule, MeetingType, PartState, Section
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

    def open_profile(profile="one"):
        settings_store = SettingsStore.for_namespace(profile, "Timer")
        repository = QSettingsTimerRepository(settings_store)
        custom = JsonCustomTimerRepository(tmp_path / profile / "timer_schedules")
        session = TimerSession(repository, _WEEK, custom)
        engine = TimerEngine(qt_object_owner)
        operations = TimerOperations(engine, session, parent=qt_object_owner)
        return operations, custom, settings_store

    return open_profile, now


def _edit(operations, schedule_id, action, **values):
    schedule = operations.get_custom_schedule(schedule_id)
    assert schedule is not None
    assert operations.edit_custom_schedule(schedule_id, schedule.revision, action, **values)
    return operations.get_custom_schedule(schedule_id)


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
