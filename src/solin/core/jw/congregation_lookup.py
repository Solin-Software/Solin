"""Congregation meeting-time lookup backed by the public jw.org meetings API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from solin.core.meetings.schedule import (
    MIDWEEK,
    UNCONFIGURED_WEEKDAY,
    WEEKEND,
    MeetingSchedule,
    MeetingSlot,
)
from solin.core.network.http import HttpStatusError, get_json

CONGREGATION_SEARCH_URL = "https://hub.jw.org/meetings/api/congregations"
MEETING_SEARCH_URL = "https://hub.jw.org/meetings/api/meeting-search"

_TIMEOUT = 15.0
_MAX_RESULTS = 20

RATE_LIMITED = "rate_limited"
UNAVAILABLE = "unavailable"
_TOO_MANY_REQUESTS = 429
# Shortest query worth a request; below it every congregation matches.
MINIMUM_QUERY_LENGTH = 4
# jw.org allows about ten searches per half minute, so a request only goes
# out once typing actually stops.
SEARCH_DEBOUNCE_MS = 900


def classify_failure(error: Exception) -> str:
    """Separate a spent request budget from an unreachable endpoint."""

    if isinstance(error, HttpStatusError) and error.status_code == _TOO_MANY_REQUESTS:
        return RATE_LIMITED
    return UNAVAILABLE


@dataclass(frozen=True, slots=True)
class CongregationMatch:
    """One congregation suggestion returned by the meetings API."""

    guid: str
    name: str
    formatted_name: str


def search_congregations(name: str) -> list[CongregationMatch]:
    """Return congregation suggestions for a partial congregation name."""

    query = name.strip()
    if not query:
        return []
    data = get_json(
        CONGREGATION_SEARCH_URL,
        params={"congregationName": query},
        timeout=_TIMEOUT,
    )
    if not isinstance(data, list):
        return []
    matches = [_match_from_item(item) for item in data]
    return [match for match in matches if match is not None][:_MAX_RESULTS]


def fetch_meeting_schedule(guid: str) -> MeetingSchedule | None:
    """Return the published meeting schedule for a congregation, if any."""

    if not guid:
        return None
    data = get_json(
        MEETING_SEARCH_URL,
        params={"first": _MAX_RESULTS, "meetingLocationEventGuid": guid},
        timeout=_TIMEOUT,
    )
    meeting = _congregation_meeting(data, guid)
    if meeting is None:
        return None
    schedule = MeetingSchedule(
        midweek=MeetingSlot.from_values(
            MIDWEEK,
            _weekday_from_api(meeting.get("midweekMeetingDay")),
            _time_from_api(meeting.get("midweekMeetingTime")),
        ),
        weekend=MeetingSlot.from_values(
            WEEKEND,
            _weekday_from_api(meeting.get("weekendMeetingDay")),
            _time_from_api(meeting.get("weekendMeetingTime")),
        ),
    )
    return schedule if schedule.has_configured_slot else None


def _match_from_item(item: Any) -> CongregationMatch | None:
    if not isinstance(item, dict):
        return None
    guid = str(item.get("congregationGuid") or "")
    name = str(item.get("name") or "")
    if not guid or not name:
        return None
    return CongregationMatch(
        guid=guid,
        name=name,
        formatted_name=str(item.get("formattedName") or name),
    )


def _congregation_meeting(data: Any, guid: str) -> dict[str, Any] | None:
    """Prefer the meeting whose id matches the searched congregation."""

    if not isinstance(data, dict):
        return None
    fallback: dict[str, Any] | None = None
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        for meeting in item.get("congregationMeetings") or []:
            if not isinstance(meeting, dict):
                continue
            if meeting.get("id") == guid:
                return meeting
            if fallback is None:
                fallback = meeting
    return fallback


def _weekday_from_api(value: Any) -> int:
    """Convert the API weekday (0 = Sunday) into a Python weekday (0 = Monday)."""

    try:
        weekday = int(value)
    except (TypeError, ValueError):
        return UNCONFIGURED_WEEKDAY
    if not 0 <= weekday <= 6:
        return UNCONFIGURED_WEEKDAY
    return (weekday - 1) % 7


def _time_from_api(value: Any) -> str:
    """Reduce an ``HH:mm:ss`` API time to the ``HH:mm`` form Solin persists."""

    if not isinstance(value, str):
        return ""
    return value.strip()[:5]


__all__ = [
    "CONGREGATION_SEARCH_URL",
    "MEETING_SEARCH_URL",
    "MINIMUM_QUERY_LENGTH",
    "RATE_LIMITED",
    "SEARCH_DEBOUNCE_MS",
    "UNAVAILABLE",
    "CongregationMatch",
    "classify_failure",
    "fetch_meeting_schedule",
    "search_congregations",
]
