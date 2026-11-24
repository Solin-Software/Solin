"""Build default meeting schedules and edit their individual parts."""

from __future__ import annotations

from datetime import date

from .models import (
    MAX_PLANNED_PART_SECONDS,
    MIN_PLANNED_PART_SECONDS,
    TimerPart,
    MeetingSchedule,
    MeetingType,
    PartKind,
    PartState,
    Section,
)
from .part_titles import (
    BIBLE_READING_TITLE,
    CBS_TITLE,
    CONCLUDING_COMMENTS_TITLE,
    GENERIC_INDEXED_PART_TITLE,
    OPENING_COMMENTS_TITLE,
    PUBLIC_TALK_INDEXED_PART_TITLE,
    PUBLIC_TALK_TITLE,
    SPIRITUAL_GEMS_TITLE,
    TREASURES_INDEXED_PART_TITLE,
    TREASURES_TALK_TITLE,
    WATCHTOWER_STUDY_INDEXED_PART_TITLE,
    WATCHTOWER_STUDY_TITLE,
)

_TREASURES_DEFAULTS = [
    (TREASURES_TALK_TITLE, 10 * 60),
    (SPIRITUAL_GEMS_TITLE, 10 * 60),
    (BIBLE_READING_TITLE, 4 * 60),
]
_MIDWEEK_DEFAULT_COUNTS = {
    Section.TREASURES: len(_TREASURES_DEFAULTS),
    Section.MINISTRY: 3,
    Section.LIVING: 2,
}
_DEFAULT_PART_SECONDS = {
    Section.OPENING_COMMENTS: 60,
    Section.TREASURES: 5 * 60,
    Section.MINISTRY: 4 * 60,
    Section.LIVING: 7 * 60 + 30,
    Section.CONCLUDING_COMMENTS: 3 * 60,
    Section.PUBLIC_TALK: 30 * 60,
    Section.WATCHTOWER: 60 * 60,
}
_CBS_DEFAULT = 30 * 60


def _default_part(section: Section, index: int) -> TimerPart:
    if section is Section.TREASURES and index < len(_TREASURES_DEFAULTS):
        title, seconds = _TREASURES_DEFAULTS[index]
    else:
        title, seconds = _default_title(section, index), _DEFAULT_PART_SECONDS[section]
    return TimerPart.new(section, title, seconds)


def _cbs_part() -> TimerPart:
    return TimerPart.new(
        Section.LIVING,
        CBS_TITLE,
        _CBS_DEFAULT,
        kind=PartKind.CBS,
    )


def build_default_schedule(week_monday: date, meeting_type: MeetingType) -> MeetingSchedule:
    """Construct a fresh schedule with default parts for the given week/type."""
    if meeting_type is MeetingType.MIDWEEK:
        parts = [_default_part(Section.OPENING_COMMENTS, 0)]
        for section, count in _MIDWEEK_DEFAULT_COUNTS.items():
            parts.extend(_default_part(section, i) for i in range(count))
        parts.extend((_cbs_part(), _default_part(Section.CONCLUDING_COMMENTS, 0)))
    else:
        parts = [_default_part(section, 0) for section in (Section.PUBLIC_TALK, Section.WATCHTOWER)]
    return MeetingSchedule(week_monday=week_monday.isoformat(), meeting_type=meeting_type, parts=parts)


def _is_cbs_part(part: TimerPart) -> bool:
    return part.section is Section.LIVING and (part.kind is PartKind.CBS or part.title == CBS_TITLE)


def configurable_parts_in(schedule: MeetingSchedule, section: Section) -> list[TimerPart]:
    """Parts controlled by the section count."""
    if section is Section.LIVING:
        return [p for p in schedule.parts_in(section) if not _is_cbs_part(p)]
    return schedule.parts_in(section)


def configurable_count_for(schedule: MeetingSchedule, section: Section) -> int:
    return len(configurable_parts_in(schedule, section))


def is_part_duration_editable(part: TimerPart) -> bool:
    return part.state is PartState.IDLE


def set_part_seconds(part: TimerPart, seconds: int) -> bool:
    seconds = max(MIN_PLANNED_PART_SECONDS, min(int(seconds), MAX_PLANNED_PART_SECONDS))
    if not is_part_duration_editable(part) or part.planned_seconds == seconds:
        return False
    part.planned_seconds = seconds
    return True


def normalize_schedule(schedule: MeetingSchedule) -> bool:
    """Bring persisted schedules up to the current canonical timer shape."""
    changed = False

    if schedule.meeting_type is not MeetingType.MIDWEEK:
        return changed

    changed = _normalize_existing_cbs_parts(schedule) or changed

    if schedule_has_timing_data(schedule):
        return changed

    changed = _ensure_opening_comments_part(schedule) or changed
    changed = _ensure_cbs_part(schedule) or changed
    changed = _ensure_concluding_comments_part(schedule) or changed
    return changed


