"""Meeting schedule domain used by automatic meeting-aware features."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any

MIDWEEK = "midweek"
WEEKEND = "weekend"
UNCONFIGURED_WEEKDAY = -1
DEFAULT_MIDWEEK_TIME = "19:30"
DEFAULT_WEEKEND_TIME = "10:00"


def normalize_weekday(value: Any) -> int:
    """Return a Python weekday index, or -1 when not configured."""

    try:
        weekday = int(value)
    except (TypeError, ValueError):
        return UNCONFIGURED_WEEKDAY
    if 0 <= weekday <= 6:
        return weekday
    return UNCONFIGURED_WEEKDAY


def parse_time_text(value: Any) -> int | None:
    """Parse HH:mm into minutes after midnight."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def minutes_to_time_text(minutes: int | None, fallback: str = "") -> str:
    if minutes is None:
        return fallback
    clamped = max(0, min(23 * 60 + 59, int(minutes)))
    return f"{clamped // 60:02d}:{clamped % 60:02d}"


@dataclass(frozen=True, slots=True)
class MeetingSlot:
    """One configured weekly meeting start."""

    kind: str
    weekday: int = UNCONFIGURED_WEEKDAY
    start_minutes: int | None = None

    @property
    def is_configured(self) -> bool:
        return 0 <= self.weekday <= 6 and self.start_minutes is not None

    @property
    def time_text(self) -> str:
        return minutes_to_time_text(self.start_minutes)

    @classmethod
    def from_values(cls, kind: str, weekday: Any, time_text: Any) -> "MeetingSlot":
        return cls(
            kind=kind,
            weekday=normalize_weekday(weekday),
            start_minutes=parse_time_text(time_text),
        )


@dataclass(frozen=True, slots=True)
class MeetingOccurrence:
    """A concrete meeting occurrence for the current local date."""

    slot: MeetingSlot
    starts_at: datetime

    @property
    def slot_id(self) -> str:
        return f"{self.slot.kind}:{self.starts_at.date().isoformat()}:{self.slot.time_text}"

    def milliseconds_until_start(self, now: datetime) -> int:
        return max(0, int((self.starts_at - now).total_seconds() * 1000))


@dataclass(frozen=True, slots=True)
class MeetingSchedule:
    """Profile-level meeting schedule, independent from any one feature."""

    midweek: MeetingSlot
    weekend: MeetingSlot

    @property
    def has_configured_slot(self) -> bool:
        return any(slot.is_configured for slot in self.slots)

    @property
    def slots(self) -> tuple[MeetingSlot, MeetingSlot]:
        return (self.midweek, self.weekend)

    def pre_meeting_occurrence(self, now: datetime) -> MeetingOccurrence | None:
        """Return today's next configured meeting if it has not started yet."""

        candidates: list[MeetingOccurrence] = []
        for slot in self.slots:
            if not slot.is_configured or slot.weekday != now.weekday():
                continue
            assert slot.start_minutes is not None
            starts_at = datetime.combine(
                now.date(),
                time(slot.start_minutes // 60, slot.start_minutes % 60),
                tzinfo=now.tzinfo,
            )
            if now < starts_at:
                candidates.append(MeetingOccurrence(slot=slot, starts_at=starts_at))
        if not candidates:
            return None
        return min(candidates, key=lambda occurrence: occurrence.starts_at)

__all__ = [
    "DEFAULT_MIDWEEK_TIME",
    "DEFAULT_WEEKEND_TIME",
    "MIDWEEK",
    "UNCONFIGURED_WEEKDAY",
    "WEEKEND",
    "MeetingOccurrence",
    "MeetingSchedule",
    "MeetingSlot",
    "minutes_to_time_text",
    "normalize_weekday",
    "parse_time_text",
]
