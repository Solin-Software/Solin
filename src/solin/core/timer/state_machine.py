"""Framework-independent timer state transitions."""

from __future__ import annotations

from .models import (
    Direction,
    MeetingPart,
    MeetingSchedule,
    PartState,
    TimerSnapshot,
    _now,
)


class TimerStateMachine:
    """Authoritative state for one live meeting timer.

    The state machine owns no scheduler, signals, persistence, or translation.
    Callers provide ``now`` when deterministic transition timing matters.
    """

    def __init__(self) -> None:
        self._schedule: MeetingSchedule | None = None
        self._active_part_id: str | None = None
        self._direction = Direction.DOWN

    @property
    def direction(self) -> Direction:
        return self._direction

    @property
    def schedule(self) -> MeetingSchedule | None:
        return self._schedule

    @property
    def active_part_id(self) -> str | None:
        return self._active_part_id

    def set_direction(self, direction: Direction) -> bool:
        if direction is self._direction:
            return False
        self._direction = direction
        return True

    def set_schedule(self, schedule: MeetingSchedule | None) -> None:
        self._schedule = schedule
        self._active_part_id = None
        if schedule is None:
            return
        running = next(
            (part for part in schedule.parts if part.state is PartState.RUNNING),
            None,
        )
        if running is not None:
            self._active_part_id = running.id

    def start_part(self, part_id: str, *, now: float | None = None) -> bool:
        part = self._part(part_id)
        if part is None or part.state is PartState.RUNNING:
            return False

        transition_time = _now() if now is None else now
        self._freeze_active(now=transition_time)

        if part.state is PartState.STOPPED:
            part.accumulated_seconds = 0.0
        part.started_at_epoch = transition_time
        if part.first_started_epoch is None:
            part.first_started_epoch = transition_time
        part.state = PartState.RUNNING
        self._active_part_id = part.id
        return True

    def stop_part(
        self,
        part_id: str | None = None,
        *,
        now: float | None = None,
    ) -> bool:
        part = self._part(part_id or self._active_part_id)
        if part is None or part.state is not PartState.RUNNING:
            return False
        self._freeze_part(part, now=_now() if now is None else now)
        return True

    def reset_part(self, part_id: str) -> bool:
        part = self._part(part_id)
        if part is None:
            return False
        part.state = PartState.IDLE
        part.started_at_epoch = None
        part.first_started_epoch = None
        part.accumulated_seconds = 0.0
        if self._active_part_id == part.id:
            self._active_part_id = None
        return True

    def clear_stopped_part(self, part_id: str) -> bool:
        """Stop presenting ``part_id`` while preserving its frozen result."""
        part = self._part(part_id)
        if (
            self._active_part_id != part_id
            or part is None
            or part.state is not PartState.STOPPED
        ):
            return False
        self._active_part_id = None
        return True

    def snapshot(self, *, now: float | None = None) -> TimerSnapshot:
        snapshot_time = _now() if now is None else now
        part = self._part(self._active_part_id)
        if part is None:
            return TimerSnapshot(
                active=False,
                wall_clock_epoch=snapshot_time,
                direction=self._direction,
            )

        elapsed = part.elapsed(snapshot_time)
        remaining = part.planned_seconds - elapsed
        return TimerSnapshot(
            active=True,
            wall_clock_epoch=snapshot_time,
            direction=self._direction,
            active_part_id=part.id,
            active_part_title=part.title,
            active_section=part.section,
            planned_seconds=part.planned_seconds,
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            overrun=remaining < 0,
            state=part.state,
        )

    def _part(self, part_id: str | None) -> MeetingPart | None:
        if self._schedule is None or part_id is None:
            return None
        return self._schedule.part_by_id(part_id)

    def _freeze_active(self, *, now: float) -> None:
        part = self._part(self._active_part_id)
        if part is not None and part.state is PartState.RUNNING:
            self._freeze_part(part, now=now)

    @staticmethod
    def _freeze_part(part: MeetingPart, *, now: float) -> None:
        part.accumulated_seconds = part.elapsed(now)
        part.started_at_epoch = None
        part.state = PartState.STOPPED
