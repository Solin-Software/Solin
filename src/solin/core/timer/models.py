"""
models.py — Solin timer domain
==============================
Serializable, framework-agnostic value objects for the advanced timer.

Everything here is plain Python (``dataclasses`` + ``enum``) with explicit
``to_dict``/``from_dict`` so the same objects round-trip through QSettings
(persistence) *and* a future JSON network payload without any Qt coupling.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import Enum


# ── Enums ─────────────────────────────────────────────────────────────────────

class ClockMode(str, Enum):
    ANALOG = "analog"
    ANALOG_DIGITAL = "analog_digital"
    DIGITAL = "digital"


class AnalogClockStyle(str, Enum):
    SIGNATURE = "signature"
    CLASSIC = "classic"


class Direction(str, Enum):
    DOWN = "down"   # countdown (default)
    UP = "up"       # count-up / stopwatch


class PartTimerDisplay(str, Enum):
    TIMER = "timer"
    CLOCK = "clock"
    CLOCK_TIMER = "clock_timer"


class MediaCountdownPresentation(str, Enum):
    """Visual used for the countdown projected through the media windows."""

    CIRCULAR = "circular"
    YEARLY_TEXT = "yearly_text"


class MeetingType(str, Enum):
    MIDWEEK = "midweek"
    WEEKEND = "weekend"


class Section(str, Enum):
    OPENING_COMMENTS = "opening_comments"
    TREASURES = "treasures"        # Treasures From God's Word
    MINISTRY = "ministry"          # Apply Yourself to the Field Ministry (≈12 min)
    LIVING = "living"              # Living as Christians (≈15 min before CBS)
    CONCLUDING_COMMENTS = "concluding_comments"
    PUBLIC_TALK = "public_talk"    # Weekend public talk
    WATCHTOWER = "watchtower"      # Weekend Watchtower study


class PartKind(str, Enum):
    STANDARD = "standard"
    CBS = "cbs"


class PartState(str, Enum):
    IDLE = "idle"        # never started (or reset)
    RUNNING = "running"  # actively counting
    STOPPED = "stopped"  # frozen after a stop


ANALOG_CLOCK_STYLE_OPTIONS: tuple[AnalogClockStyle, ...] = (
    AnalogClockStyle.SIGNATURE,
    AnalogClockStyle.CLASSIC,
)

CLOCK_MODE_OPTIONS: tuple[ClockMode, ...] = (
    ClockMode.DIGITAL,
    ClockMode.ANALOG,
    ClockMode.ANALOG_DIGITAL,
)

PART_TIMER_DISPLAY_OPTIONS: tuple[PartTimerDisplay, ...] = (
    PartTimerDisplay.TIMER,
    PartTimerDisplay.CLOCK,
    PartTimerDisplay.CLOCK_TIMER,
)

MEDIA_COUNTDOWN_PRESENTATION_OPTIONS: tuple[MediaCountdownPresentation, ...] = (
    MediaCountdownPresentation.CIRCULAR,
    MediaCountdownPresentation.YEARLY_TEXT,
)

# Sections whose parts must keep a fixed section total (redistribution applies).
FIXED_TOTAL_SECONDS: dict[Section, int] = {
    Section.MINISTRY: 12 * 60,
    Section.LIVING: 15 * 60,
}


def _now() -> float:
    """Monotonic-ish wall clock in epoch seconds (single seam for tests)."""
    return time.time()


def _enum_or_default(enum_type, value, default):
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


# ── ClockConfig ───────────────────────────────────────────────────────────────

@dataclass
class ClockConfig:
    """Per-profile presentation settings for the advanced clock window."""

    mode: ClockMode = ClockMode.DIGITAL
    analog_style: AnalogClockStyle = AnalogClockStyle.SIGNATURE
    hour_format_24h: bool = True
    show_ampm: bool = False
    show_seconds: bool = True
    direction: Direction = Direction.DOWN
    part_timer_display: PartTimerDisplay = PartTimerDisplay.TIMER
    # Display size as a percentage of the largest rendered content that fits.
    text_scale_pct: int = 100
    # Seconds a stopped part's frozen result lingers before the clock returns
    # to the wall time.
    freeze_seconds: int = 3

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "analog_style": self.analog_style.value,
            "hour_format_24h": self.hour_format_24h,
            "show_ampm": self.show_ampm,
            "show_seconds": self.show_seconds,
            "direction": self.direction.value,
            "part_timer_display": self.part_timer_display.value,
            "text_scale_pct": int(self.text_scale_pct),
            "freeze_seconds": int(self.freeze_seconds),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ClockConfig":
        if not data:
            return cls()
        return cls(
            mode=_enum_or_default(
                ClockMode,
                data.get("mode", ClockMode.DIGITAL.value),
                ClockMode.DIGITAL,
            ),
            analog_style=_enum_or_default(
                AnalogClockStyle,
                data.get("analog_style", AnalogClockStyle.SIGNATURE.value),
                AnalogClockStyle.SIGNATURE,
            ),
            hour_format_24h=bool(data.get("hour_format_24h", True)),
            show_ampm=bool(data.get("show_ampm", False)),
            show_seconds=bool(data.get("show_seconds", True)),
            direction=_enum_or_default(
                Direction,
                data.get("direction", Direction.DOWN.value),
                Direction.DOWN,
            ),
            part_timer_display=_enum_or_default(
                PartTimerDisplay,
                data.get("part_timer_display", PartTimerDisplay.TIMER.value),
                PartTimerDisplay.TIMER,
            ),
            text_scale_pct=int(data.get("text_scale_pct", 100)),
            freeze_seconds=int(data.get("freeze_seconds", 3)),
        )

    def clamped(self) -> "ClockConfig":
        """Return a copy with display scale and freeze delay clamped to sane ranges."""
        scale = max(10, min(100, int(self.text_scale_pct)))
        freeze = max(0, min(30, int(self.freeze_seconds)))
        return replace(self, text_scale_pct=scale, freeze_seconds=freeze)


# ── MeetingPart ───────────────────────────────────────────────────────────────

@dataclass
class MeetingPart:
    """A single timed segment of a meeting.

    ``planned_seconds`` is the scheduled duration. The live progress is captured
    by ``started_at_epoch`` (wall-clock time the operator pressed Start) plus
    ``accumulated_seconds`` (time banked before the current run, e.g. after a
    stop). ``elapsed()`` combines both so the value survives an app restart.
    """

    id: str
    section: Section
    title: str
    planned_seconds: int
    kind: PartKind = PartKind.STANDARD
    state: PartState = PartState.IDLE
    # Anchor for the *current* run; cleared whenever the part is frozen/stopped.
    started_at_epoch: float | None = None
    # Wall-clock time the part was first started; kept across stops for display
    # ("when did this part begin?") and only cleared on reset.
    first_started_epoch: float | None = None
    accumulated_seconds: float = 0.0

    @staticmethod
    def new(
        section: Section,
        title: str,
        planned_seconds: int,
        *,
        kind: PartKind = PartKind.STANDARD,
    ) -> "MeetingPart":
        return MeetingPart(
            id=f"{section.value}-{uuid.uuid4().hex[:8]}",
            section=section,
            title=title,
            planned_seconds=int(planned_seconds),
            kind=kind,
        )

    # ── Live computation ─────────────────────────────────────────────────────

    def elapsed(self, now: float | None = None) -> float:
        """Seconds elapsed so far (continues past planned for overrun)."""
        base = self.accumulated_seconds
        if self.state is PartState.RUNNING and self.started_at_epoch is not None:
            now = _now() if now is None else now
            base += max(0.0, now - self.started_at_epoch)
        return base

    def remaining(self, now: float | None = None) -> float:
        """Planned − elapsed. Negative once the part overruns."""
        return self.planned_seconds - self.elapsed(now)

    def is_overrun(self, now: float | None = None) -> bool:
        return self.remaining(now) < 0

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "section": self.section.value,
            "title": self.title,
            "planned_seconds": int(self.planned_seconds),
            "kind": self.kind.value,
            "state": self.state.value,
            "started_at_epoch": self.started_at_epoch,
            "first_started_epoch": self.first_started_epoch,
            "accumulated_seconds": float(self.accumulated_seconds),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeetingPart":
        try:
            kind = PartKind(data.get("kind", PartKind.STANDARD.value))
        except ValueError:
            kind = PartKind.STANDARD
        return cls(
            id=str(data["id"]),
            section=Section(data["section"]),
            title=str(data.get("title", "")),
            planned_seconds=int(data.get("planned_seconds", 0)),
            kind=kind,
            state=PartState(data.get("state", PartState.IDLE.value)),
            started_at_epoch=(
                float(data["started_at_epoch"])
                if data.get("started_at_epoch") is not None
                else None
            ),
            first_started_epoch=(
                float(data["first_started_epoch"])
                if data.get("first_started_epoch") is not None
                else None
            ),
            accumulated_seconds=float(data.get("accumulated_seconds", 0.0)),
        )


# ── MeetingSchedule ───────────────────────────────────────────────────────────

@dataclass
class MeetingSchedule:
    """The ordered list of parts for one (week, meeting-type) pair."""

    week_monday: str  # ISO date, e.g. "2026-06-01"
    meeting_type: MeetingType
    parts: list[MeetingPart] = field(default_factory=list)

    # ── Queries ───────────────────────────────────────────────────────────────

    def part_by_id(self, part_id: str) -> MeetingPart | None:
        for p in self.parts:
            if p.id == part_id:
                return p
        return None

    def index_of(self, part_id: str) -> int:
        for i, p in enumerate(self.parts):
            if p.id == part_id:
                return i
        return -1

    def parts_in(self, section: Section) -> list[MeetingPart]:
        return [p for p in self.parts if p.section is section]

    def section_total_seconds(self, section: Section) -> int:
        return sum(p.planned_seconds for p in self.parts_in(section))

    def count_for(self, section: Section) -> int:
        return len(self.parts_in(section))

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "week_monday": self.week_monday,
            "meeting_type": self.meeting_type.value,
            "parts": [p.to_dict() for p in self.parts],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeetingSchedule":
        return cls(
            week_monday=str(data["week_monday"]),
            meeting_type=MeetingType(data["meeting_type"]),
            parts=[MeetingPart.from_dict(p) for p in data.get("parts", [])],
        )


# ── TimerSnapshot ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TimerSnapshot:
    """Immutable view of the live timer state.

    This is the canonical payload the UI renders and a future network adapter
    would serialize to JSON — built once by the engine each tick.
    """

    # When no part is active the window shows the wall clock; ``active`` is False.
    active: bool
    wall_clock_epoch: float
    direction: Direction
    active_part_id: str | None = None
    active_part_title: str = ""
    active_section: Section | None = None
    planned_seconds: int = 0
    elapsed_seconds: float = 0.0
    remaining_seconds: float = 0.0
    overrun: bool = False
    state: PartState = PartState.IDLE

    def display_seconds(self) -> float:
        """Seconds the clock face should show given the configured direction."""
        if not self.active:
            return 0.0
        if self.state is PartState.STOPPED:
            return self.elapsed_seconds
        return self.elapsed_seconds if self.direction is Direction.UP else self.remaining_seconds

    def to_dict(self) -> dict:
        data = asdict(self)
        data["direction"] = self.direction.value
        data["active_section"] = self.active_section.value if self.active_section else None
        data["state"] = self.state.value
        data["display_seconds"] = self.display_seconds()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TimerSnapshot":
        section = data.get("active_section")
        return cls(
            active=bool(data.get("active", False)),
            wall_clock_epoch=float(data.get("wall_clock_epoch", 0.0)),
            direction=Direction(data.get("direction", Direction.DOWN.value)),
            active_part_id=(
                str(data["active_part_id"])
                if data.get("active_part_id") is not None
                else None
            ),
            active_part_title=str(data.get("active_part_title", "")),
            active_section=Section(section) if section else None,
            planned_seconds=int(data.get("planned_seconds", 0)),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            remaining_seconds=float(data.get("remaining_seconds", 0.0)),
            overrun=bool(data.get("overrun", False)),
            state=PartState(data.get("state", PartState.IDLE.value)),
        )
