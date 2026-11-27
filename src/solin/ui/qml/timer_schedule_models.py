"""Presentation models for the QML timer schedule catalog, list and editor."""

from __future__ import annotations

from collections.abc import Iterable

from solin.core.i18n.timer_schedule_view import (
    format_mmss,
    format_total_duration,
    parts_model_for_schedule,
    section_appearance,
    section_icon_key,
    section_palette,
)
from solin.core.timer.models import (
    MIN_PLANNED_PART_SECONDS,
    CustomTimerSchedule,
    MeetingSchedule,
    PartState,
    TimerPart,
    TimerSection,
)
from solin.core.timer.schedule_factory import schedule_has_timing_data


def display_entries_model(
    schedule: MeetingSchedule | CustomTimerSchedule, time_format: str,
) -> list[dict[str, object]]:
    """Presentation rows including empty custom sections in structural order."""
    part_rows = parts_model_for_schedule(schedule, time_format)
    if not isinstance(schedule, CustomTimerSchedule):
        return [dict(row, rowType="part") for row in part_rows]

    by_id: dict[str, dict[str, object]] = {row["id"]: dict(row, rowType="part") for row in part_rows}
    result: list[dict[str, object]] = []
    for entry in schedule.entries:
        if isinstance(entry, TimerSection):
            if entry.parts:
                for part in entry.parts:
                    if part.id in by_id:
                        result.append(by_id[part.id])
            else:
                result.append({
                    "rowType": "empty_section",
                    "id": entry.id,
                    "section": entry.id,
                    "sectionKey": entry.id,
                    **section_appearance(entry.id, entry),
                    "firstOfSection": True,
                    "showSectionHeader": True,
                    "configurableCount": False,
                    "sectionCount": 0,
                    "sectionTotalLabel": "",
                })
        elif entry.id in by_id:
            result.append(by_id[entry.id])
    return result


def custom_schedules_model(schedules: Iterable[CustomTimerSchedule]) -> list[dict[str, object]]:
    return [custom_catalog_item(schedule) for schedule in schedules]


def custom_catalog_item(schedule: CustomTimerSchedule) -> dict[str, object]:
    return {
        "id": schedule.id,
        "title": schedule.title,
        "revision": schedule.revision,
        "partCount": len(schedule.parts),
        "totalLabel": format_total_duration(schedule.planned_total_seconds()),
        "hasTimingData": schedule_has_timing_data(schedule),
    }


def custom_schedule_model(schedule: CustomTimerSchedule) -> dict[str, object]:
    running = schedule.has_running_part()
    window = schedule.pacing_window
    if window is not None:
        window_model: dict[str, object] = {
            "configured": True,
            "startTime": window.start_time,
            "endTime": window.end_time,
            "untrackedSeconds": (
                window.untracked_seconds if window.untracked_seconds is not None else -1
            ),
            "closingSeconds": (
                window.transitions.closing_seconds
                if window.transitions.closing_seconds is not None
                else -1
            ),
            "beforeParts": dict(window.transitions.before_part_seconds),
        }
    else:
        window_model = {
            "configured": False,
            "startTime": "",
            "endTime": "",
            "untrackedSeconds": -1,
            "closingSeconds": -1,
            "beforeParts": {},
        }

    def encode_part(part: TimerPart, section_id_str: str) -> dict[str, object]:
        return {
            "type": "part",
            "id": part.id,
            "title": part.title,
            "plannedSeconds": part.planned_seconds,
            "plannedLabel": format_mmss(part.planned_seconds),
            "state": part.state.value,
            "durationEditable": part.state is PartState.IDLE and not running,
            "eligibleForPacing": part.planned_seconds >= MIN_PLANNED_PART_SECONDS,
            "adjustmentWeight": part.adjustment_weight,
            "sectionId": section_id_str,
        }

    entries: list[dict[str, object]] = []
    for entry in schedule.entries:
        if isinstance(entry, TimerSection):
            palette = section_palette(entry.id, entry.color)
            entries.append({
                "type": "section",
                "id": entry.id,
                "title": entry.title,
                "icon": entry.icon.value,
                "iconKey": section_icon_key(entry.id, entry.icon),
                "color": entry.color or "",
                "accentColor": palette["accent"],
                "textColor": palette["text"],
                "badgeBg": palette["badge"],
                "borderColor": palette["border"],
                "partCount": len(entry.parts),
                "totalLabel": format_total_duration(sum(p.planned_seconds for p in entry.parts)),
                "parts": [encode_part(part, entry.id) for part in entry.parts],
            })
        else:
            entries.append(encode_part(entry, ""))

    planned_total = schedule.planned_total_seconds()
    return {
        "id": schedule.id,
        "title": schedule.title,
        "revision": schedule.revision,
        "plannedTotalSeconds": planned_total,
        "plannedTotalLabel": format_total_duration(planned_total),
        "hasTimingData": schedule_has_timing_data(schedule),
        "hasAnyExplicitWeights": any(part.adjustment_weight > 0 for part in schedule.iter_parts()),
        "pacingWindow": window_model,
        "entries": entries,
    }
