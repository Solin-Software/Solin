"""
engine.py — Solin timer domain
==============================
``TimerEngine`` adapts the framework-independent ``TimerStateMachine`` to Qt.
It supplies refresh scheduling, delayed frozen-result reversion, translated
display titles, and signals for the presentation layer.

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

from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from .models import Direction, MeetingPart, MeetingSchedule, TimerSnapshot
from .part_titles import is_indexed_part_title_source
from .state_machine import TimerStateMachine
from ..i18n.timer_part_titles import display_part_title

# Update cadence: 250 ms keeps the countdown visually smooth while staying
# cheap. Elapsed is recomputed from wall time, so the interval only affects
# refresh smoothness, never accuracy.
_TICK_MS = 250


class TimerEngine(QObject):
    """Qt scheduler and signal adapter for the pure timer state machine."""

    # Fine-grained refresh (every _TICK_MS) — payload is TimerSnapshot.to_dict().
    tick = Signal(dict)
    # Emitted only on discrete transitions (start/stop/reset/schedule swap).
    state_changed = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._state = TimerStateMachine()

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
        if self._state.set_direction(direction):
            self._emit_state()

    def set_freeze_seconds(self, seconds: int) -> None:
        """How long a stopped part's frozen time lingers before the clock
        returns to the wall time."""
        self._freeze_seconds = max(0, int(seconds))

    @property
    def direction(self) -> Direction:
        return self._state.direction

    @property
    def schedule(self) -> MeetingSchedule | None:
        return self._state.schedule

    def set_schedule(self, schedule: MeetingSchedule | None) -> None:
        """Swap in a new schedule (e.g. week/meeting-type change).

        Any part already in the RUNNING state (restored from persistence)
        becomes the active part so a meeting in progress resumes seamlessly.
        """
        self._cancel_revert()
        self._state.set_schedule(schedule)
        self._emit_state()

    # ── Part lifecycle ────────────────────────────────────────────────────────

    def start_part(self, part_id: str) -> None:
        if self._state.start_part(part_id):
            self._cancel_revert()
            self._emit_state()

    def stop_part(self, part_id: str | None = None) -> None:
        """Freeze a running part (defaults to the active one)."""
        pid = part_id or self._state.active_part_id
        if pid is None:
            return
        if not self._state.stop_part(pid):
            return
        # Keep the frozen result on screen briefly, then fall back to the wall
        # clock. The part stays STOPPED in the schedule (its result is still
        # shown in the tab); only the clock window reverts.
        if self._freeze_seconds > 0:
            self._pending_revert_id = pid
            self._revert_timer.start(self._freeze_seconds * 1000)
        else:
            self._state.clear_stopped_part(pid)
        self._emit_state()

    def reset_part(self, part_id: str) -> None:
        if self._pending_revert_id == part_id:
            self._cancel_revert()
        if self._state.reset_part(part_id):
            self._emit_state()

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self, now: float | None = None) -> TimerSnapshot:
        snapshot = self._state.snapshot(now=now)
        part = self._active_part()
        if part is None:
            return snapshot
        return replace(
            snapshot,
            active_part_title=self._display_title_for_part(part),
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    def _active_part(self) -> MeetingPart | None:
        schedule = self._state.schedule
        active_part_id = self._state.active_part_id
        if schedule is None or active_part_id is None:
            return None
        return schedule.part_by_id(active_part_id)

    def _display_title_for_part(self, part: MeetingPart) -> str:
        schedule = self._state.schedule
        if schedule is None:
            return part.title
        indexed_number: int | None = None
        if is_indexed_part_title_source(part.title):
            section_position = 0
            for candidate in schedule.parts:
                if candidate.section is part.section:
                    section_position += 1
                if candidate.id == part.id:
                    indexed_number = section_position
                    break
        return display_part_title(part, indexed_number=indexed_number)

    def _cancel_revert(self) -> None:
        self._revert_timer.stop()
        self._pending_revert_id = None

    def _on_revert_timeout(self) -> None:
        pid = self._pending_revert_id
        self._pending_revert_id = None
        if pid is None:
            return
        if self._state.clear_stopped_part(pid):
            self._emit_state()

    def _on_tick(self) -> None:
        self.tick.emit(self.snapshot().to_dict())

    def _emit_state(self) -> None:
        snap = self.snapshot().to_dict()
        self.state_changed.emit(snap)
        # Also push an immediate tick so listeners refresh without waiting.
        self.tick.emit(snap)
