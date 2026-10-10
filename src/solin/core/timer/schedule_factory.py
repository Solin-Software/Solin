"""
schedule_factory.py — Solin timer domain
=======================================
Build default meeting schedules and keep section totals consistent when the
operator edits part durations or the number of parts.

Two sections have a fixed total (see ``FIXED_TOTAL_SECONDS``):
    • Ministry ("Apply Yourself to the Field Ministry") → 12 min.
    • Living ("Living as Christians", before the congregation Bible study) → 15 min.

Changing a part's duration makes the following part absorb the difference
so the section total stays constant (the part below responds to the change).
All functions here are pure (no Qt) and unit-tested in isolation.
"""

from __future__ import annotations

from datetime import date

from .models import (
    FIXED_TOTAL_SECONDS,
    MeetingPart,
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

# Minimum a single part may shrink to while redistributing (1 minute).
_MIN_PART_SECONDS = 60

# Default part counts for the user-configurable sections.
DEFAULT_COUNTS: dict[Section, int] = {
    Section.TREASURES: 3,
    Section.MINISTRY: 3,
    Section.LIVING: 2,
    Section.PUBLIC_TALK: 1,
    Section.WATCHTOWER: 1,
}

# Default per-part durations (seconds) for sections WITHOUT a fixed total.
# Treasures parts are independently editable (no redistribution).
_TREASURES_DEFAULTS = [
    (TREASURES_TALK_TITLE, 10 * 60),
    (SPIRITUAL_GEMS_TITLE, 10 * 60),
    (BIBLE_READING_TITLE, 4 * 60),
]
_PUBLIC_TALK_DEFAULT = 30 * 60
_WATCHTOWER_DEFAULT = 60 * 60
_OPENING_COMMENTS_DEFAULT = 1 * 60
_CBS_DEFAULT = 30 * 60
_CONCLUDING_COMMENTS_DEFAULT = 3 * 60


# ── Defaults ──────────────────────────────────────────────────────────────────

def _even_split(total: int, count: int) -> list[int]:
    """Split ``total`` seconds into ``count`` parts as evenly as possible.

    Any remainder is pushed onto the earlier parts so the sum is exact.
    """
    count = max(1, count)
    base = total // count
    rem = total - base * count
    return [base + (1 if i < rem else 0) for i in range(count)]


def _ministry_parts(count: int) -> list[MeetingPart]:
    durations = _even_split(FIXED_TOTAL_SECONDS[Section.MINISTRY], count)
    return [
        MeetingPart.new(Section.MINISTRY, _indexed_part_title(i), d)
        for i, d in enumerate(durations)
    ]


def _living_parts(count: int) -> list[MeetingPart]:
    durations = _even_split(FIXED_TOTAL_SECONDS[Section.LIVING], count)
    return [
        MeetingPart.new(Section.LIVING, _indexed_part_title(i), d)
        for i, d in enumerate(durations)
    ]


def _cbs_part() -> MeetingPart:
    return MeetingPart.new(
        Section.LIVING,
        CBS_TITLE,
        _CBS_DEFAULT,
        kind=PartKind.CBS,
    )


def _opening_comments_part() -> MeetingPart:
    return MeetingPart.new(
        Section.OPENING_COMMENTS,
        OPENING_COMMENTS_TITLE,
        _OPENING_COMMENTS_DEFAULT,
    )


def _concluding_comments_part() -> MeetingPart:
    return MeetingPart.new(
        Section.CONCLUDING_COMMENTS,
        CONCLUDING_COMMENTS_TITLE,
        _CONCLUDING_COMMENTS_DEFAULT,
    )


def _treasures_parts(count: int) -> list[MeetingPart]:
    parts: list[MeetingPart] = []
    for i in range(count):
        if i < len(_TREASURES_DEFAULTS):
            title, secs = _TREASURES_DEFAULTS[i]
        else:
            title, secs = (_default_title(Section.TREASURES, i), 5 * 60)
        parts.append(MeetingPart.new(Section.TREASURES, title, secs))
    return parts


def build_default_schedule(week_monday: date, meeting_type: MeetingType) -> MeetingSchedule:
    """Construct a fresh schedule with default parts for the given week/type."""
    parts: list[MeetingPart] = []
    if meeting_type is MeetingType.MIDWEEK:
        parts += [_opening_comments_part()]
        parts += _treasures_parts(DEFAULT_COUNTS[Section.TREASURES])
        parts += _ministry_parts(DEFAULT_COUNTS[Section.MINISTRY])
        parts += _living_parts(DEFAULT_COUNTS[Section.LIVING])
        parts += [_cbs_part()]
        parts += [_concluding_comments_part()]
    else:  # WEEKEND
        parts += [MeetingPart.new(Section.PUBLIC_TALK, PUBLIC_TALK_TITLE, _PUBLIC_TALK_DEFAULT)]
        parts += [MeetingPart.new(Section.WATCHTOWER, WATCHTOWER_STUDY_TITLE, _WATCHTOWER_DEFAULT)]
    return MeetingSchedule(
        week_monday=week_monday.isoformat(),
        meeting_type=meeting_type,
        parts=parts,
    )


# ── Redistribution (fixed-total sections) ─────────────────────────────────────

def _is_cbs_part(part: MeetingPart) -> bool:
    return (
        part.section is Section.LIVING
        and (part.kind is PartKind.CBS or part.title == CBS_TITLE)
    )


def configurable_parts_in(schedule: MeetingSchedule, section: Section) -> list[MeetingPart]:
    """Parts controlled by section count and fixed-total redistribution."""
    if section is Section.LIVING:
        return [p for p in schedule.parts_in(section) if not _is_cbs_part(p)]
    return schedule.parts_in(section)


def configurable_count_for(schedule: MeetingSchedule, section: Section) -> int:
    return len(configurable_parts_in(schedule, section))


def configurable_total_seconds(schedule: MeetingSchedule, section: Section) -> int:
    return sum(p.planned_seconds for p in configurable_parts_in(schedule, section))


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


def _part_has_timing_data(part: MeetingPart) -> bool:
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
        _opening_comments_part(),
    )
    return True


