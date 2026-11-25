"""
models.py — Solin timer domain
==============================
Serializable, framework-agnostic value objects for the advanced timer.

The models have explicit serialization for profile persistence and local
snapshots. Remote adapters publish separate wire contracts.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from enum import Enum, StrEnum
from math import isfinite

from .pacing_models import PacingRun, PacingWindow, ResolvedPacingWindow


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
    MINISTRY = "ministry"          # Apply Yourself to the Field Ministry
    LIVING = "living"              # Living as Christians
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


class CustomSectionIcon(StrEnum):
    """Persisted semantic icon choice for a timer section."""

    DEFAULT = "default"
    NONE = "none"
    TREASURES = "treasures"
    MINISTRY = "ministry"
    LIVING = "living"
    PUBLIC_TALK = "public_talk"
    WATCHTOWER = "watchtower"
    BOOK = "book"
    PEOPLE = "people"
    MICROPHONE = "microphone"
    MUSIC = "music"
    VIDEO = "video"
    CALENDAR = "calendar"
    CLOCK = "clock"
    MARKER = "marker"
    CHECK = "check"


CUSTOM_SECTION_ICON_CATALOG: tuple[CustomSectionIcon, ...] = (
    CustomSectionIcon.TREASURES,
    CustomSectionIcon.MINISTRY,
    CustomSectionIcon.LIVING,
    CustomSectionIcon.PUBLIC_TALK,
    CustomSectionIcon.WATCHTOWER,
    CustomSectionIcon.BOOK,
    CustomSectionIcon.PEOPLE,
    CustomSectionIcon.MICROPHONE,
    CustomSectionIcon.MUSIC,
    CustomSectionIcon.VIDEO,
    CustomSectionIcon.CALENDAR,
    CustomSectionIcon.CLOCK,
    CustomSectionIcon.MARKER,
    CustomSectionIcon.CHECK,
)

# Curated reusable section color presets (hex strings), including the hues used
# by official meeting sections so custom schedules share the exact same visual system.
CUSTOM_SECTION_COLOR_PRESETS: tuple[tuple[str, str], ...] = (
    ("teal", "#41b6c4"),
    ("gold", "#c8bf41"),
    ("crimson", "#c84145"),
    ("amber", "#c88841"),
    ("indigo", "#4d5bd4"),
    ("blue", "#4b89dc"),
    ("emerald", "#38b27b"),
    ("violet", "#9466e8"),
    ("neutral", "#8b949e"),
)

_SECTION_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


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

CONFIGURABLE_PART_SECTIONS = (Section.MINISTRY, Section.LIVING)
MIN_SECTION_PART_COUNT = 1
MAX_SECTION_PART_COUNT = 8
MIN_PLANNED_PART_SECONDS = 60
MIN_CUSTOM_PART_SECONDS = 1
MAX_PLANNED_PART_SECONDS = 24 * 60 * 60 - 1
MAX_TIMER_TITLE_LENGTH = 200
MAX_ADJUSTMENT_WEIGHT = 100


class AssignmentSetup(str, Enum):
    DEFAULT = "default"
    DETECTED = "detected"
    MANUAL = "manual"
    LEGACY = "legacy"


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


def normalize_timer_title(title: str) -> str:
    if not isinstance(title, str):
        raise ValueError("A timer name must be text.")
    title = title.strip()
    if (
        not title or len(title) > MAX_TIMER_TITLE_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in title)
    ):
        raise ValueError(f"A timer name must contain 1–{MAX_TIMER_TITLE_LENGTH} characters on one line.")
    return title


def section_id(section: Section | str | None) -> str | None:
    return section.value if isinstance(section, Section) else section


def normalize_section_icon(value: object) -> CustomSectionIcon:
    """Validate an explicit custom-section icon choice."""
    if isinstance(value, CustomSectionIcon):
        return value
    if isinstance(value, str):
        try:
            return CustomSectionIcon(value)
        except ValueError:
            pass
    raise ValueError("Choose a valid timer section icon.")


def _section_icon_from_persisted(value: object) -> CustomSectionIcon:
    """Read a persisted section icon, falling back safely to the default."""
    if isinstance(value, CustomSectionIcon):
        return value
    if isinstance(value, str):
        try:
            return CustomSectionIcon(value)
        except ValueError:
            return CustomSectionIcon.DEFAULT
    return CustomSectionIcon.DEFAULT


def normalize_section_color(value: object) -> str | None:
    """Validate an explicit custom-section color choice (None/'default' or #RRGGBB)."""
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if not cleaned or cleaned == "default":
            return None
        if _SECTION_HEX_COLOR_RE.match(cleaned):
            return cleaned
    raise ValueError("Choose a valid timer section color.")


def _section_color_from_persisted(value: object) -> str | None:
    """Read a persisted section color, falling back safely to default (None)."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if _SECTION_HEX_COLOR_RE.match(cleaned):
        return cleaned
    return None


def _section_from_id(value: str | None) -> Section | str | None:
    return _enum_or_default(Section, value, value)


# ── TimerPart ─────────────────────────────────────────────────────────────────

@dataclass
class TimerPart:
    """A single timed segment with its latest recorded progress.

    ``planned_seconds`` is the scheduled duration. The live progress is captured
    by ``started_at_epoch`` (the current wall-clock anchor) plus
    ``accumulated_seconds`` (elapsed time banked at that anchor). A manual
    adjustment rebases both and corrects ``first_started_epoch`` to match.
    ``elapsed()`` combines the anchor and banked time across app restarts.
    """

    id: str
    section: Section | str | None
    title: str
    planned_seconds: int
    kind: PartKind = PartKind.STANDARD
    state: PartState = PartState.IDLE
    # Anchor for the *current* run; cleared whenever the part is frozen/stopped.
    started_at_epoch: float | None = None
    # Corrected wall-clock start; retained across stops and cleared on reset.
    first_started_epoch: float | None = None
    accumulated_seconds: float = 0.0
    title_override: str | None = None
    adjustment_weight: int = 0
    actual_started_epoch: float | None = None
    actual_stopped_epoch: float | None = None
    run_target_seconds: int | None = None

    @property
    def effective_seconds(self) -> int:
        return self.planned_seconds if self.run_target_seconds is None else self.run_target_seconds

    @property
    def section_id(self) -> str | None:
        return section_id(self.section)

    @property
    def uses_canonical_title(self) -> bool:
        return isinstance(self.section, Section) and self.title_override is None

    @staticmethod
    def new(
        section: Section | str | None,
        title: str,
        planned_seconds: int,
        *,
        kind: PartKind = PartKind.STANDARD,
    ) -> TimerPart:
        return TimerPart(
            id=f"{section.value if isinstance(section, Section) else 'part'}-{uuid.uuid4().hex}",
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
        """Effective target minus elapsed, negative on overrun."""
        return self.effective_seconds - self.elapsed(now)

    def is_overrun(self, now: float | None = None) -> bool:
        return self.remaining(now) < 0

    # ── Serialization ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "section": self.section_id,
            "title": self.title,
            "planned_seconds": int(self.planned_seconds),
            "kind": self.kind.value,
            "state": self.state.value,
            "started_at_epoch": self.started_at_epoch,
            "first_started_epoch": self.first_started_epoch,
            "accumulated_seconds": float(self.accumulated_seconds),
            "title_override": self.title_override,
            "adjustment_weight": self.adjustment_weight,
            "actual_started_epoch": self.actual_started_epoch,
            "actual_stopped_epoch": self.actual_stopped_epoch,
            "run_target_seconds": self.run_target_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TimerPart:
        try:
            kind = PartKind(data.get("kind", PartKind.STANDARD.value))
        except ValueError:
            kind = PartKind.STANDARD
        weight = data.get("adjustment_weight", 0)
        actual_start = data.get("actual_started_epoch")
        actual_stop = data.get("actual_stopped_epoch")
        target = data.get("run_target_seconds")
        if (
            type(weight) is not int or not 0 <= weight <= MAX_ADJUSTMENT_WEIGHT
            or any(value is not None and not isfinite(value) for value in (actual_start, actual_stop))
            or (actual_stop is not None and (actual_start is None or actual_stop < actual_start))
            or (target is not None and (type(target) is not int or not 1 <= target <= MAX_PLANNED_PART_SECONDS))
        ):
            raise ValueError("Invalid timer pacing data.")
        return cls(
            id=str(data["id"]),
            section=_section_from_id(data["section"]),
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
            title_override=(
                normalize_timer_title(data["title_override"])
                if data.get("title_override") is not None else None
            ),
            adjustment_weight=weight,
            actual_started_epoch=actual_start,
            actual_stopped_epoch=actual_stop,
            run_target_seconds=target,
        )


class _OrderedTimerParts:
    """Common queries over the canonical execution order."""

    def iter_parts(self) -> Iterator[TimerPart]:
        raise NotImplementedError

    def part_by_id(self, part_id: str) -> TimerPart | None:
        return next((part for part in self.iter_parts() if part.id == part_id), None)

    def index_of(self, part_id: str) -> int:
        for i, p in enumerate(self.iter_parts()):
            if p.id == part_id:
                return i
        return -1

    def parts_in(self, section: Section | str | None) -> list[TimerPart]:
        return [part for part in self.iter_parts() if part.section == section]

    def section_total_seconds(self, section: Section | str | None) -> int:
        return sum(p.planned_seconds for p in self.parts_in(section))

    def count_for(self, section: Section | str | None) -> int:
        return len(self.parts_in(section))

    def has_running_part(self) -> bool:
        return any(part.state is PartState.RUNNING for part in self.iter_parts())

    def planned_total_seconds(self) -> int:
        return sum(part.planned_seconds for part in self.iter_parts())

    pacing_window: PacingWindow | None
    pacing_run: PacingRun | None

    @property
    def effective_pacing_window(self) -> ResolvedPacingWindow | PacingWindow | None:
        return self.pacing_run.window if self.pacing_run is not None else self.pacing_window

    def resolve_pacing_window(self, now: float) -> ResolvedPacingWindow | None:
        if self.pacing_run is not None:
            return self.pacing_run.window
        if self.pacing_window is not None:
            return self.pacing_window.resolve_at(now)
        return None

    def _pacing_to_dict(self) -> dict:
        return {
            "pacing_window": self.pacing_window.to_dict() if self.pacing_window else None,
            "pacing_run": self.pacing_run.to_dict() if self.pacing_run else None,
        }


@dataclass
class MeetingSchedule(_OrderedTimerParts):
    """The ordered list of parts for one (week, meeting-type) pair."""

    week_monday: str
    meeting_type: MeetingType
    parts: list[TimerPart] = field(default_factory=list)
    assignment_setup: AssignmentSetup = AssignmentSetup.DEFAULT
    pacing_window: PacingWindow | None = None
    pacing_run: PacingRun | None = None
    has_unreadable_record: bool = field(default=False, compare=False, repr=False)

    def iter_parts(self) -> Iterator[TimerPart]:
        return iter(self.parts)

    def to_dict(self) -> dict:
        return {
            "week_monday": self.week_monday,
            "meeting_type": self.meeting_type.value,
            "parts": [p.to_dict() for p in self.parts],
            "assignment_setup": self.assignment_setup.value,
            **self._pacing_to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MeetingSchedule":
        return cls(
            week_monday=str(data["week_monday"]),
            meeting_type=MeetingType(data["meeting_type"]),
            parts=[
                TimerPart.from_dict(dict(part, section=Section(part["section"])))
                for part in data.get("parts", [])
            ],
            assignment_setup=_enum_or_default(
                AssignmentSetup,
                data.get("assignment_setup", AssignmentSetup.LEGACY.value),
                AssignmentSetup.MANUAL,
            ),
            pacing_window=PacingWindow.from_dict(data.get("pacing_window")),
            pacing_run=PacingRun.from_dict(data.get("pacing_run")),
        )


@dataclass
class TimerSection:
    id: str
    title: str
    parts: list[TimerPart] = field(default_factory=list)
    icon: CustomSectionIcon = CustomSectionIcon.DEFAULT
    color: str | None = None

    @classmethod
    def new(
        cls,
        title: str,
        *,
        icon: CustomSectionIcon = CustomSectionIcon.DEFAULT,
        color: str | None = None,
    ) -> TimerSection:
        return cls(
            f"section-{uuid.uuid4().hex}",
            normalize_timer_title(title),
            icon=normalize_section_icon(icon),
            color=normalize_section_color(color),
        )


@dataclass
class CustomTimerSchedule(_OrderedTimerParts):
    id: str
    title: str
    entries: list[TimerPart | TimerSection] = field(default_factory=list)
    revision: int = 0
    pacing_window: PacingWindow | None = None
    pacing_run: PacingRun | None = None

    @classmethod
    def new(cls, title: str) -> CustomTimerSchedule:
        return cls(uuid.uuid4().hex, normalize_timer_title(title))

    @property
    def parts(self) -> list[TimerPart]:
        return list(self.iter_parts())

    def iter_parts(self) -> Iterator[TimerPart]:
        for entry in self.entries:
            if isinstance(entry, TimerSection):
                yield from entry.parts
            else:
                yield entry

    def section_by_id(self, section_id: str) -> TimerSection | None:
        return next(
            (entry for entry in self.entries if isinstance(entry, TimerSection) and entry.id == section_id),
            None,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "revision": self.revision,
            **self._pacing_to_dict(),
            "entries": [
                {
                    "type": "section", "id": entry.id, "title": entry.title,
                    "icon": entry.icon.value,
                    "color": entry.color,
                    "parts": [_encode_custom_part(part) for part in entry.parts],
                }
                if isinstance(entry, TimerSection) else dict(_encode_custom_part(entry), type="part")
                for entry in self.entries
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> CustomTimerSchedule:
        revision = data["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid timer schedule revision.")
        schedule = cls(
            data["id"], normalize_timer_title(data["title"]), revision=revision,
            pacing_window=PacingWindow.from_dict(data.get("pacing_window")),
            pacing_run=PacingRun.from_dict(data.get("pacing_run")),
        )
        for entry in data["entries"]:
            if entry["type"] == "section":
                parent = entry["id"]
                schedule.entries.append(TimerSection(
                    parent, normalize_timer_title(entry["title"]),
                    [_decode_custom_part(part, parent) for part in entry["parts"]],
                    icon=_section_icon_from_persisted(entry.get("icon")),
                    color=_section_color_from_persisted(entry.get("color")),
                ))
            elif entry["type"] == "part":
                schedule.entries.append(_decode_custom_part(entry, None))
            else:
                raise ValueError("Invalid timer schedule entry.")
        ids = [entry.id for entry in schedule.entries if isinstance(entry, TimerSection)]
        parts = schedule.parts
        ids.extend(part.id for part in parts)
        if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("Invalid or duplicate timer entry ID.")
        if sum(part.state is PartState.RUNNING for part in parts) > 1:
            raise ValueError("Multiple running timer parts.")
        return schedule


def _encode_custom_part(part: TimerPart) -> dict:
    data = part.to_dict()
    for key in ("section", "kind", "title_override"):
        del data[key]
    return data


def _decode_custom_part(data: dict, parent: str | None) -> TimerPart:
    if not isinstance(data["id"], str) or not data["id"]:
        raise ValueError("Invalid timer part ID.")
    seconds = data["planned_seconds"]
    if type(seconds) is not int or not MIN_CUSTOM_PART_SECONDS <= seconds <= MAX_PLANNED_PART_SECONDS:
        raise ValueError("Invalid timer part duration.")
    part = TimerPart.from_dict(dict(
        data, section=None, title=normalize_timer_title(data["title"]),
        kind=PartKind.STANDARD.value, title_override=None,
    ))
    part.section = parent
    if (
        not isfinite(part.accumulated_seconds)
        or part.accumulated_seconds < 0
        or (part.state is PartState.RUNNING) != (part.started_at_epoch is not None)
        or any(value is not None and not isfinite(value) for value in (
            part.started_at_epoch, part.first_started_epoch,
        ))
    ):
        raise ValueError("Invalid timer part progress.")
    return part


TimerSchedule = MeetingSchedule | CustomTimerSchedule


# ── TimerSnapshot ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TimerSnapshot:
    """Immutable view of the live timer state.

    The engine produces this view for local rendering and remote publication.
    """

    # When no part is active the window shows the wall clock; ``active`` is False.
    active: bool
    wall_clock_epoch: float
    direction: Direction
    active_part_id: str | None = None
    active_part_title: str = ""
    active_section: Section | str | None = None
    planned_seconds: int = 0  # Target fixed at start, including any pacing adjustment.
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
        data["active_section"] = section_id(self.active_section)
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
            active_section=_section_from_id(section),
            planned_seconds=int(data.get("planned_seconds", 0)),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            remaining_seconds=float(data.get("remaining_seconds", 0.0)),
            overrun=bool(data.get("overrun", False)),
            state=PartState(data.get("state", PartState.IDLE.value)),
        )
