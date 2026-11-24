"""Persisted settings and observations for a timer schedule's execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite


# Leave a day at each end for rendering in the local time zone.
_MIN_WINDOW_EPOCH = int(datetime(1, 1, 2, tzinfo=UTC).timestamp())
_MAX_WINDOW_EPOCH = int(datetime(9999, 12, 30, 23, 59, 59, tzinfo=UTC).timestamp())


class PacingMode(StrEnum):
    OFF = "off"
    MONITOR = "monitor"
    AUTOMATIC = "automatic"


@dataclass(frozen=True, slots=True)
class PacingTransitions:
    """Optional estimates by upcoming part ID, plus time after the final part."""

    before_part_seconds: tuple[tuple[str, int], ...] = ()
    closing_seconds: int | None = None

    def __post_init__(self) -> None:
        before = dict(self.before_part_seconds)
        if len(before) != len(self.before_part_seconds) or any(
            not isinstance(part_id, str) or not part_id or type(seconds) is not int or seconds < 0
            for part_id, seconds in before.items()
        ):
            raise ValueError("Invalid timer transition estimates.")
        if self.closing_seconds is not None and (type(self.closing_seconds) is not int or self.closing_seconds < 0):
            raise ValueError("Invalid timer closing estimate.")
        object.__setattr__(self, "before_part_seconds", tuple(sorted(before.items())))

    def to_dict(self) -> dict:
        return {"before_parts": dict(self.before_part_seconds), "closing_seconds": self.closing_seconds}

    @classmethod
    def from_dict(cls, data: dict | None) -> PacingTransitions:
        if data is None:
            return cls()
        return cls(tuple(data.get("before_parts", {}).items()), data.get("closing_seconds"))


@dataclass(frozen=True, slots=True)
class PacingWindow:
    """Planned start and end as absolute instants in Unix seconds."""

    start_epoch: int
    end_epoch: int
    untracked_seconds: int | None = None
    transitions: PacingTransitions = PacingTransitions()

    def __post_init__(self) -> None:
        if (
            type(self.start_epoch) is not int or type(self.end_epoch) is not int
            or not _MIN_WINDOW_EPOCH <= self.start_epoch < self.end_epoch <= _MAX_WINDOW_EPOCH
        ):
            raise ValueError("Set a valid timer start and an end after the start.")
        if self.untracked_seconds is not None and (
            type(self.untracked_seconds) is not int or not 0 <= self.untracked_seconds <= self.duration_seconds
        ):
            raise ValueError("The estimated time outside parts must fit within the timer window.")
        if not isinstance(self.transitions, PacingTransitions):
            raise ValueError("Invalid timer transition estimates.")
        if (
            any(seconds > self.duration_seconds for _, seconds in self.transitions.before_part_seconds)
            or (self.transitions.closing_seconds or 0) > self.duration_seconds
        ):
            raise ValueError("Each timer transition estimate must fit within the timer window.")

    @property
    def duration_seconds(self) -> int:
        return self.end_epoch - self.start_epoch

    def to_dict(self) -> dict:
        data = {
            "start_epoch": self.start_epoch, "end_epoch": self.end_epoch,
            "untracked_seconds": self.untracked_seconds,
        }
        if self.transitions != PacingTransitions():
            data["transitions"] = self.transitions.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict | None) -> PacingWindow | None:
        if data is None:
            return None
        return cls(
            data["start_epoch"], data["end_epoch"], data.get("untracked_seconds"),
            PacingTransitions.from_dict(data.get("transitions")),
        )


@dataclass(frozen=True, slots=True)
class PacingRun:
    """The active execution's reference and most recent observed boundary."""

    window: PacingWindow | None
    reached_ids: frozenset[str]
    boundary_epoch: float
    completed_projection_epoch: float | None = None

    def to_dict(self) -> dict:
        return {
            "window": self.window.to_dict() if self.window else None,
            "reached_ids": sorted(self.reached_ids),
            "boundary_epoch": self.boundary_epoch,
            "completed_projection_epoch": self.completed_projection_epoch,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> PacingRun | None:
        if data is None:
            return None
        reached = data["reached_ids"]
        boundary = data["boundary_epoch"]
        completed = data.get("completed_projection_epoch")
        if (
            not isinstance(reached, list) or any(not isinstance(item, str) for item in reached)
            or not isfinite(boundary)
            or (completed is not None and (not isfinite(completed) or completed < boundary))
        ):
            raise ValueError("Invalid timer execution state.")
        return cls(PacingWindow.from_dict(data["window"]), frozenset(reached), boundary, completed)
