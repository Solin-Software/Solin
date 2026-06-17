"""
meeting_weeks.py - Solin
====================================
Pure meeting week and publication issue calculations.
"""

from __future__ import annotations

from datetime import date, timedelta


def monday_of_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def current_monday(today: date | None = None) -> date:
    return monday_of_week(today or date.today())


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
    "current_monday",
    "monday_of_week",
    "mwb_issue_for_week",
    "watchtower_issue_candidates",
]
