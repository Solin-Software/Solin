"""Shared presentation of timer schedules for desktop, remote, and reports."""

from __future__ import annotations

from ..meetings.colors import section_colors, section_colors_from_hex
from ..meetings.section_meta import SECTION_META
from ..timer.models import (
    CONFIGURABLE_PART_SECTIONS,
    CustomSectionIcon,
    CustomTimerSchedule,
    MeetingSchedule,
    PartState,
    Section,
    TimerSection,
)
from ..timer.part_titles import is_indexed_part_title_source
from ..timer.schedule_factory import configurable_count_for, is_part_duration_editable
from ...styles.theme import current_theme_scheme
from .date import format_time_with_seconds
from .meeting_sections import translate_meeting_section_title
from .timer_part_titles import display_part_title

_SECTION_CODES = {
    Section.OPENING_COMMENTS: "opening_comments",
    Section.TREASURES: "tgw",
    Section.MINISTRY: "ayfm",
    Section.LIVING: "lac",
    Section.CONCLUDING_COMMENTS: "concluding_comments",
    Section.PUBLIC_TALK: "public_talk",
    Section.WATCHTOWER: "wt",
}

_STANDALONE_SECTIONS = {
    Section.OPENING_COMMENTS,
    Section.CONCLUDING_COMMENTS,
}

CUSTOM_SECTION_DEFAULT_ICON_KEY = "section_group"

# Canonical meeting sections resolve through the same semantic icon catalog
# as custom sections, keeping one unified icon set across all schedules.
_OFFICIAL_SECTION_ICONS: dict[Section, CustomSectionIcon] = {
    Section.TREASURES: CustomSectionIcon.TREASURES,
    Section.MINISTRY: CustomSectionIcon.MINISTRY,
    Section.LIVING: CustomSectionIcon.LIVING,
    Section.PUBLIC_TALK: CustomSectionIcon.PUBLIC_TALK,
    Section.WATCHTOWER: CustomSectionIcon.WATCHTOWER,
}

_SECTION_ICON_KEYS: dict[CustomSectionIcon, str] = {
    CustomSectionIcon.DEFAULT: CUSTOM_SECTION_DEFAULT_ICON_KEY,
    CustomSectionIcon.NONE: "",
    CustomSectionIcon.TREASURES: "treasures",
    CustomSectionIcon.MINISTRY: "ministry",
    CustomSectionIcon.LIVING: "living",
    CustomSectionIcon.PUBLIC_TALK: "public_talk",
    CustomSectionIcon.WATCHTOWER: "watchtower",
    CustomSectionIcon.BOOK: "book",
    CustomSectionIcon.PEOPLE: "people",
    CustomSectionIcon.MICROPHONE: "microphone",
    CustomSectionIcon.MUSIC: "music",
    CustomSectionIcon.VIDEO: "video",
    CustomSectionIcon.CALENDAR: "calendar",
    CustomSectionIcon.CLOCK: "clock",
    CustomSectionIcon.MARKER: "marker",
    CustomSectionIcon.CHECK: "check",
}


def section_icon_key(
    section: Section | str | None,
    custom_icon: CustomSectionIcon | None = None,
) -> str:
    """Resolve a canonical or custom section choice to its visual icon key."""
    if isinstance(section, Section):
        official_icon = _OFFICIAL_SECTION_ICONS.get(section)
        return _SECTION_ICON_KEYS.get(official_icon, "") if official_icon is not None else ""
    if section is None or custom_icon is None:
        return ""
    return _SECTION_ICON_KEYS.get(custom_icon, CUSTOM_SECTION_DEFAULT_ICON_KEY)


def section_palette(
    section: Section | str | None,
    custom_color: str | None = None,
) -> dict[str, str]:
    scheme = current_theme_scheme()
    if isinstance(section, Section):
        code = _SECTION_CODES.get(section, "")
        hue = SECTION_META.get(code, ("", 215))[1]
        colors = section_colors(hue, scheme)
    elif custom_color:
        colors = section_colors_from_hex(custom_color, scheme)
    else:
        colors = section_colors(215, scheme)
    return {
        "accent": colors["accent"],
        "text": colors["text"],
        "badge": colors["badge"],
        "border": colors["border"],
    }


def format_mmss(seconds: float) -> str:
    """Format a duration in seconds as MM:SS or H:MM:SS."""
    total = int(round(abs(seconds)))
    sign = "-" if seconds < 0 else ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{sign}{h}:{m:02d}:{s:02d}"
    return f"{sign}{m:02d}:{s:02d}"