def schedule_has_timing_data(schedule: MeetingSchedule) -> bool:
    """True once any part has live or historical stopwatch data."""
    return any(_part_has_timing_data(part) for part in schedule.parts)


def _part_has_timing_data(part: TimerPart) -> bool:
    return (
        part.state is not PartState.IDLE
        or part.started_at_epoch is not None
        or part.first_started_epoch is not None
        or abs(part.accumulated_seconds) > 0.0001
    )


def _normalize_existing_cbs_parts(schedule: MeetingSchedule) -> bool:
    changed = False
    for part in schedule.parts:
        if _is_cbs_part(part) and part.kind is not PartKind.CBS:
            part.kind = PartKind.CBS
            changed = True
    return changed


def _ensure_opening_comments_part(schedule: MeetingSchedule) -> bool:
    if any(part.section is Section.OPENING_COMMENTS for part in schedule.parts):
        return False
    schedule.parts.insert(
        _default_insert_index(schedule, Section.OPENING_COMMENTS),
        _default_part(Section.OPENING_COMMENTS, 0),
    )
    return True


def _ensure_cbs_part(schedule: MeetingSchedule) -> bool:
    if any(_is_cbs_part(part) for part in schedule.parts):
        return False
    insert_at = _default_insert_index(schedule, Section.LIVING)
    living_indices = [i for i, p in enumerate(schedule.parts) if p.section is Section.LIVING]
    if living_indices:
        insert_at = living_indices[-1] + 1
    schedule.parts.insert(insert_at, _cbs_part())
    return True


def _ensure_concluding_comments_part(schedule: MeetingSchedule) -> bool:
    if any(part.section is Section.CONCLUDING_COMMENTS for part in schedule.parts):
        return False
    insert_at = _default_insert_index(schedule, Section.CONCLUDING_COMMENTS)
    living_indices = [i for i, p in enumerate(schedule.parts) if p.section is Section.LIVING]
    if living_indices:
        insert_at = living_indices[-1] + 1
    schedule.parts.insert(insert_at, _default_part(Section.CONCLUDING_COMMENTS, 0))
    return True


def set_section_part_count(schedule: MeetingSchedule, section: Section, count: int) -> None:
    """Resize the configurable span, preserving the surviving parts and their times."""
    count = max(1, int(count))
    existing = configurable_parts_in(schedule, section)
    if len(existing) == count:
        return

    indices = [i for i, part in enumerate(schedule.parts) if part.section is section]
    insert_at = indices[0] if indices else _default_insert_index(schedule, section)
    existing_ids = {part.id for part in existing}
    parts = existing[:count]
    parts.extend(_default_part(section, i) for i in range(len(existing), count))
    schedule.parts = [part for part in schedule.parts if part.id not in existing_ids]
    schedule.parts[insert_at:insert_at] = parts


def _default_title(section: Section, index: int) -> str:
    if section in {Section.MINISTRY, Section.LIVING}:
        return GENERIC_INDEXED_PART_TITLE

    if section is Section.TREASURES:
        if index < len(_TREASURES_DEFAULTS):
            return _TREASURES_DEFAULTS[index][0]
        return TREASURES_INDEXED_PART_TITLE

    # Single-part sections read naturally without an index suffix.
    singles = {
        Section.OPENING_COMMENTS: OPENING_COMMENTS_TITLE,
        Section.PUBLIC_TALK: PUBLIC_TALK_TITLE,
        Section.WATCHTOWER: WATCHTOWER_STUDY_TITLE,
        Section.CONCLUDING_COMMENTS: CONCLUDING_COMMENTS_TITLE,
    }
    if section in singles and index == 0:
        return singles[section]

    indexed_titles = {
        Section.PUBLIC_TALK: PUBLIC_TALK_INDEXED_PART_TITLE,
        Section.WATCHTOWER: WATCHTOWER_STUDY_INDEXED_PART_TITLE,
    }
    return indexed_titles.get(section, GENERIC_INDEXED_PART_TITLE)


def _default_insert_index(schedule: MeetingSchedule, section: Section) -> int:
    """Where a brand-new section span should go to keep canonical order."""
    order = [
        Section.OPENING_COMMENTS,
        Section.TREASURES,
        Section.MINISTRY,
        Section.LIVING,
        Section.CONCLUDING_COMMENTS,
        Section.PUBLIC_TALK,
        Section.WATCHTOWER,
    ]
    target_rank = order.index(section)
    for i, p in enumerate(schedule.parts):
        if isinstance(p.section, Section) and order.index(p.section) > target_rank:
            return i
    return len(schedule.parts)
