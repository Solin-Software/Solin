from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtTest import QSignalSpy

from solin.controllers.timer_engine import TimerEngine
from solin.controllers.timer_operations import TimerOperations
from solin.controllers.timer_pdf_export_controller import TimerPdfExportController
from solin.controllers.timer_hand_alert_controller import TimerHandAlertController
from solin.core.foundation.settings_store import SettingsStore
from solin.core.i18n.date import format_time_with_seconds
from solin.core.integrations.automation.settings import ZoomSettingsStore
from solin.core.timer.application import TimerSession
from solin.core.timer.infrastructure import JsonCustomTimerRepository, QSettingsTimerRepository
from solin.core.meetings.workbook import Workbook, WorkbookPart
from solin.core.timer.models import AssignmentSetup, Direction, MeetingType, PartState, Section
from solin.ui.qml.timer_bridge import TimerBridge
from solin.ui.qml.timer_output import ClockRenderBridge


_WEEK = date(2026, 10, 12)


class _Automation(QObject):
    state_changed = Signal()


class _Lookup(QObject):
    completed = Signal(object, object)

    def __init__(self, parent):
        super().__init__(parent)
        self.requests = []
        self.invalidations = 0
        self.closed = False

    def request(self, generation, monday, code):
        self.requests.append((generation, monday, code))

    def invalidate(self):
        self.invalidations += 1

    def shutdown(self):
        self.closed = True


class _Language(QObject):
    language_changed = Signal(str)
    media_language_changed = Signal(str)
    media_api_code = "T"
    api_code = "E"

    @property
    def jw_lang_service(self):
        return self


@pytest.fixture
def timer_context(tmp_path, monkeypatch, qt_object_owner):
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr("solin.ui.qml.timer_bridge.current_monday", lambda: _WEEK)
    monkeypatch.setattr("solin.core.timer.models._now", lambda: clock.now)
    monkeypatch.setattr("solin.core.timer.state_machine._now", lambda: clock.now)
    settings_path = str(tmp_path / "timer.ini")
    monkeypatch.setattr(
        SettingsStore, "_settings",
        lambda _: QSettings(settings_path, QSettings.Format.IniFormat),
    )
    repository = QSettingsTimerRepository(SettingsStore.for_namespace("test", "timer"))
    custom = JsonCustomTimerRepository(tmp_path / "timer_schedules")
    session = TimerSession(repository, _WEEK, custom)
    engine = TimerEngine(qt_object_owner)
    automation = _Automation(qt_object_owner)
    hand_alert = TimerHandAlertController(
        ZoomSettingsStore(SettingsStore.for_namespace("test", "prefs")), qt_object_owner,
    )
    lookup = _Lookup(qt_object_owner)
    language = _Language(qt_object_owner)
    notifications = []
    bridge = TimerBridge(
        operations=TimerOperations(engine, session, parent=qt_object_owner),
        output=None, monitors=None, pdf_export=None,
        hand_alert=hand_alert, media_countdown_automation=automation, parent=qt_object_owner,
        language_manager=language, workbook_lookup=lookup,
        notify_error=notifications.append,
    )
    return SimpleNamespace(
        clock=clock, repository=repository, session=session, engine=engine, bridge=bridge,
        hand_alert=hand_alert, lookup=lookup, language=language, notifications=notifications,
    )


def test_custom_catalog_initial_read_failure_can_recover(timer_context, monkeypatch, qt_object_owner):
    ctx = timer_context
    operations = ctx.bridge.operations
    list_schedules = operations.list_custom_schedules

    def fail_read():
        raise OSError("catalog unavailable")

    monkeypatch.setattr(operations, "list_custom_schedules", fail_read)
    bridge = TimerBridge(
        operations=operations, output=None, monitors=None, pdf_export=None,
        hand_alert=ctx.hand_alert, media_countdown_automation=ctx.bridge._media_countdown_automation,
        parent=qt_object_owner,
    )
    assert not bridge.customCatalogAvailable
    assert bridge.customSchedules == []

    monkeypatch.setattr(operations, "list_custom_schedules", list_schedules)
    assert bridge.refreshCustomCatalog()
    assert bridge.customCatalogAvailable
    assert bridge.customSchedules == []