def format_total_duration(seconds: float) -> str:
    """A compact section-total label: '30 min' on whole minutes, else 'MM:SS'."""
    s = int(round(seconds))
    if s % 60 == 0:
        return f"{s // 60} min"
    return format_mmss(s)


def _started_label(epoch: float | None, time_format: str = "HH:mm:ss") -> str:
    if epoch is None:
        return ""
    return format_time_with_seconds(epoch, time_format)


def section_title(section: Section | str | None, custom_title: str = "") -> str:
    """Return the localized official title or the literal custom section title."""
    if not isinstance(section, Section):
        return custom_title
    code = _SECTION_CODES.get(section, "")
    source = SECTION_META.get(code, (section.value, 215))[0]
    return translate_meeting_section_title(source)


def parts_model_for_schedule(
    schedule: MeetingSchedule | CustomTimerSchedule, time_format: str = "HH:mm:ss",
) -> list[dict]:
    """Build ordered presentation rows from either kind of timer schedule."""
    is_meeting = isinstance(schedule, MeetingSchedule)
    allow_duration_edits = is_meeting or not schedule.has_running_part()
    sections = (
        {entry.id: entry for entry in schedule.entries if isinstance(entry, TimerSection)}
        if isinstance(schedule, CustomTimerSchedule) else {}
    )
    parts = schedule.parts
    totals: dict[str | None, int] = {}
    counts: dict[str | None, int] = {}
    for part in parts:
        section = part.section if is_meeting else part.section_id
        totals[section] = totals.get(section, 0) + part.planned_seconds
        counts[section] = counts.get(section, 0) + 1

    configurable_counts: dict[str | None, int] = (
        {
            section: configurable_count_for(schedule, section)
            for section in totals if isinstance(section, Section)
        }
        if isinstance(schedule, MeetingSchedule) else counts
    )
    rows = []
    seen_sections: set[object] = set()
    section_positions: dict[object, int] = {}
    for number, part in enumerate(parts, start=1):
        section = part.section if is_meeting else part.section_id
        section_position = section_positions.get(section, 0) + 1
        section_positions[section] = section_position
        first = section not in seen_sections
        seen_sections.add(section)
        section_value = section.value if isinstance(section, Section) else (section or "")
        custom_section = sections.get(section_value)
        palette = section_palette(section, custom_section.color if custom_section else None)
        indexed_number = section_position if is_indexed_part_title_source(part.title) else None
        show_section_header = (
            section not in _STANDALONE_SECTIONS if is_meeting else section is not None
        )
        title = section_title(section, custom_section.title if custom_section else "")
        icon_key = section_icon_key(section, custom_section.icon if custom_section else None)
        rows.append({
            "id": part.id,
            "section": section_value,
            "sectionKey": section_value,
            "sectionIconKey": icon_key,
            "sectionIconChoice": custom_section.icon.value if custom_section else "",
            "sectionCustomColor": (custom_section.color or "") if custom_section else "",
            "sectionTitle": title,
            "sectionColor": palette["accent"],
            "sectionTextColor": palette["text"],
            "sectionBadgeBg": palette["badge"],
            "sectionBorderColor": palette["border"],
            "title": display_part_title(part, indexed_number=indexed_number),
            "hasTitleOverride": part.title_override is not None,
            "displayNumber": number,
            "plannedSeconds": part.planned_seconds,
            "plannedLabel": format_mmss(part.planned_seconds),
            "effectiveSeconds": part.effective_seconds,
            "effectiveLabel": format_mmss(part.effective_seconds),
            "hasAdjustedTarget": part.effective_seconds != part.planned_seconds,
            "adjustmentWeight": part.adjustment_weight,
            "state": part.state.value,
            "firstOfSection": first,
            "showSectionHeader": show_section_header,
            "configurableCount": is_meeting and section in CONFIGURABLE_PART_SECTIONS,
            "sectionCount": configurable_counts.get(section, 0),
            "sectionTotalLabel": format_total_duration(totals[section]) if show_section_header else "",
            "durationEditable": allow_duration_edits and is_part_duration_editable(part),
            "startedLabel": _started_label(part.first_started_epoch, time_format),
            "resultLabel": format_mmss(part.accumulated_seconds) if part.state is PartState.STOPPED else "",
            "overTarget": (
                part.state is PartState.STOPPED
                and round(part.accumulated_seconds - part.effective_seconds) > 0
            ),
        })
    return rows
