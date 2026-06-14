"""Framework-independent application service for the meeting timer."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol

from .models import ClockConfig, MeetingSchedule, MeetingType, PartState, Section
from .schedule_factory import (
    adjust_part,
    build_default_schedule,
    configurable_count_for,
    redistribute_section,
    set_section_part_count,
)


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

    def persist_live_state(self) -> None:
        self._repository.save_schedule(self._schedule)

    def adjust_part(self, part_id: str, delta_seconds: int) -> bool:
        if not self._part_is_editable(part_id):
            return False
        adjust_part(self._schedule, part_id, int(delta_seconds))
        self.persist_live_state()
        return True

    def set_part_seconds(self, part_id: str, seconds: int) -> bool:
        if not self._part_is_editable(part_id):
            return False
        redistribute_section(self._schedule, part_id, int(seconds))
        self.persist_live_state()
        return True

    def set_section_count(self, section: Section, count: int) -> bool:
        normalized_count = max(1, int(count))
        if configurable_count_for(self._schedule, section) == normalized_count:
            return False
        set_section_part_count(self._schedule, section, normalized_count)
        self.persist_live_state()
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
        if schedule is not None:
            return schedule
        return build_default_schedule(self._week_monday, self._meeting_type)

    def _part_is_editable(self, part_id: str) -> bool:
        part = self._schedule.part_by_id(part_id)
        return part is not None and part.state is PartState.IDLE
