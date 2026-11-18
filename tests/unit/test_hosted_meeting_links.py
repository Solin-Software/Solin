"""Short codes, passwords and the four weeks of prepared meetings."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from solin.core.hosted_meeting.devices import APPROVAL_LIFETIME_S, ApprovedDevice, DeviceRegistry, KeyKind, \
    StaffRole, network_of
from solin.core.hosted_meeting.links import (
    CODE_ALPHABET,
    LinksState,
    clean_password,
    new_code,
    new_password,
    normalize_code,
    weekly_occurrences,
)
from solin.core.meetings.schedule import MIDWEEK, WEEKEND, MeetingSchedule, MeetingSlot

TZ = timezone(timedelta(hours=-3))
# Monday 2026-11-16, 12:00.
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)
SCHEDULE = MeetingSchedule(midweek=MeetingSlot(MIDWEEK, 2, 19 * 60 + 30),   # Wednesday 19:30
                           weekend=MeetingSlot(WEEKEND, 6, 9 * 60))         # Sunday 09:00


def test_codes_avoid_confusable_letters_and_vowels():
    assert not set("0o1liaeu") & set(CODE_ALPHABET)
    codes = {new_code() for _ in range(500)}
    assert all(len(c) == 6 and set(c) <= set(CODE_ALPHABET) for c in codes)
    assert new_code(taken=codes) not in codes


@pytest.mark.parametrize("typed, expected", [
    ("k7m4p2", "k7m4p2"), ("K7M-4P2", "k7m4p2"), (" k7m 4p2 ", "k7m4p2"), ("k7m.4p2", "k7m4p2"),
    ("k7m4p", None), ("k7m4p22", None), ("k7m4a2", None), ("k0m4p2", None), ("", None),
])
def test_normalize_code(typed, expected):
    assert normalize_code(typed) == expected


def test_passwords():
    assert all(len(p) == 6 and p.isdigit() for p in (new_password() for _ in range(50)))
    assert clean_password(" 1234 ") == "1234"
    assert clean_password("abc") is None and clean_password("a b c d") is None and clean_password("x" * 33) is None


def test_weekly_occurrences_cover_four_weeks_and_keep_todays_open_window():
    meetings = weekly_occurrences(SCHEDULE, NOW, NOW + timedelta(weeks=4))
    assert [m.starts_at.isoformat(timespec="minutes") for m in meetings[:3]] == [
        "2026-11-18T19:30-03:00", "2026-11-22T09:00-03:00", "2026-11-25T19:30-03:00"]
    assert len(meetings) == 8
    # A meeting that started an hour ago is still in its window: it stays.
    during = datetime(2026, 11, 18, 20, 30, tzinfo=TZ)
    assert weekly_occurrences(SCHEDULE, during, during + timedelta(days=1))[0].id == "midweek:2026-11-18"


def test_window_and_count_time():
    meeting = weekly_occurrences(SCHEDULE, NOW, NOW + timedelta(days=3))[0]
    assert meeting.opens_at.strftime("%H:%M") == "19:00"
    assert meeting.closes_at.strftime("%H:%M") == "22:00"
    assert meeting.count_at.strftime("%H:%M") == "21:00"   # 0.75 of the two-hour worst case
    assert not meeting.in_window(meeting.opens_at - timedelta(minutes=1))
    assert meeting.in_window(meeting.opens_at) and not meeting.in_window(meeting.closes_at)


def test_per_meeting_links_are_unique_and_survive_a_top_up():
    state = LinksState().top_up(SCHEDULE, NOW, with_links=True)
    codes = [m.code for m in state.meetings]
    assert len(set(codes)) == len(codes) == 8 and all(m.password for m in state.meetings)
    again = state.top_up(SCHEDULE, NOW + timedelta(hours=1), with_links=True)
    assert again == state
    # A week later the first meetings are gone (their codes retired) and new ones prepared.
    later = state.top_up(SCHEDULE, NOW + timedelta(weeks=1), with_links=True)
    assert later.meetings[0].starts_at > NOW + timedelta(weeks=1) - timedelta(hours=3)
    assert set(codes[:2]) <= set(later.retired)
    assert not set(later.retired) & {m.code for m in later.meetings}


def test_same_link_mode_keeps_times_without_codes():
    state = LinksState().top_up(SCHEDULE, NOW, with_links=False).with_permanent()
    assert all(m.code == "" for m in state.meetings)
    assert state.permanent is not None and state.with_permanent() == state


def test_renew_cancel_move_and_one_offs():
    state = LinksState().top_up(SCHEDULE, NOW, with_links=True)
    first, second = state.meetings[0], state.meetings[1]
    renewed = state.renew(first.id, NOW)
    assert renewed.find(first.id).code != first.code and first.code in renewed.retired
    everything = state.renew_upcoming(NOW, skip=first.id)
    assert everything.find(first.id) == first and everything.find(second.id).code != second.code

    moved = state.move(first.id, first.starts_at + timedelta(days=1))
    assert moved.find(first.id).moved
    assert moved.top_up(SCHEDULE, NOW, with_links=True).find(first.id).starts_at == first.starts_at + timedelta(days=1)
    cancelled = state.set_cancelled(second.id, True)
    assert cancelled.top_up(SCHEDULE, NOW, with_links=True).find(second.id).cancelled  # not recreated

    added, meeting_id = state.add_meeting(NOW + timedelta(days=2), with_link=True)
    assert added.find(meeting_id).code and added.find(meeting_id).kind == "extra"
    removed = added.remove_meeting(meeting_id, NOW)
    assert removed.find(meeting_id) is None and added.find(meeting_id).code in removed.retired


def test_in_window_picks_the_open_meeting_only():
    state = LinksState().top_up(SCHEDULE, NOW, with_links=True)
    wednesday = datetime(2026, 11, 18, 19, 10, tzinfo=TZ)
    assert state.in_window(wednesday).id == "midweek:2026-11-18"
    assert state.in_window(NOW) is None
    assert state.set_cancelled("midweek:2026-11-18", True).in_window(wednesday) is None


def test_low_on_codes():
    state = LinksState().top_up(SCHEDULE, NOW, with_links=True)
    assert not state.low_on_codes(SCHEDULE, NOW)
    assert state.low_on_codes(SCHEDULE, NOW + timedelta(weeks=3))
    empty = MeetingSchedule(midweek=MeetingSlot(MIDWEEK), weekend=MeetingSlot(WEEKEND))
    assert not LinksState().low_on_codes(empty, NOW)


def test_state_round_trips_and_rejects_damaged_data():
    state = LinksState().top_up(SCHEDULE, NOW, with_links=True).with_permanent().renew("midweek:2026-11-18", NOW)
    assert LinksState.from_json(state.to_json()) == state
    for damaged in (None, [], {"version": 1, "slug": "x"}, {"version": 2, "permanent": {"code": "bad", "password": "1"}}):
        assert LinksState.from_json(damaged).permanent is None


def test_approved_devices_expire_and_networks_are_compared_by_prefix():
    device = ApprovedDevice("m1", StaffRole.MODERATOR, "Lima", KeyKind.SECRET, "h", approved_at=0.0)
    registry = DeviceRegistry([device])
    assert registry.get("m1", APPROVAL_LIFETIME_S - 1) is not None
    assert registry.get("m1", APPROVAL_LIFETIME_S + 1) is None
    assert registry.prune(APPROVAL_LIFETIME_S + 1) and registry.to_json() == []
    assert network_of("198.51.100.23") == network_of("198.51.100.200") == "198.51.100.0/24"
    assert network_of("2804:8cb0:0:1:be24:11ff:feb2:5e57") == "2804:8cb0:0:1::/64"
    assert network_of("::ffff:198.51.100.7") == "198.51.100.0/24"
    assert network_of("garbage") == ""
