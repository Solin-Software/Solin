"""Framework-independent application service for the meeting timer."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

from .models import (
    CONFIGURABLE_PART_SECTIONS,
    MAX_PLANNED_PART_SECONDS,
    MAX_SECTION_PART_COUNT,
    MIN_PLANNED_PART_SECONDS,
    MIN_SECTION_PART_COUNT,
    AssignmentSetup,
    ClockConfig,
    MeetingSchedule,
    MeetingType,
    PartKind,
    Section,
)
from .schedule_factory import (
    build_default_schedule,
    configurable_count_for,
    schedule_has_timing_data,
    set_part_seconds,
    set_section_part_count,
)


class AssignmentAutofillScope(StrEnum):
    OFF = "off"
    VARIABLE_SECTIONS = "variable_sections"
    ALL_MIDWEEK_DURATIONS = "all_midweek_durations"


# Release default. Inject another scope at composition to change rollout.
DEFAULT_ASSIGNMENT_AUTOFILL_SCOPE = AssignmentAutofillScope.VARIABLE_SECTIONS


@dataclass(frozen=True, slots=True)
class DetectedPartTiming:
    section: Section
    kind: PartKind
    duration_seconds: int | None


class TimerRepository(Protocol):
    def load_clock_config(self) -> ClockConfig: ...

    def save_clock_config(self, config: ClockConfig) -> None: ...

    def get_timer_visible(self) -> bool: ...

    def set_timer_visible(self, visible: bool) -> None: ...

    def load_last_meeting_type(self) -> MeetingType: ...

    def save_last_meeting_type(self, meeting_type: MeetingType) -> None: ...

    def load_schedule(
        self,
        week_monday: date,
        meeting_type: MeetingType,
    ) -> MeetingSchedule | None: ...

    def save_schedule(self, schedule: MeetingSchedule) -> None: ...


class TimerSession:
    """Owns selected timer context and persists every application mutation."""

    def __init__(
        self,
        repository: TimerRepository,
        current_week_monday: date,
    ) -> None:
        self._repository = repository
        self._current_week_monday = current_week_monday
        self._week_monday = current_week_monday
        self._meeting_type = repository.load_last_meeting_type()
        self._clock_config = repository.load_clock_config().clamped()
        self._schedule = self._load_schedule()

    @property
    def week_monday(self) -> date:
        return self._week_monday

    @property
    def meeting_type(self) -> MeetingType:
        return self._meeting_type

    @property
    def clock_config(self) -> ClockConfig:
        return self._clock_config

    @property
    def schedule(self) -> MeetingSchedule:
        return self._schedule

    @property
    def is_current_week(self) -> bool:
        return self._week_monday == self._current_week_monday

    def previous_week(self) -> MeetingSchedule:
        self._week_monday -= timedelta(days=7)
        return self._replace_schedule()

    def next_week(self) -> MeetingSchedule:
        self._week_monday += timedelta(days=7)
        return self._replace_schedule()

    def go_to_current_week(self, current_week_monday: date) -> int:
        direction = (current_week_monday > self._week_monday) - (
            current_week_monday < self._week_monday
        )
        self._current_week_monday = current_week_monday
        self._week_monday = current_week_monday
        self._replace_schedule()
        return int(direction)

    def set_meeting_type(self, meeting_type: MeetingType) -> bool:
        if meeting_type is self._meeting_type:
            return False
        self._meeting_type = meeting_type
        self._repository.save_last_meeting_type(meeting_type)
        self._replace_schedule()
        return True

    def record_schedule_change(self) -> None:
        self._schedule.assignment_setup = AssignmentSetup.MANUAL
        self._repository.save_schedule(self._schedule)

    def adjust_part(self, part_id: str, delta_seconds: int) -> bool:
        part = self._schedule.part_by_id(part_id)
        return part is not None and self.set_part_seconds(
            part_id, part.planned_seconds + int(delta_seconds),
        )

    def set_part_seconds(self, part_id: str, seconds: int) -> bool:
        part = self._schedule.part_by_id(part_id)
        if part is None or not set_part_seconds(part, seconds):
            return False
        self.record_schedule_change()
        return True

    def set_section_count(self, section: Section, count: int) -> bool:
        if section not in CONFIGURABLE_PART_SECTIONS:
            return False
        normalized_count = max(MIN_SECTION_PART_COUNT, min(int(count), MAX_SECTION_PART_COUNT))
        if configurable_count_for(self._schedule, section) == normalized_count:
            return False
        set_section_part_count(self._schedule, section, normalized_count)
        self.record_schedule_change()
        return True

    def can_autofill_assignments(self, current_week_monday: date) -> bool:
        return (
            self._meeting_type is MeetingType.MIDWEEK
            and self._week_monday >= current_week_monday
            and self._schedule.assignment_setup is AssignmentSetup.DEFAULT
            and not schedule_has_timing_data(self._schedule)
        )

    def apply_detected_assignments(
        self,
        current_week_monday: date,
        timings: Iterable[DetectedPartTiming],
        scope: AssignmentAutofillScope,
    ) -> bool:
        if scope is AssignmentAutofillScope.OFF or not self.can_autofill_assignments(
            current_week_monday
        ):
            return False

        durations: dict[tuple[Section, PartKind], list[int | None]] = {}
        for timing in timings:
            durations.setdefault((timing.section, timing.kind), []).append(timing.duration_seconds)

        candidate = deepcopy(self._schedule)
        for section in CONFIGURABLE_PART_SECTIONS:
            count = len(durations.get((section, PartKind.STANDARD), ()))
            if not MIN_SECTION_PART_COUNT <= count <= MAX_SECTION_PART_COUNT:
                return False
            set_section_part_count(candidate, section, count)

        groups = {(section, PartKind.STANDARD) for section in CONFIGURABLE_PART_SECTIONS}
        if scope is AssignmentAutofillScope.ALL_MIDWEEK_DURATIONS:
            groups.update((part.section, part.kind) for part in candidate.parts)

        for section, kind in groups:
            parts = [part for part in candidate.parts_in(section) if part.kind is kind]
            seconds_by_part = durations.get((section, kind), ())
            if len(parts) != len(seconds_by_part):
                return False
            for part, seconds in zip(parts, seconds_by_part, strict=True):
                if seconds is not None and MIN_PLANNED_PART_SECONDS <= seconds <= MAX_PLANNED_PART_SECONDS:
                    set_part_seconds(part, seconds)

        candidate.assignment_setup = AssignmentSetup.DETECTED
        self._repository.save_schedule(candidate)
        self._schedule = candidate
        return True

    def update_clock(self, key: str, value: object) -> ClockConfig | None:
        data = self._clock_config.to_dict()
        if key not in data:
            return None
        data[key] = value
        self._clock_config = ClockConfig.from_dict(data).clamped()
        self._repository.save_clock_config(self._clock_config)
        return self._clock_config

    def timer_visible(self) -> bool:
        return self._repository.get_timer_visible()

    def set_timer_visible(self, visible: bool) -> None:
        self._repository.set_timer_visible(bool(visible))

    def _replace_schedule(self) -> MeetingSchedule:
        self._schedule = self._load_schedule()
        return self._schedule

    def _load_schedule(self) -> MeetingSchedule:
        schedule = self._repository.load_schedule(
            self._week_monday,
            self._meeting_type,
        )
        if schedule is None:
            return build_default_schedule(self._week_monday, self._meeting_type)
        if schedule.assignment_setup is AssignmentSetup.LEGACY:
            initial = build_default_schedule(self._week_monday, self._meeting_type)
            schedule.assignment_setup = (
                AssignmentSetup.DEFAULT
                if not schedule_has_timing_data(schedule)
                and _schedule_shape(schedule) == _schedule_shape(initial)
                else AssignmentSetup.MANUAL
            )
        return schedule


def _schedule_shape(schedule: MeetingSchedule) -> list[tuple[Section, PartKind, str, int]]:
    return [
        (part.section, part.kind, part.title, part.planned_seconds)
        for part in schedule.parts
    ]
