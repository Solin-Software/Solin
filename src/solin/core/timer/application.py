"""Framework-independent application service for the timer."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Protocol

from .custom_schedule import (
    TimerOperationError,
    duplicate_custom_schedule,
    edit_custom_schedule,
    validate_title,
)
from .models import (
    CONFIGURABLE_PART_SECTIONS,
    MAX_PLANNED_PART_SECONDS,
    MAX_SECTION_PART_COUNT,
    MIN_CUSTOM_PART_SECONDS,
    MIN_PLANNED_PART_SECONDS,
    MIN_SECTION_PART_COUNT,
    AssignmentSetup,
    ClockConfig,
    CustomTimerSchedule,
    MeetingSchedule,
    MeetingType,
    PartKind,
    PartState,
    Section,
    TimerSchedule,
)
from .schedule_factory import (
    build_default_schedule,
    configurable_count_for,
    schedule_has_timing_data,
    set_part_seconds,
    set_section_part_count,
)


log = logging.getLogger(__name__)


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


class CustomTimerRepository(Protocol):
    def list_schedules(self) -> tuple[CustomTimerSchedule, ...]: ...

    def load_schedule(self, schedule_id: str) -> CustomTimerSchedule | None: ...

    def save_schedule(self, schedule: CustomTimerSchedule) -> None: ...

    def delete_schedule(self, schedule_id: str) -> None: ...

    def load_selection(self) -> str | None: ...

    def save_selection(self, schedule_id: str | None) -> None: ...


class TimerSession:
    """Owns selected timer context and persists every application mutation."""

    def __init__(
        self,
        repository: TimerRepository,
        current_week_monday: date,
        custom_repository: CustomTimerRepository | None = None,
    ) -> None:
        self._repository = repository
        self._custom_repository = custom_repository
        self._unrestored_custom_selection = False
        self._current_week_monday = current_week_monday
        self._week_monday = current_week_monday
        self._meeting_type = repository.load_last_meeting_type()
        self._clock_config = repository.load_clock_config().clamped()
        custom = self._load_custom_selection()
        self._schedule: TimerSchedule = (
            custom or self._load_meeting_schedule(self._week_monday, self._meeting_type)
        )

    def _load_custom_selection(self) -> CustomTimerSchedule | None:
        store = self._custom_repository
        if store is None:
            return None
        try:
            selected_id = store.load_selection()
            if selected_id is None:
                return None
            schedule = store.load_schedule(selected_id)
            if schedule is not None:
                return schedule
        except OSError:
            log.warning("Could not restore the selected timer schedule.", exc_info=True)
        self._unrestored_custom_selection = True
        return None

    def _clear_unrestored_custom_selection(self) -> None:
        if self._unrestored_custom_selection:
            self._custom_store().save_selection(None)
            self._unrestored_custom_selection = False

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
    def schedule(self) -> TimerSchedule:
        return self._schedule

    @property
    def is_current_week(self) -> bool:
        return (
            isinstance(self._schedule, MeetingSchedule)
            and self._week_monday == self._current_week_monday
        )

    def previous_week(self) -> MeetingSchedule:
        return self._select_meeting(self._week_monday - timedelta(days=7), self._meeting_type)

    def next_week(self) -> MeetingSchedule:
        return self._select_meeting(self._week_monday + timedelta(days=7), self._meeting_type)

    def go_to_current_week(self, current_week_monday: date) -> int:
        direction = (current_week_monday > self._week_monday) - (
            current_week_monday < self._week_monday
        )
        self._select_meeting(current_week_monday, self._meeting_type)
        self._current_week_monday = current_week_monday
        return int(direction)

    def set_meeting_type(self, meeting_type: MeetingType) -> bool:
        if meeting_type is self._meeting_type and isinstance(self._schedule, MeetingSchedule):
            return False
        self._select_meeting(self._week_monday, meeting_type)
        return True

    def record_schedule_change(self) -> None:
        schedule = self._schedule
        if isinstance(schedule, CustomTimerSchedule):
            self._save_custom_revision(schedule)
            return
        self._clear_unrestored_custom_selection()
        previous_setup = schedule.assignment_setup
        schedule.assignment_setup = AssignmentSetup.MANUAL
        try:
            self._repository.save_schedule(schedule)
        except (OSError, RuntimeError):
            schedule.assignment_setup = previous_setup
            raise

    def adjust_part(self, part_id: str, delta_seconds: int) -> bool:
        part = self._schedule.part_by_id(part_id)
        if part is None:
            return False
        seconds = part.planned_seconds + int(delta_seconds)
        if isinstance(self._schedule, CustomTimerSchedule):
            seconds = max(MIN_CUSTOM_PART_SECONDS, min(seconds, MAX_PLANNED_PART_SECONDS))
        return self.set_part_seconds(part_id, seconds)

    def set_part_seconds(self, part_id: str, seconds: int) -> bool:
        schedule = self._schedule
        if isinstance(schedule, CustomTimerSchedule):
            return self.edit_custom_schedule(
                schedule.id, schedule.revision, "set_part_seconds", part_id=part_id, planned_seconds=seconds,
            )
        part = schedule.part_by_id(part_id)
        if part is None:
            return False
        previous = part.planned_seconds
        if not set_part_seconds(part, seconds):
            return False
        try:
            self.record_schedule_change()
        except (OSError, RuntimeError):
            part.planned_seconds = previous
            raise
        return True

    def set_section_count(self, section: Section, count: int) -> bool:
        schedule = self._schedule
        if (
            not isinstance(schedule, MeetingSchedule)
            or schedule.meeting_type is not MeetingType.MIDWEEK
            or section not in CONFIGURABLE_PART_SECTIONS
        ):
            return False
        normalized_count = max(MIN_SECTION_PART_COUNT, min(int(count), MAX_SECTION_PART_COUNT))
        if configurable_count_for(schedule, section) == normalized_count:
            return False
        previous = schedule.parts
        set_section_part_count(schedule, section, normalized_count)
        removed_ids = {part.id for part in previous} - {part.id for part in schedule.parts}
        if any(part.id in removed_ids and part.state is PartState.RUNNING for part in previous):
            schedule.parts = previous
            raise TimerOperationError("blocked", "Stop the timer part before removing it.")
        try:
            self.record_schedule_change()
        except (OSError, RuntimeError):
            schedule.parts = previous
            raise
        return True

    def rename_part(self, part_id: str, title: str | None) -> bool:
        schedule = self._schedule
        if isinstance(schedule, CustomTimerSchedule):
            return self.edit_custom_schedule(
                schedule.id, schedule.revision, "rename_part", part_id=part_id, title=title,
            )
        part = schedule.part_by_id(part_id)
        if part is None:
            raise TimerOperationError("not_found", "The timer part no longer exists.")
        title = validate_title(title) if title is not None else None
        previous = part.title_override
        if previous == title:
            return False
        part.title_override = title
        try:
            self.record_schedule_change()
        except (OSError, RuntimeError):
            part.title_override = previous
            raise
        return True

    def list_custom_schedules(self) -> tuple[CustomTimerSchedule, ...]:
        if self._custom_repository is None:
            return ()
        return self._custom_repository.list_schedules()

    def get_custom_schedule(self, schedule_id: str) -> CustomTimerSchedule | None:
        active = self._schedule
        if isinstance(active, CustomTimerSchedule) and active.id == schedule_id:
            return deepcopy(active)
        return self._custom_store().load_schedule(schedule_id)

    def create_custom_schedule(self, title: str) -> CustomTimerSchedule:
        schedule = CustomTimerSchedule.new(validate_title(title))
        self._custom_store().save_schedule(schedule)
        return schedule

    def duplicate_custom_schedule(
        self, schedule_id: str, title: str, expected_revision: int,
    ) -> CustomTimerSchedule:
        source = self._require_custom_schedule(schedule_id, expected_revision)
        schedule = duplicate_custom_schedule(source, title)
        self._custom_store().save_schedule(schedule)
        return schedule

    def delete_custom_schedule(self, schedule_id: str, expected_revision: int) -> None:
        schedule = self._require_custom_schedule(schedule_id, expected_revision)
        if isinstance(self._schedule, CustomTimerSchedule) and self._schedule.id == schedule_id:
            raise TimerOperationError("blocked", "Select another schedule before deleting this one.")
        if schedule.has_running_part():
            raise TimerOperationError("blocked", "Stop the timer before deleting this schedule.")
        self._custom_store().delete_schedule(schedule_id)

    def select_custom_schedule(self, schedule_id: str) -> bool:
        if isinstance(self._schedule, CustomTimerSchedule) and self._schedule.id == schedule_id:
            return False
        self._require_stopped()
        schedule = self._require_custom_schedule(schedule_id)
        self._custom_store().save_selection(schedule_id)
        self._unrestored_custom_selection = False
        self._schedule = schedule
        return True

    def select_meeting_schedule(self) -> bool:
        if isinstance(self._schedule, MeetingSchedule):
            self._clear_unrestored_custom_selection()
            return False
        self._require_stopped()
        schedule = self._load_meeting_schedule(self._week_monday, self._meeting_type)
        self._custom_store().save_selection(None)
        self._schedule = schedule
        return True

    def edit_custom_schedule(
        self, schedule_id: str, expected_revision: int, action: str, **values: object,
    ) -> bool:
        schedule = self._require_custom_schedule(schedule_id, expected_revision)
        candidate = deepcopy(schedule)
        edit_custom_schedule(candidate, action, **values)
        if candidate == schedule:
            return False
        self._save_custom_revision(candidate)
        if isinstance(self._schedule, CustomTimerSchedule) and self._schedule.id == schedule_id:
            self._schedule = candidate
        return True

    def _custom_store(self) -> CustomTimerRepository:
        if self._custom_repository is None:
            raise TimerOperationError("unavailable", "Custom timer schedules are unavailable.")
        return self._custom_repository

    def _require_custom_schedule(
        self, schedule_id: str, expected_revision: int | None = None,
    ) -> CustomTimerSchedule:
        active = self._schedule
        schedule = (
            active if isinstance(active, CustomTimerSchedule) and active.id == schedule_id
            else self._custom_store().load_schedule(schedule_id)
        )
        if schedule is None:
            raise TimerOperationError("not_found", "The timer schedule no longer exists.")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision != schedule.revision
        ):
            raise TimerOperationError("schedule_stale", "The timer schedule changed. Reload it before editing.")
        return schedule

    def _save_custom_revision(self, schedule: CustomTimerSchedule) -> None:
        schedule.revision += 1
        try:
            self._custom_store().save_schedule(schedule)
        except (OSError, RuntimeError):
            schedule.revision -= 1
            raise

    def _require_stopped(self) -> None:
        if self._schedule.has_running_part():
            raise TimerOperationError("blocked", "Stop the timer before selecting another schedule.")

    def can_autofill_assignments(self, current_week_monday: date) -> bool:
        return (
            isinstance(self._schedule, MeetingSchedule)
            and self._meeting_type is MeetingType.MIDWEEK
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
        schedule = self._schedule
        if (
            not isinstance(schedule, MeetingSchedule)
            or scope is AssignmentAutofillScope.OFF
            or not self.can_autofill_assignments(current_week_monday)
        ):
            return False

        durations: dict[tuple[Section, PartKind], list[int | None]] = {}
        for timing in timings:
            durations.setdefault((timing.section, timing.kind), []).append(timing.duration_seconds)

        candidate = deepcopy(schedule)
        for section in CONFIGURABLE_PART_SECTIONS:
            count = len(durations.get((section, PartKind.STANDARD), ()))
            if not MIN_SECTION_PART_COUNT <= count <= MAX_SECTION_PART_COUNT:
                return False
            set_section_part_count(candidate, section, count)

        groups = {(section, PartKind.STANDARD) for section in CONFIGURABLE_PART_SECTIONS}
        if scope is AssignmentAutofillScope.ALL_MIDWEEK_DURATIONS:
            groups.update(
                (part.section, part.kind) for part in candidate.parts
                if isinstance(part.section, Section)
            )

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
        config = ClockConfig.from_dict(data).clamped()
        self._repository.save_clock_config(config)
        self._clock_config = config
        return self._clock_config

    def timer_visible(self) -> bool:
        return self._repository.get_timer_visible()

    def set_timer_visible(self, visible: bool) -> None:
        self._repository.set_timer_visible(bool(visible))

    def _select_meeting(self, week_monday: date, meeting_type: MeetingType) -> MeetingSchedule:
        if not isinstance(self._schedule, MeetingSchedule):
            raise TimerOperationError("blocked", "Select meetings before changing the week or meeting type.")
        if (week_monday, meeting_type) == (self._week_monday, self._meeting_type):
            self._clear_unrestored_custom_selection()
            return self._schedule
        self._require_stopped()
        schedule = self._load_meeting_schedule(week_monday, meeting_type)
        self._clear_unrestored_custom_selection()
        if meeting_type is not self._meeting_type:
            self._repository.save_last_meeting_type(meeting_type)
        self._week_monday = week_monday
        self._meeting_type = meeting_type
        self._schedule = schedule
        return schedule

    def _load_meeting_schedule(self, week_monday: date, meeting_type: MeetingType) -> MeetingSchedule:
        schedule = self._repository.load_schedule(week_monday, meeting_type)
        if schedule is None:
            return build_default_schedule(week_monday, meeting_type)
        if schedule.assignment_setup is AssignmentSetup.LEGACY:
            initial = build_default_schedule(week_monday, meeting_type)
            schedule.assignment_setup = (
                AssignmentSetup.DEFAULT
                if not schedule_has_timing_data(schedule)
                and _schedule_shape(schedule) == _schedule_shape(initial)
                else AssignmentSetup.MANUAL
            )
        return schedule


def _schedule_shape(schedule: MeetingSchedule) -> list[tuple[str | None, PartKind, str, int]]:
    return [
        (part.section_id, part.kind, part.title, part.planned_seconds)
        for part in schedule.parts
    ]
