from __future__ import annotations

from dataclasses import replace
import uuid
import threading

import pytest

from solin.core.remote_control.command_gate import CommandDisposition
from solin.core.remote_control.features import TimerRemoteFeatureFlags
from solin.core.timer.models import (
    MAX_PLANNED_PART_SECONDS,
    MAX_SECTION_PART_COUNT,
    MIN_PLANNED_PART_SECONDS,
    MIN_SECTION_PART_COUNT,
)
from solin.core.remote_control.timer.contracts import (
    CreateCustomScheduleCommand,
    EditCustomScheduleCommand,
    DeleteCustomScheduleCommand,
    DuplicateCustomScheduleCommand,
    SelectCustomScheduleCommand,
    SelectMeetingScheduleCommand,
    RenamePartCommand,
    ResetAllCommand,
    CustomSchedulePartWireItem,
    CustomScheduleSectionWireItem,
    CustomScheduleWireItem,
    AdjustPartCommand,
    NavigateWeekCommand,
    ResetPartCommand,
    SetMeetingTypeCommand,
    SetPartSecondsCommand,
    SetSectionCountCommand,
    StartPartCommand,
    StopPartCommand,
    TimerCommandError,
    TimerCommandErrorCode,
    TimerControlCapabilities,
    TimerLiveWireSnapshot,
    TimerPartWireItem,
    TimerScheduleWireSnapshot,
    TimerWeekTarget,
    parse_timer_command,
)
from solin.core.remote_control.timer.state import (
    RemoteTimerStateStore,
    TimerCommandSession,
)
from solin.core.timer.models import Direction, MeetingType, PartState, Section


def test_custom_commands_round_trip_and_validate_action_arguments() -> None:
    cid = str(uuid.uuid4())
    commands = [
        CreateCustomScheduleCommand(cid, 2, "Event"),
        EditCustomScheduleCommand(cid, 2, "s", 3, "add_part", title="Part", planned_seconds=1),
        EditCustomScheduleCommand(cid, 2, "s", 3, "move_part", part_id="p", before_id="q"),
        DeleteCustomScheduleCommand(cid, 2, "s", 3),
        DuplicateCustomScheduleCommand(cid, 2, "s", 3, "Copy"),
        SelectCustomScheduleCommand(cid, 2, "s", 3),
        SelectMeetingScheduleCommand(cid, 2),
        RenamePartCommand(cid, 2, "p", None),
        ResetAllCommand(cid, 2),
        SetPartSecondsCommand(cid, 2, "p", 1),
    ]
    for command in commands:
        assert parse_timer_command(command.to_dict()) == command
    with pytest.raises(ValueError):
        EditCustomScheduleCommand(cid, 2, "s", 3, "delete_part", part_id="p", title="extra")
    with pytest.raises(ValueError):
        EditCustomScheduleCommand(cid, 2, "s", 3, "move_section", section_id=None)
    with pytest.raises((TypeError, ValueError)):
        parse_timer_command(
            {**commands[1].to_dict(), "values": {"title": "x", "planned_seconds": True}}
        )


def test_custom_snapshot_keeps_empty_sections_progress_and_policy() -> None:
    part = CustomSchedulePartWireItem("p", "Part", 1, PartState.RUNNING, 12.0, 100.0, 120.0)
    custom = CustomScheduleWireItem(
        "s", "Event", 4, (CustomScheduleSectionWireItem("empty", "Empty", ()), part)
    )
    state = RemoteTimerStateStore(
        schedule=replace(
            TimerScheduleWireSnapshot.empty(),
            custom_schedule=custom,
        )
    )
    cid = str(uuid.uuid4())
    assert (
        state.validate_command_preconditions(StartPartCommand(cid, 0, "p")).code
        is TimerCommandErrorCode.BLOCKED
    )
    assert state.schedule.to_dict()["capabilities"]["manageCustomSchedules"] is False
    assert "customSchedule" not in state.schedule.to_dict()
    assert custom.to_dict()["entries"][0]["parts"] == []
    assert custom.to_dict()["entries"][1]["startedAtEpoch"] == 120.0
    with pytest.raises(ValueError):
        CustomSchedulePartWireItem("p", "Part", 1, PartState.RUNNING)