def test_live_controls_update_the_active_catalog_row_without_reloading(timer_context, monkeypatch):
    ctx = timer_context
    operations = ctx.bridge.operations
    schedule = operations.create_custom_schedule("Training", first_part_title="Talk", first_part_seconds=90, select=True)
    part_id = schedule.parts[0].id
    reads = []
    list_schedules = operations.list_custom_schedules

    def counted_read():
        reads.append(True)
        return list_schedules()

    monkeypatch.setattr(operations, "list_custom_schedules", counted_read)
    structure_updates = QSignalSpy(ctx.bridge.scheduleChanged)
    live_updates = QSignalSpy(ctx.bridge.liveStateChanged)
    assert operations.start_part(part_id)
    assert ctx.bridge.customSchedules[0]["hasTimingData"]
    structure_before_tick = structure_updates.count()
    live_before_tick = live_updates.count()
    ctx.clock.now += 5
    ctx.engine.tick.emit(ctx.engine.snapshot().to_dict())
    assert live_updates.count() == live_before_tick + 1
    assert structure_updates.count() == structure_before_tick
    assert operations.stop_part()
    assert operations.reset_all()
    assert not ctx.bridge.customSchedules[0]["hasTimingData"]
    assert ctx.bridge.customSchedules[0]["revision"] == ctx.session.schedule.revision
    assert not reads


def test_timer_operation_errors_use_central_notifications_outside_dialogs(timer_context, monkeypatch):
    ctx = timer_context

    def fail_save(_schedule):
        raise OSError("disk error")

    part = ctx.session.schedule.parts[0]
    monkeypatch.setattr(ctx.repository, "save_schedule", fail_save)
    ctx.bridge.startPart(part.id)
    assert ctx.notifications == [ctx.bridge.feedback["message"]]
    assert not ctx.session.schedule.has_running_part()

    binding = ctx.bridge.scheduleSummary
    assert not ctx.bridge.savePartEdit(
        binding["scheduleId"], binding["scheduleRevision"], part.id, "New title", -1,
    )
    assert ctx.bridge.feedback["scope"] == f"part:{part.id}"
    assert len(ctx.notifications) == 1


@pytest.mark.parametrize("fail", [False, True])
def test_timer_pdf_reports_export_outcome_through_central_notifications(timer_context, tmp_path, monkeypatch, fail):
    from solin.controllers import timer_pdf_export_controller as export_module

    output = tmp_path / "report.pdf"
    notifications = []
    notifications_center = SimpleNamespace(
        error=lambda message, **_: notifications.append(("error", message)),
        success=lambda message, **_: notifications.append(("success", message)),
    )

    def export(_schedule, path, **_kwargs):
        if fail:
            raise OSError("disk error")
        with open(path, "wb") as report:
            report.write(b"%PDF")

    monkeypatch.setattr(export_module.QFileDialog, "getSaveFileName", lambda *_: (str(output), ""))
    monkeypatch.setattr(export_module, "export_schedule_pdf", export)
    TimerPdfExportController(None, notifications_center).export(
        timer_context.session.schedule,
        week_label="Week",
        meeting_type_label="Midweek",
        date_format="dd/MM/yyyy",
        time_format="HH:mm:ss",
    )

    assert [kind for kind, _ in notifications] == ["error" if fail else "success"]
    assert output.exists() is not fail


