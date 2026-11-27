"""Structural editing rules for user-owned timer schedules."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from .models import (
    MAX_ADJUSTMENT_WEIGHT,
    MAX_PLANNED_PART_SECONDS,
    MIN_CUSTOM_PART_SECONDS,
    MIN_PLANNED_PART_SECONDS,
    CustomSectionIcon,
    CustomTimerSchedule,
    PartState,
    TimerPart,
    TimerSection,
    normalize_section_color,
    normalize_section_icon,
    normalize_timer_title,
)
from .pacing_models import PacingTransitions, PacingWindow


class TimerOperationError(ValueError):
    """An application error that adapters can present or serialize."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TimerDurationBudgetError(TimerOperationError):
    def __init__(self, available_seconds: int, requested_seconds: int) -> None:
        self.available_seconds = available_seconds
        self.requested_seconds = requested_seconds
        super().__init__(
            "duration_budget",
            f"Parts require {requested_seconds} seconds but the schedule allows {available_seconds} seconds.",
        )


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
    icon: CustomSectionIcon | str = CustomSectionIcon.DEFAULT,
    color: str | None = None,
) -> None:
    schedule.entries.insert(
        _before_index(schedule.entries, before_id),
        TimerSection.new(
            validate_title(title),
            icon=_validate_section_icon(icon),
            color=_validate_section_color(color),
        ),
    )


def _rename_part(schedule: CustomTimerSchedule, *, part_id: str, title: str) -> None:
    _part(schedule, part_id).title = validate_title(title)


def _validate_section_icon(icon: object) -> CustomSectionIcon:
    try:
        return normalize_section_icon(icon)
    except ValueError as error:
        raise TimerOperationError("invalid", str(error)) from error


def _validate_section_color(color: object) -> str | None:
    try:
        return normalize_section_color(color)
    except ValueError as error:
        raise TimerOperationError("invalid", str(error)) from error


def _rename_section(schedule: CustomTimerSchedule, *, section_id: str, title: str) -> None:
    _section(schedule, section_id).title = validate_title(title)


def _set_section_icon(
    schedule: CustomTimerSchedule, *, section_id: str, icon: CustomSectionIcon | str,
) -> None:
    _section(schedule, section_id).icon = _validate_section_icon(icon)


def _set_section_color(
    schedule: CustomTimerSchedule, *, section_id: str, color: str | None,
) -> None:
    _section(schedule, section_id).color = _validate_section_color(color)


def _set_section_appearance(
    schedule: CustomTimerSchedule,
    *,
    section_id: str,
    icon: CustomSectionIcon | str,
    color: str | None,
) -> None:
    section = _section(schedule, section_id)
    validated_icon = _validate_section_icon(icon)
    validated_color = _validate_section_color(color)
    section.icon = validated_icon
    section.color = validated_color


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
    if (window := schedule.pacing_window) is not None:
        transitions = window.transitions
        before = tuple(item for item in transitions.before_part_seconds if item[0] != part_id)
        schedule.pacing_window = replace(window, transitions=replace(transitions, before_part_seconds=before))


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


def _set_pacing_window(
    schedule: CustomTimerSchedule, *, start_time: str | None, end_time: str | None,
    untracked_seconds: int | None = None,
) -> None:
    if untracked_seconds is not None and (type(untracked_seconds) is not int or untracked_seconds < 0):
        raise TimerOperationError("invalid", "Set a nonnegative duration for time outside parts.")
    if start_time is None and end_time is None and not untracked_seconds:
        schedule.pacing_window = None
    elif start_time is None or end_time is None:
        raise TimerOperationError("invalid", "Set both the timer start and end.")
    else:
        existing = schedule.pacing_window
        if untracked_seconds is None:
            untracked_seconds = existing.untracked_seconds if existing else 0
        try:
            transitions = existing.transitions if existing else PacingTransitions()
            schedule.pacing_window = PacingWindow(start_time, end_time, untracked_seconds, transitions)
        except ValueError as error:
            raise TimerOperationError("invalid", str(error)) from error


def _set_pacing_transition(
    schedule: CustomTimerSchedule, *, before_part_id: str | None, estimated_seconds: int | None,
) -> None:
    window = schedule.pacing_window
    if window is None:
        raise TimerOperationError("blocked", "Set a timer window before estimating transitions.")
    if estimated_seconds is not None and (
        type(estimated_seconds) is not int or not 0 <= estimated_seconds <= window.duration_seconds
    ):
        raise TimerOperationError("invalid", "Set a transition estimate within the timer window.")
    transitions = window.transitions
    if before_part_id is None:
        transitions = replace(transitions, closing_seconds=estimated_seconds)
    else:
        _part(schedule, before_part_id)
        before = dict(transitions.before_part_seconds)
        if estimated_seconds is None:
            before.pop(before_part_id, None)
        else:
            before[before_part_id] = estimated_seconds
        transitions = replace(transitions, before_part_seconds=tuple(before.items()))
    schedule.pacing_window = replace(window, transitions=transitions)