def _ensure_cbs_part(schedule: MeetingSchedule) -> bool:
    if any(_is_cbs_part(part) for part in schedule.parts):
        return False
    insert_at = _default_insert_index(schedule, Section.LIVING)
    living_indices = [
        i for i, p in enumerate(schedule.parts)
        if p.section is Section.LIVING
    ]
    if living_indices:
        insert_at = living_indices[-1] + 1
    schedule.parts.insert(insert_at, _cbs_part())
    return True


def _ensure_concluding_comments_part(schedule: MeetingSchedule) -> bool:
    if any(part.section is Section.CONCLUDING_COMMENTS for part in schedule.parts):
        return False
    insert_at = _default_insert_index(schedule, Section.CONCLUDING_COMMENTS)
    living_indices = [
        i for i, p in enumerate(schedule.parts)
        if p.section is Section.LIVING
    ]
    if living_indices:
        insert_at = living_indices[-1] + 1
    schedule.parts.insert(insert_at, _concluding_comments_part())
    return True


def _apply_delta_chain(parts: list[MeetingPart], start_index: int, order: list[int],
                       delta: int) -> None:
    """Absorb ``delta`` seconds across ``order`` (a list of indices into ``parts``),
    clamping each part to ``_MIN_PART_SECONDS`` and carrying the overflow on.

    A positive ``delta`` means those parts must collectively *lose* ``delta``
    seconds (because the edited part grew); negative means they gain.
    """
    remaining = delta
    for idx in order:
        if remaining == 0:
            break
        current = parts[idx].planned_seconds
        # We want to subtract ``remaining`` from this part (positive remaining
        # shrinks it). Clamp at the minimum.
        new_val = current - remaining
        if new_val < _MIN_PART_SECONDS:
            absorbed = current - _MIN_PART_SECONDS
            parts[idx].planned_seconds = _MIN_PART_SECONDS
            remaining -= absorbed
        else:
            parts[idx].planned_seconds = new_val
            remaining = 0
    # If we still could not place all of it (everyone hit the floor), dump the
    # leftover back onto the very first redistributable part so the total holds.
    if remaining != 0 and order:
        parts[order[0]].planned_seconds = max(
            _MIN_PART_SECONDS, parts[order[0]].planned_seconds - remaining
        )