def test_settings_feedback_survives_unrelated_changes_and_clears_on_retry(timer_context, monkeypatch):
    ctx = timer_context
    save_clock = ctx.repository.save_clock_config
    save_pacing = ctx.repository.save_pacing_mode

    def fail_save(_value):
        raise OSError("settings unavailable")

    monkeypatch.setattr(ctx.repository, "save_clock_config", fail_save)
    show_seconds = not ctx.bridge.clockConfig["show_seconds"]
    ctx.bridge.updateClock("show_seconds", show_seconds)
    clock_error = ctx.bridge.feedback
    assert clock_error["scope"] == "settings"
    assert clock_error["code"] == "failed"
    assert ctx.bridge.setPacingMode("monitor")
    assert ctx.bridge.feedback == clock_error

    monkeypatch.setattr(ctx.repository, "save_clock_config", save_clock)
    ctx.bridge.updateClock("show_seconds", show_seconds)
    assert ctx.bridge.clockConfig["show_seconds"] == show_seconds
    assert not ctx.bridge.feedback

    monkeypatch.setattr(ctx.repository, "save_pacing_mode", fail_save)
    assert not ctx.bridge.setPacingMode("automatic")
    pacing_error = ctx.bridge.feedback
    assert pacing_error["scope"] == "settings"
    assert pacing_error["code"] == "failed"
    ctx.bridge.updateClock("show_seconds", not show_seconds)
    assert ctx.bridge.feedback == pacing_error

    monkeypatch.setattr(ctx.repository, "save_pacing_mode", save_pacing)
    assert ctx.bridge.setPacingMode("automatic")
    assert not ctx.bridge.feedback


def test_manual_hand_control_updates_the_projection_without_changing_the_clock(timer_context):
    ctx = timer_context
    output = ClockRenderBridge(ctx.engine, lambda: ctx.session.clock_config, parent=ctx.engine)
    alert = ctx.hand_alert
    alert.alert_changed.connect(output.set_hand_alert)
    clock_updates = QSignalSpy(output.modelChanged)
    alert_updates = QSignalSpy(output.handAlertChanged)

    assert not ctx.bridge.manualHandAvailable
    ctx.bridge.setManualHand(True)
    assert not ctx.bridge.manualHandRaised

    alert.set_outputs(available=True)
    assert ctx.bridge.manualHandAvailable
    ctx.bridge.setManualHand(True)
    assert ctx.bridge.manualHandRaised
    assert output.property("handAlert") == {"kind": "manual"}
    ctx.bridge.setManualHand(True)
    alert.set_outputs(available=False)
    assert not ctx.bridge.manualHandAvailable
    assert not ctx.bridge.manualHandRaised
    assert not output.property("handAlert")
    assert alert_updates.count() == 2
    assert clock_updates.count() == 0


@pytest.mark.parametrize("direction", [Direction.DOWN, Direction.UP])
def test_running_adjustment_updates_elapsed_output_and_persistence(timer_context, direction):
    ctx = timer_context
    part = ctx.session.schedule.parts_in(Section.MINISTRY)[0]
    planned = [item.planned_seconds for item in ctx.session.schedule.parts]
    ctx.engine.set_direction(direction)
    output = ClockRenderBridge(ctx.engine, lambda: ctx.session.clock_config, parent=ctx.engine)
    ctx.bridge.startPart(part.id)
    ctx.clock.now += 90.25
    states = QSignalSpy(ctx.engine.state_changed)
    ticks = QSignalSpy(ctx.engine.tick)
    renders = QSignalSpy(output.modelChanged)

    ctx.bridge.adjustPart(part.id, 1)

    snapshot = ctx.engine.snapshot()
    assert snapshot.elapsed_seconds == 91.25
    assert snapshot.remaining_seconds == part.planned_seconds - 91.25
    assert snapshot.display_seconds() == (
        91.25 if direction is Direction.UP else part.planned_seconds - 91.25
    )
    assert states.count() == ticks.count() == renders.count() == 1
    assert ctx.bridge.liveState == snapshot.to_dict()
    assert part.state is PartState.RUNNING
    assert part.first_started_epoch == 999.0
    row = next(row for row in ctx.bridge.parts if row["id"] == part.id)
    assert row["startedLabel"] == format_time_with_seconds(999.0)
    assert [item.planned_seconds for item in ctx.session.schedule.parts] == planned

    restored = ctx.repository.load_schedule(_WEEK, MeetingType.MIDWEEK)
    assert restored is not None
    restored_part = restored.part_by_id(part.id)
    assert restored_part is not None
    assert restored_part.first_started_epoch == 999.0
    assert restored_part.elapsed(ctx.clock.now + 15) == 106.25
    ctx.clock.now += 15
    ctx.bridge.stopPart(part.id)
    assert part.accumulated_seconds == 106.25


