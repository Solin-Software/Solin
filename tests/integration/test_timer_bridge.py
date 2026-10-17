from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtTest import QSignalSpy

from solin.controllers.timer_engine import TimerEngine
from solin.core.foundation.settings_store import SettingsStore
from solin.core.i18n.date import format_time_with_seconds
from solin.core.timer.application import TimerSession
from solin.core.timer.infrastructure import QSettingsTimerRepository
from solin.core.timer.models import Direction, MeetingType, PartState, Section
from solin.ui.qml.timer_bridge import TimerBridge
from solin.ui.qml.timer_output import ClockRenderBridge


_WEEK = date(2026, 10, 12)


class _Automation(QObject):
    state_changed = Signal()


@pytest.fixture
def timer_context(tmp_path, monkeypatch, qt_object_owner):
    clock = SimpleNamespace(now=1_000.0)
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
    bridge = TimerBridge(
        engine=engine, session=session, output=None, monitors=None, pdf_export=None,
        media_countdown_automation=automation, parent=qt_object_owner,
    )
    return SimpleNamespace(
        clock=clock, repository=repository, session=session, engine=engine, bridge=bridge,
    )


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
