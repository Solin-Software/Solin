from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.foundation.settings_keys import SettingsKey
from app.core.meetings.schedule import (
    MIDWEEK,
    UNCONFIGURED_WEEKDAY,
    WEEKEND,
    MeetingSchedule,
    MeetingSlot,
    load_meeting_schedule,
    minutes_to_time_text,
    normalize_weekday,
    parse_time_text,
)


class _Prefs:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def value(self, key: str, default=None, *_args):
        return self._values.get(key, default)


def _now(hour: int, minute: int = 0) -> datetime:
    tz = timezone(timedelta(hours=-3))
    return datetime(2026, 6, 8, hour, minute, tzinfo=tz)


def test_weekday_and_time_helpers_normalize_invalid_values():
    assert normalize_weekday("2") == 2
    assert normalize_weekday(7) == UNCONFIGURED_WEEKDAY
    assert normalize_weekday("bad") == UNCONFIGURED_WEEKDAY

    assert parse_time_text("19:30") == 19 * 60 + 30
    assert parse_time_text("24:00") is None
    assert parse_time_text("") is None
    assert minutes_to_time_text(19 * 60 + 5) == "19:05"


def test_schedule_returns_today_occurrence_before_start():
    now = _now(19, 0)
    schedule = MeetingSchedule(
        midweek=MeetingSlot(MIDWEEK, now.weekday(), 19 * 60 + 30),
        weekend=MeetingSlot(WEEKEND),
    )

    occurrence = schedule.pre_meeting_occurrence(now)

    assert occurrence is not None
    assert occurrence.starts_at == _now(19, 30)
    assert occurrence.milliseconds_until_start(now) == 30 * 60 * 1000


def test_schedule_does_not_return_occurrence_at_or_after_start():
    start = _now(19, 30)
    schedule = MeetingSchedule(
        midweek=MeetingSlot(MIDWEEK, start.weekday(), 19 * 60 + 30),
        weekend=MeetingSlot(WEEKEND),
    )

    assert schedule.pre_meeting_occurrence(start) is None
    assert schedule.pre_meeting_occurrence(_now(20, 0)) is None


def test_schedule_uses_earliest_configured_meeting_today():
    now = _now(18, 0)
    schedule = MeetingSchedule(
        midweek=MeetingSlot(MIDWEEK, now.weekday(), 20 * 60),
        weekend=MeetingSlot(WEEKEND, now.weekday(), 19 * 60),
    )

    occurrence = schedule.pre_meeting_occurrence(now)

    assert occurrence is not None
    assert occurrence.slot.kind == WEEKEND
    assert occurrence.starts_at == _now(19, 0)


def test_load_schedule_keeps_invalid_values_unconfigured():
    prefs = _Prefs({
        SettingsKey.MEETING_MIDWEEK_DAY: "7",
        SettingsKey.MEETING_MIDWEEK_TIME: "25:00",
        SettingsKey.MEETING_WEEKEND_DAY: "5",
        SettingsKey.MEETING_WEEKEND_TIME: "10:00",
    })

    schedule = load_meeting_schedule(prefs)

    assert not schedule.midweek.is_configured
    assert schedule.weekend.is_configured
    assert schedule.weekend.weekday == 5
    assert schedule.weekend.time_text == "10:00"