def test_custom_command_gate_checks_revision_and_replays_results() -> None:
    caps = TimerControlCapabilities.from_feature_flags(
        TimerRemoteFeatureFlags(allow_custom_schedules=True)
    )
    state = RemoteTimerStateStore(schedule=TimerScheduleWireSnapshot.empty(capabilities=caps))
    session = TimerCommandSession(state)
    command = CreateCustomScheduleCommand(str(uuid.uuid4()), 0, "Event")
    assert session.prepare(command).should_execute
    result = session.complete_success(command)
    state.update_schedule(replace(state.schedule, week_label="changed"))
    assert session.prepare(command).result == result
    assert (
        session.prepare(replace(command, title="Different")).result.error.code
        is TimerCommandErrorCode.COMMAND_ID_CONFLICT
    )
    stale = session.prepare(replace(command, command_id=str(uuid.uuid4())))
    assert stale.result.error.code is TimerCommandErrorCode.SCHEDULE_STALE


def test_atomic_snapshots_preserve_revisions_and_validate_before_publication() -> None:
    state = RemoteTimerStateStore()
    schedule = replace(state.schedule, week_label="context")
    live = replace(state.live, active_part_title="context")
    published = state.update_snapshots(schedule, live)
    assert (
        published.schedule_revision,
        published.live_revision,
        published.public_live_revision,
    ) == (1, 1, 1)
    assert state.update_snapshots(schedule, live) == published
    with pytest.raises(TypeError):
        state.update_snapshots(schedule, None)
    assert state.snapshot() == published
    changed = state.update_snapshots(replace(schedule, week_label="other"), live)
    assert changed.schedule_revision == 2
    assert changed.live_revision == published.live_revision
    assert changed.public_live_revision == published.public_live_revision


def test_atomic_snapshots_never_expose_mixed_contexts() -> None:
    caps = TimerControlCapabilities(can_manage_custom_schedules=True)
    state = RemoteTimerStateStore(schedule=TimerScheduleWireSnapshot.empty(capabilities=caps))
    barrier = threading.Barrier(2)
    done = threading.Event()
    failures = []

    def writer() -> None:
        barrier.wait()
        for index in range(500):
            label = str(index % 2)
            schedule = replace(state.schedule, week_label=label)
            live = replace(state.live, active_part_title=label)
            state.update_snapshots(schedule, live)
        done.set()

    state.update_snapshots(
        replace(state.schedule, week_label="0"),
        replace(state.live, active_part_title="0"),
    )
    thread = threading.Thread(target=writer)
    thread.start()
    barrier.wait()
    while True:
        snapshot = state.snapshot()
        if snapshot.schedule.week_label != snapshot.live.active_part_title:
            failures.append(snapshot)
            break
        if done.is_set():
            break
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures


def test_custom_section_identity_serializes_without_enum_coercion() -> None:
    for section in (None, "", "custom-section"):
        assert (
            replace(_part(), section=section, section_key=section or "").to_dict()["section"]
            == section
        )
        live = TimerLiveWireSnapshot.idle(server_epoch_ms=1)
        assert replace(live, active_section=section).to_dict()["activeSection"] == section


def _part(part_id: str = "part-1", *, display_number: int = 1) -> TimerPartWireItem:
    return TimerPartWireItem(
        id=part_id,
        display_number=display_number,
        section=Section.MINISTRY,
        section_key="ministry",
        section_header_title="Apply Yourself to the Field Ministry",
        title="Starting a Conversation",
        planned_seconds=180,
        planned_label="03:00",
        state=PartState.IDLE,
        first_of_section=True,
        show_section_header=True,
        configurable_count=True,
        section_count=3,
        section_total_label="12 min",
    )


