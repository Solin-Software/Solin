"""
meeting_weeks.py - Solin
====================================
Pure meeting week and publication issue calculations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True, slots=True)
class MeetingWeekWindow:
    """Shared navigation and persistence window around the current week."""

    previous_weeks: int
    next_weeks: int


MEETING_WEEK_WINDOW = MeetingWeekWindow(
    previous_weeks=2,
    next_weeks=5,
)


def monday_of_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def current_monday(today: date | None = None) -> date:
    return monday_of_week(today or date.today())


def meeting_week_bounds(today: date | None = None) -> tuple[date, date]:
    """Return the inclusive oldest/newest weeks exposed by the application."""

    anchor = current_monday(today)
    return (
        anchor - timedelta(weeks=MEETING_WEEK_WINDOW.previous_weeks),
        anchor + timedelta(weeks=MEETING_WEEK_WINDOW.next_weeks),
    )


def selectable_meeting_weeks(today: date | None = None) -> tuple[date, ...]:
    """Return every meeting week exposed by week navigation, oldest first."""

    oldest, newest = meeting_week_bounds(today)
    count = (newest - oldest).days // 7 + 1
    return tuple(
        oldest + timedelta(weeks=offset)
        for offset in range(count)
    )


def is_selectable_meeting_week(
    monday: date,
    *,
    today: date | None = None,
) -> bool:
    oldest, newest = meeting_week_bounds(today)
    return monday.weekday() == 0 and oldest <= monday <= newest


def mwb_issue_for_week(monday: date) -> str:
    month = monday.month
    if month % 2 == 0:
        month -= 1
    return f"{monday.year}{month:02d}00"


def watchtower_issue_candidates(monday: date) -> list[str]:
    return [
        f"{(monday - timedelta(weeks=weeks)).year}"
        f"{(monday - timedelta(weeks=weeks)).month:02d}00"
        for weeks in (6, 8, 10, 12)
    ]


__all__ = [
    "MEETING_WEEK_WINDOW",
    "MeetingWeekWindow",
    "current_monday",
    "is_selectable_meeting_week",
    "meeting_week_bounds",
    "monday_of_week",
    "mwb_issue_for_week",
    "selectable_meeting_weeks",
    "watchtower_issue_candidates",
]