def _validate_adjustment_weight(
    schedule: CustomTimerSchedule, *, part_id: str, adjustment_weight: int,
) -> tuple[TimerPart, int]:
    if type(adjustment_weight) is not int or not 0 <= adjustment_weight <= MAX_ADJUSTMENT_WEIGHT:
        raise TimerOperationError("invalid", "Choose a valid timer adjustment weight.")
    part = _part(schedule, part_id)
    if (
        adjustment_weight > 0
        and adjustment_weight != part.adjustment_weight
        and part.planned_seconds < MIN_PLANNED_PART_SECONDS
    ):
        raise TimerOperationError("invalid", "An adjustable timer part must have at least 60 seconds.")
    return part, adjustment_weight


def _set_adjustment_weight(
    schedule: CustomTimerSchedule, *, part_id: str, adjustment_weight: int,
) -> None:
    part, validated = _validate_adjustment_weight(
        schedule, part_id=part_id, adjustment_weight=adjustment_weight,
    )
    part.adjustment_weight = validated


_EDITORS: dict[str, Callable[..., None]] = {
    "add_part": _add_part,
    "add_section": _add_section,
    "rename_part": _rename_part,
    "rename_section": _rename_section,
    "set_section_icon": _set_section_icon,
    "set_section_color": _set_section_color,
    "set_section_appearance": _set_section_appearance,
    "rename_schedule": _rename_schedule,
    "move_part": _move_part,
    "move_section": _move_section,
    "delete_part": _delete_part,
    "delete_section": _delete_section,
    "set_part_seconds": _set_part_seconds,
    "set_pacing_window": _set_pacing_window,
    "set_pacing_transition": _set_pacing_transition,
    "set_adjustment_weight": _set_adjustment_weight,
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
            section = TimerSection.new(entry.title, icon=entry.icon, color=entry.color)
            section.parts.extend(
                _duplicate_part(part, section.id)
                for part in entry.parts
            )
            duplicate.entries.append(section)
        else:
            duplicate.entries.append(_duplicate_part(entry, None))
    if (window := source.pacing_window) is not None:
        new_ids = {old.id: new.id for old, new in zip(source.iter_parts(), duplicate.iter_parts(), strict=True)}
        transitions = window.transitions
        before = tuple(
            (new_ids[part_id], seconds)
            for part_id, seconds in transitions.before_part_seconds if part_id in new_ids
        )
        duplicate.pacing_window = replace(window, transitions=replace(transitions, before_part_seconds=before))
    return duplicate


def _duplicate_part(part: TimerPart, section_id: str | None) -> TimerPart:
    duplicate = TimerPart.new(section_id, part.title, part.planned_seconds)
    duplicate.adjustment_weight = part.adjustment_weight
    return duplicate


def replace_custom_schedule_entries(
    schedule: CustomTimerSchedule,
    draft_entries: Sequence[dict[str, object]],
) -> None:
    """Apply an entire edited custom-schedule structure atomically.

    Existing parts and sections retain their stable IDs and recorded timing
    data, newly created draft entries receive fresh IDs, deleted parts are
    pruned from transition estimates, and all validation rules remain enforced.
    """
    if schedule.has_running_part():
        raise TimerOperationError("blocked", "Stop the timer before editing this schedule.")

    existing_parts = {part.id: part for part in schedule.iter_parts()}
    existing_sections = {
        entry.id: entry for entry in schedule.entries if isinstance(entry, TimerSection)
    }
    used_part_ids: set[str] = set()
    used_section_ids: set[str] = set()

    def build_part(raw: dict[str, object], section_id: str | None) -> TimerPart:
        raw_id = str(raw.get("id") or "")
        title = validate_title(str(raw.get("title") or ""))
        raw_planned = raw.get("plannedSeconds")
        planned_int = (
            raw_planned
            if type(raw_planned) is int
            else (int(raw_planned) if isinstance(raw_planned, float) and raw_planned.is_integer() else -1)
        )
        planned = _validate_seconds(planned_int)
        if raw_id and raw_id in existing_parts:
            if raw_id in used_part_ids:
                raise TimerOperationError("invalid", "Duplicate timer part in schedule.")
            used_part_ids.add(raw_id)
            part = existing_parts[raw_id]
            if planned != part.planned_seconds and part.state is not PartState.IDLE:
                raise TimerOperationError(
                    "blocked", "Reset the timer part before changing its duration."
                )
            part.section = section_id
            part.title = title
            part.planned_seconds = planned
            return part
        return TimerPart.new(section_id, title, planned)

    new_entries: list[TimerPart | TimerSection] = []
    for raw in draft_entries:
        if not isinstance(raw, dict):
            raise TimerOperationError("invalid", "Invalid timer schedule entry.")
        kind = raw.get("type")
        if kind == "part":
            new_entries.append(build_part(raw, None))
        elif kind == "section":
            raw_id = str(raw.get("id") or "")
            title = validate_title(str(raw.get("title") or ""))
            icon = _validate_section_icon(raw.get("icon", CustomSectionIcon.DEFAULT))
            color = _validate_section_color(raw.get("color"))
            if raw_id and raw_id in existing_sections:
                if raw_id in used_section_ids:
                    raise TimerOperationError("invalid", "Duplicate timer section in schedule.")
                used_section_ids.add(raw_id)
                sec_id = raw_id
            else:
                sec_id = TimerSection.new(title, icon=icon, color=color).id
            raw_parts = raw.get("parts")
            if not isinstance(raw_parts, list) or any(not isinstance(item, dict) for item in raw_parts):
                raise TimerOperationError("invalid", "Invalid section parts list.")
            parts = [build_part(item, sec_id) for item in raw_parts]
            new_entries.append(TimerSection(sec_id, title, parts, icon=icon, color=color))
        else:
            raise TimerOperationError("invalid", "Unknown timer schedule entry type.")

    schedule.entries = new_entries
    surviving_ids = {part.id for part in schedule.iter_parts()}
    if (window := schedule.pacing_window) is not None and window.transitions.before_part_seconds:
        before = tuple(
            (pid, secs)
            for pid, secs in window.transitions.before_part_seconds
            if pid in surviving_ids
        )
        if before != window.transitions.before_part_seconds:
            schedule.pacing_window = replace(
                window,
                transitions=replace(window.transitions, before_part_seconds=before),
            )


def replace_custom_timing_settings(
    schedule: CustomTimerSchedule,
    *,
    start_time: str | None,
    end_time: str | None,
    untracked_seconds: int | None = None,
    update_untracked: bool = False,
    weights: Mapping[str, int] | None = None,
    before_parts: Mapping[str, int | None] | None = None,
    closing_seconds: int | None = None,
    update_closing: bool = False,
) -> None:
    """Validate and apply window, part weights, and transition estimates atomically."""
    if schedule.has_running_part():
        raise TimerOperationError("blocked", "Stop the timer before editing this schedule.")
    if update_untracked and untracked_seconds is not None and (
        type(untracked_seconds) is not int or untracked_seconds < 0
    ):
        raise TimerOperationError("invalid", "Set a nonnegative duration for time outside parts.")

    staged_weights: list[tuple[TimerPart, int]] = []
    if weights is not None:
        for part_id, weight in weights.items():
            staged_weights.append(
                _validate_adjustment_weight(schedule, part_id=part_id, adjustment_weight=weight)
            )

    next_window: PacingWindow | None
    if start_time is None and end_time is None:
        has_explicit_transitions = (
            (update_untracked and untracked_seconds is not None)
            or (update_closing and closing_seconds is not None)
            or bool(before_parts and any(val is not None for val in before_parts.values()))
        )
        if has_explicit_transitions:
            raise TimerOperationError("invalid", "Set both the timer start and end.")
        next_window = None
    elif start_time is None or end_time is None:
        raise TimerOperationError("invalid", "Set both the timer start and end.")
    else:
        existing = schedule.pacing_window
        resolved_untracked = (
            untracked_seconds
            if update_untracked
            else (existing.untracked_seconds if existing else None)
        )
        before_dict = dict(existing.transitions.before_part_seconds) if existing else {}
        if before_parts is not None:
            for part_id, estimated in before_parts.items():
                _part(schedule, part_id)
                if estimated is None:
                    before_dict.pop(part_id, None)
                else:
                    if type(estimated) is not int or estimated < 0:
                        raise TimerOperationError(
                            "invalid", "Set a transition estimate within the timer window."
                        )
                    before_dict[part_id] = estimated

        resolved_closing = (
            closing_seconds
            if update_closing
            else (existing.transitions.closing_seconds if existing else None)
        )
        if resolved_closing is not None and (type(resolved_closing) is not int or resolved_closing < 0):
            raise TimerOperationError("invalid", "Set a transition estimate within the timer window.")

        try:
            transitions = PacingTransitions(tuple(before_dict.items()), resolved_closing)
            next_window = PacingWindow(
                start_time, end_time, resolved_untracked, transitions,
            )
        except ValueError as error:
            raise TimerOperationError("invalid", str(error)) from error

    for part, validated_weight in staged_weights:
        part.adjustment_weight = validated_weight
    schedule.pacing_window = next_window