def test_parse_timer_command_round_trips_all_supported_command_types() -> None:
    cid = str(uuid.uuid4())
    cases = [
        StartPartCommand(cid, schedule_revision=2, part_id="part-1"),
        StopPartCommand(cid, part_id="part-1"),
        StopPartCommand(cid, part_id=None),
        ResetPartCommand(cid, schedule_revision=2, part_id="part-1"),
        AdjustPartCommand(cid, schedule_revision=2, part_id="part-1", delta_seconds=-1),
        AdjustPartCommand(cid, schedule_revision=2, part_id="part-1", delta_seconds=60),
        SetPartSecondsCommand(
            cid,
            schedule_revision=2,
            part_id="part-1",
            planned_seconds=MIN_PLANNED_PART_SECONDS,
        ),
        SetPartSecondsCommand(
            cid,
            schedule_revision=2,
            part_id="part-1",
            planned_seconds=MAX_PLANNED_PART_SECONDS,
        ),
        SetSectionCountCommand(
            cid,
            schedule_revision=2,
            section=Section.MINISTRY,
            count=MIN_SECTION_PART_COUNT,
        ),
        SetSectionCountCommand(
            cid,
            schedule_revision=2,
            section=Section.LIVING,
            count=MAX_SECTION_PART_COUNT,
        ),
        SetMeetingTypeCommand(cid, meeting_type=MeetingType.WEEKEND),
        NavigateWeekCommand(cid, target=TimerWeekTarget.NEXT),
    ]

    for expected in cases:
        parsed = parse_timer_command(expected.to_dict())
        assert parsed == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"commandId": "not-a-uuid", "type": "stop_part"},
        {"commandId": str(uuid.uuid4()), "type": "unknown"},
        {
            "commandId": str(uuid.uuid4()),
            "type": "start_part",
            "scheduleRevision": 0,
            "partId": "part-1",
            "unexpected": True,
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "adjust_part",
            "scheduleRevision": 0,
            "partId": "part-1",
            "deltaSeconds": 5,
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "set_part_seconds",
            "scheduleRevision": 0,
            "partId": "part-1",
            "plannedSeconds": 0,
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "set_part_seconds",
            "scheduleRevision": 0,
            "partId": "part-1",
            "plannedSeconds": MAX_PLANNED_PART_SECONDS + 1,
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "set_section_count",
            "scheduleRevision": 0,
            "section": "treasures",
            "count": 2,
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "set_section_count",
            "scheduleRevision": 0,
            "section": "ministry",
            "count": 0,
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "set_section_count",
            "scheduleRevision": 0,
            "section": "ministry",
            "count": MAX_SECTION_PART_COUNT + 1,
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "set_meeting_type",
            "meetingType": "convention",
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "navigate_week",
            "target": "tomorrow",
        },
        {
            "commandId": str(uuid.uuid4()),
            "type": "start_part",
            "scheduleRevision": True,
            "partId": "part-1",
        },
    ],
)
def test_parse_timer_command_rejects_malformed_or_out_of_bounds_envelopes(
    payload: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_timer_command(payload)


def test_live_wire_snapshot_rebasing_and_comparable_key_ignore_pure_wall_clock_progression() -> (
    None
):
    running = TimerLiveWireSnapshot(
        live_revision=1,
        server_epoch_ms=1_721_130_000_000,
        utc_offset_minutes=-180,
        active=True,
        state=PartState.RUNNING,
        direction=Direction.DOWN,
        active_part_id="part-1",
        active_part_title="Opening Comments",
        active_section=Section.OPENING_COMMENTS,
        planned_seconds=60,
        elapsed_seconds=10.0,
    )

    rebased = running.rebased_at(1_721_130_005_250)
    assert rebased.live_revision == 1
    assert rebased.server_epoch_ms == 1_721_130_005_250
    assert rebased.elapsed_seconds == pytest.approx(15.25)
    assert rebased.comparable_key() == running.comparable_key()

    nudged = replace(rebased, elapsed_seconds=rebased.elapsed_seconds + 1.0)
    assert nudged.comparable_key() != running.comparable_key()


def test_state_store_deduplicates_pure_tick_updates_and_revises_on_anchor_shift() -> None:
    store = RemoteTimerStateStore(
        live=TimerLiveWireSnapshot.idle(server_epoch_ms=1_721_130_000_000),
    )
    assert store.live_revision == 0

    started = TimerLiveWireSnapshot(
        live_revision=0,
        server_epoch_ms=1_721_130_001_000,
        utc_offset_minutes=0,
        active=True,
        state=PartState.RUNNING,
        active_part_id="part-1",
        active_part_title="Opening Comments",
        planned_seconds=60,
        elapsed_seconds=0.0,
    )
    published = store.update_live(started)
    assert published.live_revision == 1

    tick_later = TimerLiveWireSnapshot(
        live_revision=0,
        server_epoch_ms=1_721_130_001_250,
        utc_offset_minutes=0,
        active=True,
        state=PartState.RUNNING,
        active_part_id="part-1",
        active_part_title="Opening Comments",
        planned_seconds=60,
        elapsed_seconds=0.25,
    )
    assert store.update_live(tick_later).live_revision == 1
    assert store.live_revision == 1

    nudged = replace(tick_later, elapsed_seconds=1.25)
    assert store.update_live(nudged).live_revision == 2


def test_schedule_snapshot_rejects_duplicate_part_ids_and_non_iso_week() -> None:
    with pytest.raises(ValueError, match="unique"):
        TimerScheduleWireSnapshot(
            schedule_revision=0,
            week_monday="2026-07-13",
            week_label="13-19 Jul",
            is_current_week=True,
            meeting_type=MeetingType.MIDWEEK,
            parts=(_part("dup", display_number=1), _part("dup", display_number=2)),
        )

    with pytest.raises(ValueError, match="ISO"):
        TimerScheduleWireSnapshot.empty(week_monday="13/07/2026")


def test_feature_flags_govern_capabilities_and_server_preconditions() -> None:
    restricted_caps = TimerControlCapabilities.from_feature_flags(
        TimerRemoteFeatureFlags(allow_schedule_structure_edits=False)
    )
    store = RemoteTimerStateStore(
        schedule=TimerScheduleWireSnapshot.empty(capabilities=restricted_caps)
    )
    rev = store.schedule_revision

    assert (
        store.validate_command_preconditions(
            StartPartCommand(str(uuid.uuid4()), schedule_revision=rev, part_id="part-1")
        )
        is None
    )
    assert (
        store.validate_command_preconditions(
            AdjustPartCommand(
                str(uuid.uuid4()),
                schedule_revision=rev,
                part_id="part-1",
                delta_seconds=60,
            )
        )
        is None
    )
    assert (
        store.validate_command_preconditions(
            SetMeetingTypeCommand(str(uuid.uuid4()), meeting_type=MeetingType.WEEKEND)
        )
        is None
    )

    for blocked_cmd in (
        SetPartSecondsCommand(
            str(uuid.uuid4()),
            schedule_revision=rev,
            part_id="part-1",
            planned_seconds=120,
        ),
        SetSectionCountCommand(
            str(uuid.uuid4()),
            schedule_revision=rev,
            section=Section.MINISTRY,
            count=4,
        ),
        NavigateWeekCommand(str(uuid.uuid4()), target=TimerWeekTarget.PREVIOUS),
    ):
        err = store.validate_command_preconditions(blocked_cmd)
        assert err is not None
        assert err.code is TimerCommandErrorCode.BLOCKED


def test_timer_command_session_enforces_idempotency_conflict_in_progress_and_stale_schedule() -> (
    None
):
    store = RemoteTimerStateStore()
    session = TimerCommandSession(store, deduplication_limit=4)

    cmd_id = str(uuid.uuid4())
    cmd = StartPartCommand(cmd_id, schedule_revision=0, part_id="part-1")
    first = session.prepare(cmd)
    assert first.disposition is CommandDisposition.ACCEPTED
    assert first.should_execute is True

    in_flight = session.prepare(cmd)
    assert in_flight.disposition is CommandDisposition.IN_PROGRESS
    assert in_flight.result is not None
    assert in_flight.result.error is not None
    assert in_flight.result.error.code is TimerCommandErrorCode.COMMAND_IN_PROGRESS

    conflicting = StopPartCommand(cmd_id, part_id="part-1")
    conflict = session.prepare(conflicting)
    assert conflict.disposition is CommandDisposition.REJECTED
    assert conflict.result is not None
    assert conflict.result.error is not None
    assert conflict.result.error.code is TimerCommandErrorCode.COMMAND_ID_CONFLICT

    completed = session.complete_success(cmd)
    assert completed.ok is True

    replay = session.prepare(cmd)
    assert replay.disposition is CommandDisposition.REPLAYED
    assert replay.result == completed

    store.update_schedule(
        TimerScheduleWireSnapshot(
            schedule_revision=0,
            week_monday="2026-07-13",
            week_label="13-19 Jul",
            is_current_week=True,
            meeting_type=MeetingType.MIDWEEK,
            parts=(_part(),),
        )
    )
    assert store.schedule_revision == 1

    stale = session.prepare(
        ResetPartCommand(str(uuid.uuid4()), schedule_revision=0, part_id="part-1")
    )
    assert stale.should_execute is False
    assert stale.result is not None
    assert stale.result.error is not None
    assert stale.result.error.code is TimerCommandErrorCode.SCHEDULE_STALE
    assert stale.result.error.retryable is True

    failed_cmd = StopPartCommand(str(uuid.uuid4()), part_id="part-1")
    assert session.prepare(failed_cmd).should_execute is True
    failed_res = session.complete_failure(
        failed_cmd,
        TimerCommandError(TimerCommandErrorCode.BLOCKED, "Not running."),
    )
    assert failed_res.ok is False
    assert session.prepare(failed_cmd).result == failed_res


def test_part_wire_item_exposes_and_validates_duration_editable() -> None:
    editable = _part()
    assert editable.duration_editable is True
    assert editable.to_dict()["durationEditable"] is True

    locked = replace(editable, duration_editable=False)
    assert locked.to_dict()["durationEditable"] is False

    with pytest.raises(TypeError, match="duration_editable"):
        replace(editable, duration_editable="no")  # type: ignore[arg-type]
