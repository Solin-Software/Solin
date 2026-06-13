"""
engine.py — Solin timer domain
==============================
``TimerEngine`` is the authoritative live clock/stopwatch for the advanced
timer. It is the *only* component that mutates part run-state and produces the
``TimerSnapshot`` that every observer (QML window, the tab UI, and a future
network adapter) renders.

Design notes:
    • Elapsed time is derived from ``started_at`` epoch each tick, never
      accumulated by adding deltas — so it cannot drift and it survives the
      machine sleeping or the app restarting.
    • Exactly one part runs at a time. Starting a part freezes whatever was
      running. Lifecycle per part: idle → (Start) → running → (Stop) → stopped
      → (Reset) → idle.
    • When nothing is running and nothing is frozen-active, the snapshot is
      ``active=False`` and the window shows the wall clock.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from .models import (
    Direction,
    MeetingPart,
    MeetingSchedule,
    PartState,
    TimerSnapshot,
    _now,
)
from .part_titles import is_indexed_part_title_source
from ..i18n.timer_part_titles import display_part_title

# Update cadence: 250 ms keeps the countdown visually smooth while staying
# cheap. Elapsed is recomputed from wall time, so the interval only affects
# refresh smoothness, never accuracy.
_TICK_MS = 250


class TimerEngine(QObject):
    """Live timer state machine. Emits snapshots; holds no UI references."""

    # Fine-grained refresh (every _TICK_MS) — payload is TimerSnapshot.to_dict().
    tick = Signal(dict)
    # Emitted only on discrete transitions (start/stop/reset/schedule swap).
    state_changed = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._schedule: MeetingSchedule | None = None
        self._active_part_id: str | None = None
        self._direction: Direction = Direction.DOWN

        # After a Stop, the frozen result stays on screen for this long before
        # the window falls back to the wall clock (0 = revert immediately).
        self._freeze_seconds: int = 3
        self._pending_revert_id: str | None = None
        self._revert_timer = QTimer(self)
        self._revert_timer.setSingleShot(True)
        self._revert_timer.timeout.connect(self._on_revert_timeout)

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    # ── Configuration ─────────────────────────────────────────────────────────

    def set_direction(self, direction: Direction) -> None:
        if direction is not self._direction:
            self._direction = direction
            self._emit_state()

    def set_freeze_seconds(self, seconds: int) -> None:
        """How long a stopped part's frozen time lingers before the clock
        returns to the wall time."""
        self._freeze_seconds = max(0, int(seconds))

    @property
    def direction(self) -> Direction:
        return self._direction

    @property
    def schedule(self) -> MeetingSchedule | None:
        return self._schedule

    def set_schedule(self, schedule: MeetingSchedule | None) -> None:
        """Swap in a new schedule (e.g. week/meeting-type change).

        Any part already in the RUNNING state (restored from persistence)
        becomes the active part so a meeting in progress resumes seamlessly.
        """
        self._cancel_revert()
        self._schedule = schedule
        self._active_part_id = None
        if schedule is not None:
            running = next(
                (p for p in schedule.parts if p.state is PartState.RUNNING), None
            )
            if running is not None:
                self._active_part_id = running.id
        self._emit_state()

    # ── Part lifecycle ────────────────────────────────────────────────────────

    def start_part(self, part_id: str) -> None:
        if self._schedule is None:
            return
        part = self._schedule.part_by_id(part_id)
        if part is None or part.state is PartState.RUNNING:
            return

        # A new part supersedes any pending "show frozen result" revert.
        self._cancel_revert()

        # Freeze whatever is currently running.
        self._freeze_active()

        # Start fresh from idle; if it was previously stopped, Reset is required
        # first, so a stopped part is treated as a clean restart here as well.
        if part.state is PartState.STOPPED:
            part.accumulated_seconds = 0.0
        now = _now()
        part.started_at_epoch = now
        if part.first_started_epoch is None:
            part.first_started_epoch = now
        part.state = PartState.RUNNING
        self._active_part_id = part.id
        self._emit_state()

    def stop_part(self, part_id: str | None = None) -> None:
        """Freeze a running part (defaults to the active one)."""
        if self._schedule is None:
            return
        pid = part_id or self._active_part_id
        if pid is None:
            return
        part = self._schedule.part_by_id(pid)
        if part is None or part.state is not PartState.RUNNING:
            return
        self._freeze_part(part)
        # Keep the frozen result on screen briefly, then fall back to the wall
        # clock. The part stays STOPPED in the schedule (its result is still
        # shown in the tab); only the clock window reverts.
        if self._freeze_seconds > 0:
            self._pending_revert_id = part.id
            self._revert_timer.start(self._freeze_seconds * 1000)
        else:
            self._active_part_id = None
        self._emit_state()

    def reset_part(self, part_id: str) -> None:
        if self._schedule is None:
            return
        part = self._schedule.part_by_id(part_id)
        if part is None:
            return
        if self._pending_revert_id == part.id:
            self._cancel_revert()
        part.state = PartState.IDLE
        part.started_at_epoch = None
        part.first_started_epoch = None
        part.accumulated_seconds = 0.0
        if self._active_part_id == part.id:
            self._active_part_id = None
        self._emit_state()

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self, now: float | None = None) -> TimerSnapshot:
        now = _now() if now is None else now
        part = (
            self._schedule.part_by_id(self._active_part_id)
            if (self._schedule is not None and self._active_part_id is not None)
            else None
        )
        if part is None:
            return TimerSnapshot(
                active=False,
                wall_clock_epoch=now,
                direction=self._direction,
            )
        elapsed = part.elapsed(now)
        remaining = part.planned_seconds - elapsed
        return TimerSnapshot(
            active=True,
            wall_clock_epoch=now,
            direction=self._direction,
            active_part_id=part.id,
            active_part_title=self._display_title_for_part(part),
            active_section=part.section,
            planned_seconds=part.planned_seconds,
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            overrun=remaining < 0,
            state=part.state,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    def _freeze_active(self) -> None:
        if self._schedule is None or self._active_part_id is None:
            return
        part = self._schedule.part_by_id(self._active_part_id)
        if part is not None and part.state is PartState.RUNNING:
            self._freeze_part(part)

    def _display_title_for_part(self, part: MeetingPart) -> str:
        indexed_number: int | None = None
        if self._schedule is not None and is_indexed_part_title_source(part.title):
            section_position = 0
            for candidate in self._schedule.parts:
                if candidate.section is part.section:
                    section_position += 1
                if candidate.id == part.id:
                    indexed_number = section_position
                    break
        return display_part_title(part, indexed_number=indexed_number)

    @staticmethod
    def _freeze_part(part) -> None:
        part.accumulated_seconds = part.elapsed()
        part.started_at_epoch = None
        part.state = PartState.STOPPED

    def _cancel_revert(self) -> None:
        self._revert_timer.stop()
        self._pending_revert_id = None

    def _on_revert_timeout(self) -> None:
        pid = self._pending_revert_id
        self._pending_revert_id = None
        if pid is None or self._schedule is None:
            return
        part = self._schedule.part_by_id(pid)
        # Only revert if that part is still the (stopped) active one — the user
        # may have started another part or reset it during the hold window.
        if self._active_part_id == pid and (part is None or part.state is PartState.STOPPED):
            self._active_part_id = None
            self._emit_state()

    def _on_tick(self) -> None:
        self.tick.emit(self.snapshot().to_dict())

    def _emit_state(self) -> None:
        snap = self.snapshot().to_dict()
        self.state_changed.emit(snap)
        # Also push an immediate tick so listeners refresh without waiting.
        self.tick.emit(snap)