def test_adjustment_clamps_elapsed_and_keeps_idle_and_stopped_semantics(timer_context):
    ctx = timer_context
    part = ctx.session.schedule.parts_in(Section.MINISTRY)[0]
    planned = part.planned_seconds
    ctx.bridge.adjustPart(part.id, 60)
    assert part.planned_seconds == planned + 60
    ctx.bridge.startPart(part.id)
    ctx.clock.now += 0.5

    ctx.bridge.adjustPart(part.id, -1)

    assert ctx.engine.snapshot().elapsed_seconds == 0.0
    assert part.first_started_epoch == ctx.clock.now
    ctx.clock.now += 5.25
    ctx.bridge.stopPart(part.id)
    stopped = ctx.session.schedule.to_dict()
    states = QSignalSpy(ctx.engine.state_changed)
    changes = QSignalSpy(ctx.bridge.scheduleChanged)
    ctx.bridge.adjustPart(part.id, 60)
    ctx.bridge.adjustPart("missing", -60)
    assert ctx.session.schedule.to_dict() == stopped
    assert states.count() == changes.count() == 0
    assert part.accumulated_seconds == 5.25

    ctx.bridge.resetPart(part.id)
    ctx.bridge.startPart(part.id)
    assert ctx.engine.snapshot().elapsed_seconds == 0.0



_WORKBOOK = Workbook(parts=(
    WorkbookPart("ministry", "standard", 4, "Published first assignment", 180),
    WorkbookPart("ministry", "standard", 5, "Published second assignment", 300),
    WorkbookPart("living", "standard", 6, "Published discussion", 900),
    WorkbookPart("living", "cbs", 7, "Published study", 1800),
))


def _show_assignments(context):
    context.bridge.set_timer_page_visible(True)
    assert not context.lookup.requests
    context.bridge.setAssignmentsModeActive(True)
    return context.lookup.requests[-1][0]


def test_lookup_waits_for_visible_assignments_and_can_retry_on_reentry(timer_context):
    ctx = timer_context
    generation = _show_assignments(ctx)
    assert ctx.lookup.requests[-1][1:] == (_WEEK, "T")
    assert len(ctx.bridge.parts) == 11
    assert ctx.repository.load_schedule(_WEEK, MeetingType.MIDWEEK) is None

    ctx.bridge.set_timer_page_visible(False)
    ctx.bridge.set_timer_page_visible(True)
    retry = ctx.lookup.requests[-1][0]
    assert retry != generation
    ctx.lookup.completed.emit(generation, _WORKBOOK)
    assert len(ctx.bridge.parts) == 11
    ctx.lookup.completed.emit(retry, _WORKBOOK)

    assert ctx.engine.schedule is ctx.session.schedule
    assert len(ctx.bridge.parts) == 9
    assert [p.planned_seconds for p in ctx.session.schedule.parts_in(Section.MINISTRY)] == [180, 300]
    assert all("Published" not in p.title for p in ctx.session.schedule.parts)
    assert ctx.repository.load_schedule(_WEEK, MeetingType.MIDWEEK).assignment_setup is AssignmentSetup.DETECTED
    ctx.bridge.set_timer_page_visible(False)
    ctx.bridge.set_timer_page_visible(True)
    assert len(ctx.lookup.requests) == 2


