from __future__ import annotations

import pytest

from solin.core.jw import congregation_lookup
from solin.core.jw.congregation_lookup import (
    CongregationMatch,
    fetch_meeting_schedule,
    search_congregations,
)

pytestmark = pytest.mark.unit


_SUGGESTIONS = [
    {
        "congregationGuid": "guid-central",
        "formattedName": "Central - Curitiba PR (46789)",
        "name": "Central - Curitiba PR",
    },
    {"congregationGuid": "", "name": "Sem guid"},
    "not a mapping",
]

_MEETING_SEARCH = {
    "items": [
        {
            "congregationMeetings": [
                {
                    "id": "guid-other",
                    "name": "Outra",
                    "midweekMeetingDay": 1,
                    "midweekMeetingTime": "19:30:00",
                    "weekendMeetingDay": 6,
                    "weekendMeetingTime": "09:00:00",
                },
                {
                    "id": "guid-central",
                    "name": "Central - Curitiba PR",
                    "midweekMeetingDay": 3,
                    "midweekMeetingTime": "19:45:00",
                    "weekendMeetingDay": 0,
                    "weekendMeetingTime": "10:00:00",
                },
            ]
        }
    ]
}


def _stub_get_json(monkeypatch, payloads: dict[str, object]) -> list[dict]:
    calls: list[dict] = []

    def get_json(url: str, *, params=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        return payloads[url]

    monkeypatch.setattr(congregation_lookup, "get_json", get_json)
    return calls


def test_search_congregations_keeps_only_usable_suggestions(monkeypatch):
    calls = _stub_get_json(
        monkeypatch,
        {congregation_lookup.CONGREGATION_SEARCH_URL: _SUGGESTIONS},
    )

    matches = search_congregations("  Curitiba  ")

    assert matches == [
        CongregationMatch(
            guid="guid-central",
            name="Central - Curitiba PR",
            formatted_name="Central - Curitiba PR (46789)",
        )
    ]
    assert calls[0]["params"] == {"congregationName": "Curitiba"}


def test_search_congregations_skips_the_request_when_the_query_is_blank(monkeypatch):
    calls = _stub_get_json(monkeypatch, {})

    assert search_congregations("   ") == []
    assert calls == []


def test_fetch_meeting_schedule_converts_the_api_weekdays_and_times(monkeypatch):
    _stub_get_json(monkeypatch, {congregation_lookup.MEETING_SEARCH_URL: _MEETING_SEARCH})

    schedule = fetch_meeting_schedule("guid-central")

    assert schedule is not None
    # API weekday 3 (Wednesday) maps to the Python weekday 2.
    assert schedule.midweek.weekday == 2
    assert schedule.midweek.time_text == "19:45"
    # API weekday 0 (Sunday) maps to the Python weekday 6.
    assert schedule.weekend.weekday == 6
    assert schedule.weekend.time_text == "10:00"


def test_fetch_meeting_schedule_returns_none_without_published_times(monkeypatch):
    _stub_get_json(
        monkeypatch,
        {
            congregation_lookup.MEETING_SEARCH_URL: {
                "items": [{"congregationMeetings": [{"id": "guid-central"}]}]
            }
        },
    )

    assert fetch_meeting_schedule("guid-central") is None


def test_fetch_meeting_schedule_skips_the_request_without_a_guid(monkeypatch):
    calls = _stub_get_json(monkeypatch, {})

    assert fetch_meeting_schedule("") is None
    assert calls == []
