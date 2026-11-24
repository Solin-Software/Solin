"""Framework-independent timer state transitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .models import (
    Direction,
    PartState,
    TimerPart,
    TimerSchedule,
    TimerSnapshot,
    _now,
)
from .pacing import (
    DEFAULT_ALLOW_PACING_EXTENSION,
    PacingSnapshot,
    effective_target_at_start,
    pacing_snapshot,
    transition_budget,
)
from .pacing_models import PacingRun


class TimerStateMachine:
    """Authoritative state for one live timer.

    The state machine owns no scheduler, signals, persistence, or translation.
    Callers provide ``now`` when deterministic transition timing matters.
    """

    def __init__(self, *, allow_pacing_extension: bool = DEFAULT_ALLOW_PACING_EXTENSION) -> None:
        self._schedule: TimerSchedule | None = None
        self._active_part_id: str | None = None
        self._direction = Direction.DOWN
        self.allow_pacing_extension = allow_pacing_extension

    @property
    def direction(self) -> Direction:
        return self._direction

    @property
    def schedule(self) -> TimerSchedule | None:
        return self._schedule

    @property
    def active_part_id(self) -> str | None:
        return self._active_part_id

    def set_direction(self, direction: Direction) -> bool:
        if direction is self._direction:
            return False
        self._direction = direction
        return True

    def set_schedule(self, schedule: TimerSchedule | None, *, preserve_stopped: bool = False) -> None:
        previous_id = self._active_part_id
        self._schedule = schedule
        self._active_part_id = None
        if schedule is None:
            return
        running = next(
            (part for part in schedule.iter_parts() if part.state is PartState.RUNNING),
            None,
        )
        if running is not None:
            self._active_part_id = running.id
        elif preserve_stopped:
            previous = self._part(previous_id)
            if previous is not None and previous.state is PartState.STOPPED:
                self._active_part_id = previous_id

    def commit_transition(
        self, transition: Callable[[], bool], save: Callable[[], None],
    ) -> bool:
        """Restore elapsed and active state if saving a transition fails."""
        active = self._active_part_id
        schedule = self._schedule
        checkpoint = [(part, replace(part)) for part in schedule.iter_parts()] if schedule else []
        run = schedule.pacing_run if schedule else None
        if not transition():
            return False
        try:
            save()
        except (OSError, RuntimeError):
            self._active_part_id = active
            if schedule is not None:
                schedule.pacing_run = run
            for part, previous in checkpoint:
                part.state = previous.state
                part.started_at_epoch = previous.started_at_epoch
                part.first_started_epoch = previous.first_started_epoch
                part.accumulated_seconds = previous.accumulated_seconds
                part.actual_started_epoch = previous.actual_started_epoch
                part.actual_stopped_epoch = previous.actual_stopped_epoch
                part.run_target_seconds = previous.run_target_seconds
            raise
        return True

    def start_part(self, part_id: str, *, now: float | None = None) -> bool:
        part = self._part(part_id)
        if part is None or part.state is PartState.RUNNING:
            return False

        transition_time = _now() if now is None else now
        self._freeze_active(now=transition_time)
        schedule = self._schedule
        assert schedule is not None
        parts = list(schedule.iter_parts())
        selected = parts.index(part)
        prior_run = schedule.pacing_run
        reached = {item.id for item in parts if item.state is not PartState.IDLE}
        if prior_run is not None:
            reached.update(item.id for item in parts if item.id in prior_run.reached_ids)
        reached.update(item.id for item in parts[:selected + 1])
        schedule.pacing_run = PacingRun(
            prior_run.window if prior_run is not None else schedule.pacing_window,
            frozenset(reached), transition_time,
        )
        part.run_target_seconds = effective_target_at_start(
            schedule, part, [item for item in parts if item.id not in reached], transition_time,
            allow_extension=self.allow_pacing_extension,
        )

        if part.state is PartState.STOPPED:
            part.accumulated_seconds = 0.0
        part.started_at_epoch = transition_time
        part.first_started_epoch = transition_time
        part.actual_started_epoch = transition_time
        part.actual_stopped_epoch = None
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

    def reset_part(self, part_id: str, *, now: float | None = None) -> bool:
        part = self._part(part_id)
        if part is None:
            return False
        if part.state is PartState.RUNNING:
            self._freeze_part(part, now=_now() if now is None else now)
        if not self._reset_part(part):
            return False
        schedule = self._schedule
        assert schedule is not None
        if (run := schedule.pacing_run) is not None:
            schedule.pacing_run = replace(
                run, reached_ids=run.reached_ids - {part_id}, completed_projection_epoch=None,
            )
        return True

    def _reset_part(self, part: TimerPart) -> bool:
        if (
            part.state is PartState.IDLE
            and part.started_at_epoch is None
            and part.first_started_epoch is None
            and part.accumulated_seconds == 0.0
        ):
            return False
        part.state = PartState.IDLE
        part.started_at_epoch = None
        part.first_started_epoch = None
        part.accumulated_seconds = 0.0
        part.actual_started_epoch = None
        part.actual_stopped_epoch = None
        part.run_target_seconds = None
        if self._active_part_id == part.id:
            self._active_part_id = None
        return True

    def reset_all(self) -> bool:
        if self._schedule is None:
            return False
        changed = False
        for part in self._schedule.iter_parts():
            changed = self._reset_part(part) or changed
        if self._schedule.pacing_run is not None:
            self._schedule.pacing_run = None
            changed = True
        return changed

    def adjust_elapsed(
        self,
        part_id: str,
        delta_seconds: int,
        *,
        now: float | None = None,
    ) -> bool:
        """Correct elapsed time and its start timestamp without changing the plan."""
        part = self._part(part_id)
        if part is None or part.state is not PartState.RUNNING or delta_seconds == 0:
            return False
        adjustment_time = _now() if now is None else now
        elapsed = part.elapsed(adjustment_time)
        adjusted = max(0.0, elapsed + delta_seconds)
        if adjusted == elapsed:
            return False
        part.accumulated_seconds = adjusted
        part.started_at_epoch = adjustment_time
        part.first_started_epoch = adjustment_time - adjusted
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
        remaining = part.effective_seconds - elapsed
        return TimerSnapshot(
            active=True,
            wall_clock_epoch=snapshot_time,
            direction=self._direction,
            active_part_id=part.id,
            active_part_title=part.title_override or part.title,
            active_section=part.section,
            planned_seconds=part.effective_seconds,
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            overrun=remaining < 0,
            state=part.state,
        )

    def pacing_snapshot(self, now: float | None = None) -> PacingSnapshot | None:
        if self._schedule is None:
            return None
        return pacing_snapshot(
            self._schedule, _now() if now is None else now, allow_extension=self.allow_pacing_extension,
        )

    def _part(self, part_id: str | None) -> TimerPart | None:
        if self._schedule is None or part_id is None:
            return None
        return self._schedule.part_by_id(part_id)

    def _freeze_active(self, *, now: float) -> None:
        part = self._part(self._active_part_id)
        if part is not None and part.state is PartState.RUNNING:
            self._freeze_part(part, now=now)

    def _freeze_part(self, part: TimerPart, *, now: float) -> None:
        part.accumulated_seconds = part.elapsed(now)
        part.started_at_epoch = None
        part.state = PartState.STOPPED
        if part.actual_started_epoch is not None:
            now = max(now, part.actual_started_epoch)
            part.actual_stopped_epoch = now
        schedule = self._schedule
        if schedule is None or (run := schedule.pacing_run) is None:
            return
        complete = None
        if run.window is not None:
            parts = list(schedule.iter_parts())
            if all(item.id in run.reached_ids for item in parts):
                _, closing = transition_budget(schedule, run.window, parts)
                complete = now + closing
        schedule.pacing_run = replace(run, boundary_epoch=now, completed_projection_epoch=complete)