def redistribute_section(schedule: MeetingSchedule, part_id: str,
                         new_planned_seconds: int) -> None:
    """Set ``part_id`` to ``new_planned_seconds`` and rebalance its section.

    For fixed-total sections the change is absorbed by the following part(s)
    (or preceding ones when editing the last part) so the section total is
    preserved. For other sections the part is simply set.
    """
    part = schedule.part_by_id(part_id)
    if part is None:
        return
    new_planned_seconds = max(_MIN_PART_SECONDS, int(new_planned_seconds))
    section = part.section

    if section not in FIXED_TOTAL_SECONDS:
        part.planned_seconds = new_planned_seconds
        return

    section_parts = configurable_parts_in(schedule, section)
    if part not in section_parts:
        part.planned_seconds = new_planned_seconds
        return
    if len(section_parts) == 1:
        # Nothing to balance against — force the fixed total.
        part.planned_seconds = FIXED_TOTAL_SECONDS[section]
        return

    local_index = section_parts.index(part)
    delta = new_planned_seconds - part.planned_seconds
    if delta == 0:
        return

    # The other parts can only yield down to the floor; cap the requested growth
    # so the section total is *always* preserved (a part can't grow past what the
    # rest can give up).
    others = [p for i, p in enumerate(section_parts) if i != local_index]
    if delta > 0:
        capacity = sum(p.planned_seconds - _MIN_PART_SECONDS for p in others)
        delta = min(delta, capacity)
    if delta == 0:
        return

    part.planned_seconds += delta

    # Absorb the delta, preferring parts after the edited one (the part "below"
    # responds), wrapping to earlier parts only when editing the last one.
    after = list(range(local_index + 1, len(section_parts)))
    before = list(range(local_index - 1, -1, -1))
    order = after + before if after else before
    _apply_delta_chain(section_parts, local_index, order, delta)
    # ``section_parts`` holds references into ``schedule.parts`` → mutation done.


def adjust_part(schedule: MeetingSchedule, part_id: str, delta_seconds: int) -> None:
    """Convenience: nudge a part by ``delta_seconds`` (e.g. ±60 for ±1 min)."""
    part = schedule.part_by_id(part_id)
    if part is None:
        return
    redistribute_section(schedule, part_id, part.planned_seconds + int(delta_seconds))


# ── Part-count configuration ──────────────────────────────────────────────────

def set_section_part_count(schedule: MeetingSchedule, section: Section, count: int) -> None:
    """Resize a section to ``count`` parts in place.

    Existing parts (and their live run-state) are preserved where the count
    overlaps; the tail is grown/trimmed. Fixed-total sections are re-split
    evenly so the section total is exactly preserved.
    """
    count = max(1, int(count))
    existing = configurable_parts_in(schedule, section)
    if len(existing) == count and section not in FIXED_TOTAL_SECONDS:
        return

    # Locate the contiguous span of configurable parts within schedule.parts.
    existing_ids = {p.id for p in existing}
    indices = [i for i, p in enumerate(schedule.parts) if p.id in existing_ids]
    if indices:
        insert_at = indices[0]
    else:
        section_indices = [
            i for i, p in enumerate(schedule.parts)
            if p.section is section
        ]
        insert_at = section_indices[0] if section_indices else _default_insert_index(schedule, section)

    # Build the new part list for the section.
    if section in FIXED_TOTAL_SECONDS:
        durations = _even_split(FIXED_TOTAL_SECONDS[section], count)
        new_parts: list[MeetingPart] = []
        for i in range(count):
            if i < len(existing):
                p = existing[i]
                p.planned_seconds = durations[i]
                new_parts.append(p)
            else:
                new_parts.append(MeetingPart.new(
                    section, _default_title(section, i), durations[i]))
    else:
        builder = {
            Section.TREASURES: _treasures_parts,
            Section.PUBLIC_TALK: lambda c: _named_parts(section, c, _PUBLIC_TALK_DEFAULT),
            Section.WATCHTOWER: lambda c: _named_parts(section, c, _WATCHTOWER_DEFAULT),
        }.get(section)
        fresh = builder(count) if builder else _named_parts(section, count, 5 * 60)
        new_parts = []
        for i in range(count):
            if i < len(existing):
                new_parts.append(existing[i])  # keep duration + run-state
            else:
                new_parts.append(fresh[i])

    # Splice: remove old configurable parts, insert the new span at the same spot.
    schedule.parts = [p for p in schedule.parts if p.id not in existing_ids]
    schedule.parts[insert_at:insert_at] = new_parts


def _named_parts(section: Section, count: int, default_secs: int) -> list[MeetingPart]:
    return [
        MeetingPart.new(section, _default_title(section, i), default_secs)
        for i in range(count)
    ]


def _default_title(section: Section, index: int) -> str:
    if section in {Section.MINISTRY, Section.LIVING}:
        return _indexed_part_title(index)

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


def _indexed_part_title(index: int) -> str:
    return GENERIC_INDEXED_PART_TITLE


def _default_insert_index(schedule: MeetingSchedule, section: Section) -> int:
    """Where a brand-new section span should go to keep canonical order."""
    order = [
        Section.OPENING_COMMENTS,
        Section.TREASURES,
        Section.MINISTRY,
        Section.LIVING,
        Section.CONCLUDING_COMMENTS,
        Section.PUBLIC_TALK, Section.WATCHTOWER,
    ]
    target_rank = order.index(section)
    for i, p in enumerate(schedule.parts):
        if order.index(p.section) > target_rank:
            return i
    return len(schedule.parts)
