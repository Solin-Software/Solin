"""Persisted settings and observations for a timer schedule's execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from math import isfinite


# Leave a day at each end for rendering in the local time zone.
_MIN_WINDOW_EPOCH = int(datetime(1, 1, 2, tzinfo=UTC).timestamp())
_MAX_WINDOW_EPOCH = int(datetime(9999, 12, 30, 23, 59, 59, tzinfo=UTC).timestamp())
_MINUTES_PER_DAY = 24 * 60


def normalize_time_of_day(value: object) -> str:
    """Validate and normalize a 24-hour ``HH:MM`` time-of-day string."""
    if not isinstance(value, str):
        raise ValueError("Set a valid start and end time (HH:MM).")
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("Set a valid start and end time (HH:MM).")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Set a valid start and end time (HH:MM).")
    return f"{hour:02d}:{minute:02d}"


def _minutes_of_day(hhmm: str) -> int:
    hour_text, minute_text = hhmm.split(":", 1)
    return int(hour_text) * 60 + int(minute_text)


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
class ResolvedPacingWindow:
    """Concrete pacing window resolved to absolute Unix instants for an execution."""

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
    def from_dict(cls, data: dict | None) -> ResolvedPacingWindow | None:
        if data is None:
            return None
        return cls(
            data["start_epoch"], data["end_epoch"], data.get("untracked_seconds"),
            PacingTransitions.from_dict(data.get("transitions")),
        )


@dataclass(frozen=True, slots=True)
class PacingWindow:
    """Planned start and end as date-independent local times of day (``HH:MM``)."""

    start_time: str
    end_time: str
    untracked_seconds: int | None = None
    transitions: PacingTransitions = PacingTransitions()

    def __post_init__(self) -> None:
        start = normalize_time_of_day(self.start_time)
        end = normalize_time_of_day(self.end_time)
        if start == end:
            raise ValueError("Set an end time different from the start time.")
        object.__setattr__(self, "start_time", start)
        object.__setattr__(self, "end_time", end)
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

    @classmethod
    def from_start_and_duration(
        cls,
        start_time: str,
        duration_seconds: int,
        *,
        untracked_seconds: int | None = None,
        transitions: PacingTransitions | None = None,
    ) -> PacingWindow:
        start = normalize_time_of_day(start_time)
        if type(duration_seconds) is not int or not 60 <= duration_seconds < _MINUTES_PER_DAY * 60:
            raise ValueError("Set a valid timer window duration.")
        start_min = _minutes_of_day(start)
        end_min = (start_min + duration_seconds // 60) % _MINUTES_PER_DAY
        end = f"{end_min // 60:02d}:{end_min % 60:02d}"
        return cls(start, end, untracked_seconds, transitions or PacingTransitions())

    @property
    def duration_seconds(self) -> int:
        start_min = _minutes_of_day(self.start_time)
        end_min = _minutes_of_day(self.end_time)
        return ((end_min - start_min) % _MINUTES_PER_DAY) * 60

    def resolve_at(self, now: float) -> ResolvedPacingWindow:
        """Anchor this time-of-day window onto the local day of ``now``."""
        local_now = datetime.fromtimestamp(now).astimezone()
        start_min = _minutes_of_day(self.start_time)
        end_min = _minutes_of_day(self.end_time)
        anchor_day = local_now.date()
        # If the window crosses midnight and we are in the post-midnight tail,
        # anchor the start to yesterday so the active window spans midnight cleanly.
        if end_min < start_min and (local_now.hour * 60 + local_now.minute) < end_min:
            anchor_day -= timedelta(days=1)
        start_dt = datetime.combine(
            anchor_day,
            time(start_min // 60, start_min % 60),
            tzinfo=local_now.tzinfo,
        )
        start_epoch = int(start_dt.timestamp())
        end_epoch = start_epoch + self.duration_seconds
        return ResolvedPacingWindow(
            start_epoch=start_epoch,
            end_epoch=end_epoch,
            untracked_seconds=self.untracked_seconds,
            transitions=self.transitions,
        )

    def to_dict(self) -> dict:
        data: dict[str, object] = {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "untracked_seconds": self.untracked_seconds,
        }
        if self.transitions != PacingTransitions():
            data["transitions"] = self.transitions.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict | None) -> PacingWindow | None:
        if data is None:
            return None
        transitions = PacingTransitions.from_dict(data.get("transitions"))
        untracked = data.get("untracked_seconds")
        if "start_time" in data and "end_time" in data:
            return cls(data["start_time"], data["end_time"], untracked, transitions)
        # Migrate persisted schedules created before windows became date-independent.
        if "start_epoch" in data and "end_epoch" in data:
            start_epoch = data["start_epoch"]
            end_epoch = data["end_epoch"]
            if type(start_epoch) is not int or type(end_epoch) is not int or end_epoch <= start_epoch:
                raise ValueError("Set a valid timer start and an end after the start.")
            start_dt = datetime.fromtimestamp(start_epoch).astimezone()
            end_dt = datetime.fromtimestamp(end_epoch).astimezone()
            start_str = start_dt.strftime("%H:%M")
            end_str = end_dt.strftime("%H:%M")
            if start_str == end_str:
                return None
            return cls(start_str, end_str, untracked, transitions)
        raise ValueError("Set a valid start and end time (HH:MM).")


@dataclass(frozen=True, slots=True)
class PacingRun:
    """The active execution's frozen reference window and most recent observed boundary."""

    window: ResolvedPacingWindow | None
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
        return cls(
            ResolvedPacingWindow.from_dict(data["window"]),
            frozenset(reached),
            boundary,
            completed,
        )
