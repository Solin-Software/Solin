"""
meeting_weeks.py - Solin
====================================
Pure meeting week and publication issue calculations.
"""

from __future__ import annotations

from datetime import date, timedelta

MEETING_WEEK_PAST_LIMIT = 2
MEETING_WEEK_FUTURE_LIMIT = 5


def monday_of_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def current_monday(today: date | None = None) -> date:
    return monday_of_week(today or date.today())


def selectable_meeting_weeks(today: date | None = None) -> tuple[date, ...]:
    """Return every meeting week exposed by week navigation, oldest first."""

    anchor = current_monday(today)
    return tuple(
        anchor + timedelta(weeks=offset)
        for offset in range(-MEETING_WEEK_PAST_LIMIT, MEETING_WEEK_FUTURE_LIMIT + 1)
    )


def is_selectable_meeting_week(
    monday: date,
    *,
    today: date | None = None,
) -> bool:
    return monday in selectable_meeting_weeks(today)


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
    "MEETING_WEEK_FUTURE_LIMIT",
    "MEETING_WEEK_PAST_LIMIT",
    "current_monday",
    "is_selectable_meeting_week",
    "monday_of_week",
    "mwb_issue_for_week",
    "selectable_meeting_weeks",
    "watchtower_issue_candidates",
]
