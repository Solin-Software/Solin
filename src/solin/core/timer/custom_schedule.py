"""Structural editing rules for user-owned timer schedules."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .models import (
    MAX_PLANNED_PART_SECONDS,
    MIN_CUSTOM_PART_SECONDS,
    CustomTimerSchedule,
    PartState,
    TimerPart,
    TimerSection,
    normalize_timer_title,
)


class TimerOperationError(ValueError):
    """An application error that adapters can present or serialize."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_title(title: str) -> str:
    try:
        return normalize_timer_title(title)
    except ValueError as error:
        raise TimerOperationError("invalid", str(error)) from error


def _validate_seconds(seconds: int) -> int:
    if type(seconds) is not int or not MIN_CUSTOM_PART_SECONDS <= seconds <= MAX_PLANNED_PART_SECONDS:
        raise TimerOperationError(
            "invalid",
            f"Set a duration between {MIN_CUSTOM_PART_SECONDS} and {MAX_PLANNED_PART_SECONDS} seconds.",
        )
    return seconds


def _before_index(entries: Sequence[TimerPart | TimerSection], entry_id: str | None) -> int:
    if entry_id is None:
        return len(entries)
    for index, entry in enumerate(entries):
        if entry.id == entry_id:
            return index
    raise TimerOperationError("not_found", "The timer position or item no longer exists.")


def _section(schedule: CustomTimerSchedule, section_id: str) -> TimerSection:
    section = schedule.section_by_id(section_id)
    if section is None:
        raise TimerOperationError("not_found", "The timer section no longer exists.")
    return section


def _part(schedule: CustomTimerSchedule, part_id: str) -> TimerPart:
    part = schedule.part_by_id(part_id)
    if part is None:
        raise TimerOperationError("not_found", "The timer part no longer exists.")
    return part


def _siblings(
    schedule: CustomTimerSchedule, section_id: str | None,
) -> list[TimerPart | TimerSection] | list[TimerPart]:
    return schedule.entries if section_id is None else _section(schedule, section_id).parts


def _add_part(
    schedule: CustomTimerSchedule, *, title: str, planned_seconds: int,
    section_id: str | None = None, before_id: str | None = None,
) -> None:
    part = TimerPart.new(section_id, validate_title(title), _validate_seconds(planned_seconds))
    siblings = _siblings(schedule, section_id)
    siblings.insert(_before_index(siblings, before_id), part)


def _add_section(
    schedule: CustomTimerSchedule, *, title: str, before_id: str | None = None,
) -> None:
    schedule.entries.insert(_before_index(schedule.entries, before_id), TimerSection.new(validate_title(title)))


def _rename_part(schedule: CustomTimerSchedule, *, part_id: str, title: str) -> None:
    _part(schedule, part_id).title = validate_title(title)


def _rename_section(schedule: CustomTimerSchedule, *, section_id: str, title: str) -> None:
    _section(schedule, section_id).title = validate_title(title)


def _rename_schedule(schedule: CustomTimerSchedule, *, title: str) -> None:
    schedule.title = validate_title(title)


def _move_part(
    schedule: CustomTimerSchedule, *, part_id: str,
    section_id: str | None = None, before_id: str | None = None,
) -> None:
    part = _part(schedule, part_id)
    source = _siblings(schedule, part.section_id)
    destination = _siblings(schedule, section_id)
    if source is destination and before_id == part_id:
        return
    source.remove(part)
    destination.insert(_before_index(destination, before_id), part)
    part.section = section_id


def _move_section(
    schedule: CustomTimerSchedule, *, section_id: str, before_id: str | None = None,
) -> None:
    section = _section(schedule, section_id)
    if before_id == section_id:
        return
    schedule.entries.remove(section)
    schedule.entries.insert(_before_index(schedule.entries, before_id), section)


def _delete_part(schedule: CustomTimerSchedule, *, part_id: str) -> None:
    part = _part(schedule, part_id)
    _siblings(schedule, part.section_id).remove(part)


def _delete_section(schedule: CustomTimerSchedule, *, section_id: str) -> None:
    section = _section(schedule, section_id)
    if section.parts:
        raise TimerOperationError("blocked", "Move or remove the section's parts before deleting it.")
    schedule.entries.remove(section)


def _set_part_seconds(
    schedule: CustomTimerSchedule, *, part_id: str, planned_seconds: int,
) -> None:
    part = _part(schedule, part_id)
    if part.state is not PartState.IDLE:
        raise TimerOperationError("blocked", "Reset the timer part before changing its duration.")
    part.planned_seconds = _validate_seconds(planned_seconds)


_EDITORS: dict[str, Callable[..., None]] = {
    "add_part": _add_part,
    "add_section": _add_section,
    "rename_part": _rename_part,
    "rename_section": _rename_section,
    "rename_schedule": _rename_schedule,
    "move_part": _move_part,
    "move_section": _move_section,
    "delete_part": _delete_part,
    "delete_section": _delete_section,
    "set_part_seconds": _set_part_seconds,
}


def edit_custom_schedule(schedule: CustomTimerSchedule, action: str, **values: object) -> None:
    editor = _EDITORS.get(action)
    if editor is None:
        raise TimerOperationError("invalid", "Unknown timer schedule edit.")
    if schedule.has_running_part():
        raise TimerOperationError("blocked", "Stop the timer before editing this schedule.")
    try:
        editor(schedule, **values)
    except TypeError as error:
        raise TimerOperationError("invalid", "Invalid timer schedule edit arguments.") from error


def duplicate_custom_schedule(source: CustomTimerSchedule, title: str) -> CustomTimerSchedule:
    duplicate = CustomTimerSchedule.new(validate_title(title))
    for entry in source.entries:
        if isinstance(entry, TimerSection):
            section = TimerSection.new(entry.title)
            section.parts.extend(
                TimerPart.new(section.id, part.title, part.planned_seconds)
                for part in entry.parts
            )
            duplicate.entries.append(section)
        else:
            duplicate.entries.append(TimerPart.new(None, entry.title, entry.planned_seconds))
    return duplicate
