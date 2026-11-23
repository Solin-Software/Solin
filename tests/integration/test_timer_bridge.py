from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtTest import QSignalSpy

from solin.controllers.timer_engine import TimerEngine
from solin.controllers.timer_hand_alert_controller import TimerHandAlertController
from solin.core.foundation.settings_store import SettingsStore
from solin.core.i18n.date import format_time_with_seconds
from solin.core.integrations.automation.settings import ZoomSettingsStore
from solin.core.timer.application import TimerSession
from solin.core.timer.infrastructure import QSettingsTimerRepository
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
    session = TimerSession(repository, _WEEK)
    engine = TimerEngine(qt_object_owner)
    automation = _Automation(qt_object_owner)
    hand_alert = TimerHandAlertController(
        ZoomSettingsStore(SettingsStore.for_namespace("test", "prefs")), qt_object_owner,
    )
    lookup = _Lookup(qt_object_owner)
    language = _Language(qt_object_owner)
    bridge = TimerBridge(
        engine=engine, session=session, output=None, monitors=None, pdf_export=None,
        hand_alert=hand_alert, media_countdown_automation=automation, parent=qt_object_owner,
        language_manager=language, workbook_lookup=lookup,
    )
    return SimpleNamespace(
        clock=clock, repository=repository, session=session, engine=engine, bridge=bridge,
        hand_alert=hand_alert, lookup=lookup, language=language,
    )


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


@pytest.mark.parametrize("action", ["duration", "count", "start-reset", "inline-edit"])
def test_stale_lookup_never_replaces_in_use_assignments(timer_context, action):
    ctx = timer_context
    initial = _show_assignments(ctx)
    part = ctx.session.schedule.parts_in(Section.MINISTRY)[0]
    if action == "duration":
        ctx.bridge.setPartSeconds(part.id, 420)
    elif action == "count":
        ctx.bridge.setSectionCount("ministry", 4)
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
