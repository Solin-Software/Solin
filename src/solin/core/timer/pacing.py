"""Forecast and adjust the finish of an ordered timer schedule."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum

from .models import (
    MAX_PLANNED_PART_SECONDS,
    MIN_PLANNED_PART_SECONDS,
    MeetingSchedule,
    PartKind,
    PartState,
    Section,
    TimerPart,
    TimerSchedule,
)
from .pacing_models import PacingMode, PacingTransitions, PacingWindow
from .part_titles import SPIRITUAL_GEMS_TITLE, TREASURES_TALK_TITLE
from .schedule_factory import meeting_pacing_transitions

PACING_TOLERANCE_SECONDS = 30
DEFAULT_ADJUSTMENT_WEIGHT = 100
DEFAULT_ALLOW_PACING_EXTENSION = True
_CONGREGATION_BIBLE_STUDY_WEIGHT = 80


class PacingStatus(StrEnum):
    AHEAD = "ahead"
    ON_TIME = "on_time"
    BEHIND = "behind"


class PacingAvailability(StrEnum):
    READY = "ready"
    MISSING_WINDOW = "missing_window"
    AWAITING_RESET = "awaiting_reset"
    NO_PROJECTION = "no_projection"


@dataclass(frozen=True, slots=True)
class PacingSnapshot:
    start_epoch: int
    end_epoch: int
    projected_end_epoch: float
    adjusted_end_epoch: float
    status: PacingStatus
    complete: bool
    projection_anchor_epoch: float
    projection_tail_seconds: float
    available_shorten_seconds: int
    available_extend_seconds: int
    tolerance_seconds: int = PACING_TOLERANCE_SECONDS

    @property
    def deviation_seconds(self) -> float:
        return self.projected_end_epoch - self.end_epoch

    @property
    def residual_seconds(self) -> float:
        return self.adjusted_end_epoch - self.end_epoch

    def to_dict(self) -> dict:
        return dict(
            asdict(self), deviation_seconds=self.deviation_seconds, residual_seconds=self.residual_seconds,
        )


@dataclass(frozen=True, slots=True)
class PacingState:
    mode: PacingMode
    availability: PacingAvailability
    projection: PacingSnapshot | None = None


def _meeting_adjustment_weight(part: TimerPart) -> int:
    if part.kind is PartKind.CBS and part.section is Section.LIVING:
        return _CONGREGATION_BIBLE_STUDY_WEIGHT
    if part.section in (Section.LIVING, Section.WATCHTOWER) or (
        part.section is Section.TREASURES and part.title in (TREASURES_TALK_TITLE, SPIRITUAL_GEMS_TITLE)
    ):
        return DEFAULT_ADJUSTMENT_WEIGHT
    return 0


def adjustment_weights(schedule: TimerSchedule, parts: Sequence[TimerPart]) -> dict[str, int]:
    """Resolve eligibility against the whole schedule, including parts already reached."""
    eligible = [part for part in parts if part.planned_seconds >= MIN_PLANNED_PART_SECONDS]
    if isinstance(schedule, MeetingSchedule):
        return {part.id: weight for part in eligible if (weight := _meeting_adjustment_weight(part))}
    if any(part.adjustment_weight for part in parts):
        return {part.id: part.adjustment_weight for part in eligible if part.adjustment_weight}
    return dict.fromkeys((part.id for part in eligible), DEFAULT_ADJUSTMENT_WEIGHT)


def resolve_transition_budget(
    window: PacingWindow, parts: list[TimerPart], estimates: PacingTransitions,
) -> tuple[dict[str, float], float]:
    """Keep explicit estimates and divide the remaining reserve among unknown intervals."""
    provided = dict(estimates.before_part_seconds)
    known = {part.id: provided[part.id] for part in parts if part.id in provided}
    closing = estimates.closing_seconds
    unallocated = window.untracked_seconds
    if unallocated is None:
        unallocated = window.duration_seconds - sum(part.planned_seconds for part in parts)
    remaining = max(0, unallocated - sum(known.values()) - (closing or 0))
    unknown = len(parts) - len(known) + (closing is None)
    fallback = remaining / unknown if unknown else 0.0
    return (
        {part.id: float(known[part.id]) if part.id in known else fallback for part in parts},
        fallback if closing is None else float(closing),
    )


def transition_budget(
    schedule: TimerSchedule, window: PacingWindow, parts: list[TimerPart],
) -> tuple[dict[str, float], float]:
    estimates = meeting_pacing_transitions(schedule) if isinstance(schedule, MeetingSchedule) else window.transitions
    return resolve_transition_budget(window, parts, estimates)


def _candidates(parts: list[TimerPart], weights: dict[str, int]) -> list[tuple[TimerPart, int]]:
    return [(part, weights[part.id]) for part in parts if part.id in weights]


def _capacity(part: TimerPart, *, extend: bool) -> int:
    if extend:
        return MAX_PLANNED_PART_SECONDS - part.planned_seconds
    return part.planned_seconds - MIN_PLANNED_PART_SECONDS


def _correction(deviation: float, shorten: int, extend: int) -> int:
    if deviation > PACING_TOLERANCE_SECONDS:
        return -min(shorten, round(deviation))
    if deviation < -PACING_TOLERANCE_SECONDS:
        return min(extend, round(-deviation))
    return 0


def _allocate(seconds: int, candidates: list[tuple[TimerPart, int]]) -> dict[str, int]:
    """Apportion whole seconds by weight, redistributing around saturated parts."""
    extend = seconds > 0
    remaining = abs(seconds)
    allocated = dict.fromkeys((part.id for part, _ in candidates), 0)
    available = [
        (part, weight, capacity) for part, weight in candidates
        if (capacity := _capacity(part, extend=extend)) > 0
    ]
    while remaining and available:
        total_weight = sum(weight for _, weight, _ in available)
        saturated = [
            (part, capacity) for part, weight, capacity in available
            if remaining * weight >= capacity * total_weight
        ]
        if saturated:
            for part, capacity in saturated:
                allocated[part.id] += capacity
                remaining -= capacity
            available = [(part, weight, cap) for part, weight, cap in available if not allocated[part.id]]
            continue
        remainders = []
        for part, weight, _ in available:
            whole, remainder = divmod(remaining * weight, total_weight)
            allocated[part.id] = whole
            remainders.append((remainder, part.id))
        for _, part_id in sorted(remainders, key=lambda item: item[0], reverse=True)[
            :remaining - sum(allocated[part.id] for part, _, _ in available)
        ]:
            allocated[part_id] += 1
        break
    return {part_id: seconds if extend else -seconds for part_id, seconds in allocated.items()}


def effective_target_at_start(
    schedule: TimerSchedule,
    part: TimerPart,
    remaining: list[TimerPart],
    now: float,
    *,
    automatic: bool,
    allow_extension: bool,
) -> int:
    window = schedule.effective_pacing_window
    if not automatic or window is None:
        return part.planned_seconds
    parts = list(schedule.iter_parts())
    weights = adjustment_weights(schedule, parts)
    if part.id not in weights:
        return part.planned_seconds
    before, closing = transition_budget(schedule, window, parts)
    all_remaining = [part, *remaining]
    expected_end = now + part.planned_seconds + closing + sum(
        item.planned_seconds + before[item.id] for item in remaining
    )
    candidates = _candidates(all_remaining, weights)
    correction = _correction(
        expected_end - window.end_epoch,
        sum(_capacity(item, extend=False) for item, _ in candidates),
        sum(_capacity(item, extend=True) for item, _ in candidates) if allow_extension else 0,
    )
    return part.planned_seconds + _allocate(correction, candidates).get(part.id, 0)


def pacing_snapshot(
    schedule: TimerSchedule, now: float, *, automatic: bool = False,
    allow_extension: bool = DEFAULT_ALLOW_PACING_EXTENSION,
) -> PacingSnapshot | None:
    run = schedule.pacing_run
    window = schedule.effective_pacing_window
    if window is None:
        return None
    parts = list(schedule.iter_parts())
    if not parts or (run is None and any(part.state is not PartState.IDLE for part in parts)):
        return None
    before, closing = transition_budget(schedule, window, parts)
    pending = [
        part for part in parts
        if part.state is PartState.IDLE and (run is None or part.id not in run.reached_ids)
    ]
    running = next((part for part in parts if part.state is PartState.RUNNING), None)
    completed = None
    if run is not None and running is None and not pending:
        completed = run.completed_projection_epoch
        if completed is None:
            completed = run.boundary_epoch + closing
    complete = completed is not None
    if complete:
        assert completed is not None
        anchor = projected_end = completed
        tail = 0.0
    else:
        tail = sum(part.planned_seconds + before[part.id] for part in pending) + closing
        if running is not None:
            assert running.started_at_epoch is not None
            anchor = running.started_at_epoch + max(0.0, running.effective_seconds - running.accumulated_seconds)
        else:
            next_gap = before[pending[0].id]
            anchor = (run.boundary_epoch if run is not None else window.start_epoch) + next_gap
            tail -= next_gap
        projected_end = max(now, anchor) + tail

    deviation = projected_end - window.end_epoch
    candidates = _candidates(pending, adjustment_weights(schedule, parts)) if automatic and pending else []
    shorten = sum(_capacity(part, extend=False) for part, _ in candidates)
    extend = sum(_capacity(part, extend=True) for part, _ in candidates) if allow_extension else 0
    adjusted_end = projected_end + _correction(deviation, shorten, extend)
    status = PacingStatus.ON_TIME
    if deviation > PACING_TOLERANCE_SECONDS:
        status = PacingStatus.BEHIND
    elif deviation < -PACING_TOLERANCE_SECONDS:
        status = PacingStatus.AHEAD
    return PacingSnapshot(
        window.start_epoch, window.end_epoch, projected_end, adjusted_end, status, complete,
        anchor, tail, shorten, extend,
    )


def pacing_state(
    schedule: TimerSchedule | None, now: float, *, mode: PacingMode,
    allow_extension: bool = DEFAULT_ALLOW_PACING_EXTENSION,
) -> PacingState:
    if schedule is None:
        return PacingState(mode, PacingAvailability.NO_PROJECTION)
    if schedule.effective_pacing_window is None:
        availability = (
            PacingAvailability.MISSING_WINDOW if schedule.pacing_window is None
            else PacingAvailability.AWAITING_RESET
        )
        return PacingState(mode, availability)
    projection = pacing_snapshot(schedule, now, automatic=mode is PacingMode.AUTOMATIC, allow_extension=allow_extension)
    availability = PacingAvailability.READY if projection else PacingAvailability.NO_PROJECTION
    return PacingState(mode, availability, projection)
