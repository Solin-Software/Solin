from __future__ import annotations

from datetime import datetime, timedelta, timezone

from solin.core.jw.background_song_service import (
    DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS,
    _scheduled_stop_delays_ms,
)
from solin.core.meetings.schedule import MIDWEEK, MeetingOccurrence, MeetingSlot


def _occurrence(seconds_until_start: int) -> tuple[MeetingOccurrence, datetime]:
    now = datetime(2026, 6, 8, 19, 0, tzinfo=timezone(timedelta(hours=-3)))
    slot = MeetingSlot(MIDWEEK, now.weekday(), 19 * 60)
    return MeetingOccurrence(slot=slot, starts_at=now + timedelta(seconds=seconds_until_start)), now


def test_scheduled_stop_includes_lead_time_before_meeting():
    occurrence, now = _occurrence(30)

    fade_delay_ms, stop_delay_ms = _scheduled_stop_delays_ms(
        occurrence,
        now,
        fade_seconds=5,
        stop_before_seconds=10,
    )

    assert fade_delay_ms == 15_000
    assert stop_delay_ms == 20_000


def test_scheduled_stop_shortens_fade_when_already_inside_fade_window():
    occurrence, now = _occurrence(12)

    fade_delay_ms, stop_delay_ms = _scheduled_stop_delays_ms(
        occurrence,
        now,
        fade_seconds=5,
        stop_before_seconds=10,
    )

    assert fade_delay_ms == 0
    assert stop_delay_ms == 2_000


def test_scheduled_stop_is_immediate_after_configured_cutoff():
    occurrence, now = _occurrence(8)

    fade_delay_ms, stop_delay_ms = _scheduled_stop_delays_ms(
        occurrence,
        now,
        fade_seconds=5,
        stop_before_seconds=DEFAULT_BACKGROUND_SONG_STOP_BEFORE_SECONDS,
    )

    assert fade_delay_ms == 0
    assert stop_delay_ms == 0