@pytest.mark.parametrize("action", ["duration", "count", "rename", "start-reset", "inline-edit"])
def test_stale_lookup_never_replaces_in_use_assignments(timer_context, action):
    ctx = timer_context
    initial = _show_assignments(ctx)
    part = ctx.session.schedule.parts_in(Section.MINISTRY)[0]
    if action == "duration":
        summary = ctx.bridge.scheduleSummary
        assert ctx.bridge.savePartEdit(summary["scheduleId"], summary["scheduleRevision"], part.id, "", 420)
    elif action == "count":
        ctx.bridge.setSectionCount("ministry", 4)
    elif action == "rename":
        assert ctx.bridge.operations.rename_part(part.id, "Edited assignment")
    elif action == "start-reset":
        ctx.bridge.startPart(part.id)
        ctx.bridge.resetPart(part.id)
    else:
        ctx.bridge.deferAssignmentLookupForEdit()
    before = ctx.session.schedule.to_dict()

    ctx.lookup.completed.emit(initial, _WORKBOOK)

    assert ctx.session.schedule.to_dict() == before
    expected = AssignmentSetup.DEFAULT if action == "inline-edit" else AssignmentSetup.MANUAL
    assert ctx.session.schedule.assignment_setup is expected
    if action == "start-reset":
        assert ctx.repository.load_schedule(_WEEK, MeetingType.MIDWEEK).assignment_setup is expected


def test_inline_edit_defers_lookup_even_when_language_changes(timer_context):
    ctx = timer_context
    _show_assignments(ctx)
    ctx.bridge.deferAssignmentLookupForEdit()

    ctx.language.media_api_code = "FI"
    ctx.language.media_language_changed.emit("FI")
    assert len(ctx.lookup.requests) == 1

    ctx.bridge.set_timer_page_visible(False)
    ctx.bridge.set_timer_page_visible(True)
    assert len(ctx.lookup.requests) == 2
    assert ctx.lookup.requests[-1][1:] == (_WEEK, "FI")


def test_custom_section_palette_falls_back_for_invalid_preview(timer_context):
    bridge = timer_context.bridge
    assert bridge.customSectionPalette("invalid") == bridge.customSectionPalette("")
    assert bridge.customSectionPalette("#41b6c4") != bridge.customSectionPalette("")


def test_manually_selecting_a_different_visible_week_or_language_discards_old_document(timer_context):
    ctx = timer_context
    initial = _show_assignments(ctx)
    ctx.bridge.previousWeek()
    assert len(ctx.lookup.requests) == 1
    ctx.lookup.completed.emit(initial, _WORKBOOK)
    assert ctx.session.schedule.assignment_setup is AssignmentSetup.DEFAULT
    ctx.bridge.nextWeek()
    ctx.bridge.nextWeek()
    future = ctx.lookup.requests[-1][0]
    assert ctx.lookup.requests[-1][1:] == (_WEEK + timedelta(weeks=1), "T")

    ctx.language.media_api_code = "FI"
    ctx.language.media_language_changed.emit("FI")
    current = ctx.lookup.requests[-1][0]
    assert ctx.lookup.requests[-1][1:] == (_WEEK + timedelta(weeks=1), "FI")
    ctx.lookup.completed.emit(future, _WORKBOOK)
    assert ctx.session.schedule.assignment_setup is AssignmentSetup.DEFAULT
    ctx.lookup.completed.emit(current, _WORKBOOK)

    assert ctx.session.schedule.assignment_setup is AssignmentSetup.DETECTED
    assert ctx.repository.load_schedule(_WEEK, MeetingType.MIDWEEK) is None
    ctx.bridge.setMeetingType("weekend")
    assert ctx.session.schedule.assignment_setup is AssignmentSetup.DEFAULT
    assert ctx.lookup.requests[-1][0] == current
    ctx.bridge.shutdown_assignment_lookup()
    assert ctx.lookup.closed
