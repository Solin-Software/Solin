from __future__ import annotations

from datetime import date

from solin.core.meetings.meeting_weeks import (
    current_monday,
    monday_of_week,
    mwb_issue_for_week,
    watchtower_issue_candidates,
)


def test_monday_of_week_returns_week_start():
    assert monday_of_week(date(2026, 6, 17)) == date(2026, 6, 15)


def test_current_monday_accepts_explicit_date():
    assert current_monday(date(2026, 6, 21)) == date(2026, 6, 15)


def test_mwb_issue_uses_previous_month_for_even_months():
    assert mwb_issue_for_week(date(2026, 6, 15)) == "20260500"
    assert mwb_issue_for_week(date(2026, 7, 6)) == "20260700"


def test_watchtower_issue_candidates_walk_back_expected_offsets():
    assert watchtower_issue_candidates(date(2026, 6, 15)) == [
        "20260500",
        "20260400",
        "20260400",
        "20260300",
    ]
