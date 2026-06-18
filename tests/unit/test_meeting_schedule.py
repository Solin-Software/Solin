from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from solin.core.foundation.constants import QSETTINGS_PREFS_APP
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.foundation.settings_store import SettingsStore
from solin.core.meetings.schedule import (
    MIDWEEK,
    UNCONFIGURED_WEEKDAY,
    WEEKEND,
    MeetingSchedule,
    MeetingSlot,
    minutes_to_time_text,
    normalize_weekday,
    parse_time_text,
)
from solin.core.meetings.schedule_settings import MeetingScheduleSettingsStore
from solin.core.profiles.settings import ProfileSettings


def _store() -> tuple[MeetingScheduleSettingsStore, SettingsStore]:
    profile_settings = ProfileSettings.for_profile_id(
        f"meeting_schedule_{uuid.uuid4().hex}"
    )
    settings = SettingsStore.for_namespace(
        profile_settings.organization,
        QSETTINGS_PREFS_APP,
    )
    return MeetingScheduleSettingsStore(settings), settings


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
    store, settings = _store()
    settings.clear()
    try:
        settings.set_value(SettingsKey.MEETING_MIDWEEK_DAY, "7", sync=False)
        settings.set_value(SettingsKey.MEETING_MIDWEEK_TIME, "25:00", sync=False)
        settings.set_value(SettingsKey.MEETING_WEEKEND_DAY, "5", sync=False)
        settings.set_value(SettingsKey.MEETING_WEEKEND_TIME, "10:00")

        schedule = store.load()

        assert not schedule.midweek.is_configured
        assert schedule.weekend.is_configured
        assert schedule.weekend.weekday == 5
        assert schedule.weekend.time_text == "10:00"
    finally:
        settings.clear()


def test_schedule_settings_roundtrips_slots():
    store, settings = _store()
    settings.clear()
    try:
        store.set_slot(MIDWEEK, 2, "19:30")
        store.set_slot(WEEKEND, 5, "10:00")

        schedule = store.load()

        assert schedule.midweek.is_configured
        assert schedule.midweek.weekday == 2
        assert schedule.midweek.time_text == "19:30"
        assert schedule.weekend.is_configured
        assert schedule.weekend.weekday == 5
        assert schedule.weekend.time_text == "10:00"

        store.set_slot(MIDWEEK, UNCONFIGURED_WEEKDAY, "19:30")

        assert store.slot_values(MIDWEEK) == (UNCONFIGURED_WEEKDAY, "")
        assert not store.load().midweek.is_configured
    finally:
        settings.clear()
