"""Tests for the countdown time helper."""

from __future__ import annotations

from PySide6.QtCore import QDateTime

from app.core.foundation.time_utils import ceil_remaining_seconds


def _target_in_ms(ms: int) -> QDateTime:
    now = QDateTime.currentDateTime()
    return now.addMSecs(ms), now


def test_exact_minute_reads_full_value():
    target, now = _target_in_ms(900_000)
    assert ceil_remaining_seconds(target, now) == 900


def test_sub_second_remainder_rounds_up():
    # 899.5 s left must still read as 15:00, never 14:59.
    target, now = _target_in_ms(899_500)
    assert ceil_remaining_seconds(target, now) == 900


def test_one_millisecond_left_is_one_second():
    target, now = _target_in_ms(1)
    assert ceil_remaining_seconds(target, now) == 1


def test_exactly_at_target_is_zero():
    now = QDateTime.currentDateTime()
    assert ceil_remaining_seconds(now, now) == 0


def test_past_target_is_zero():
    now = QDateTime.currentDateTime()
    assert ceil_remaining_seconds(now.addSecs(-5), now) == 0


def test_counts_down_one_value_per_second_without_skips():
    # Simulate a 200 ms refresh against a fixed target and assert the sequence
    # of displayed seconds is monotonic and never repeats/skips across a second.
    now = QDateTime.currentDateTime()
    target = now.addMSecs(3_000)
    seen = []
    for ms in range(0, 3_001, 200):
        seen.append(ceil_remaining_seconds(target, now.addMSecs(ms)))
    # Starts at 3, ends at 0, strictly non-increasing, every step differs by <=1.
    assert seen[0] == 3
    assert seen[-1] == 0
    assert all(0 <= b <= a for a, b in zip(seen, seen[1:], strict=False))
    assert all(a - b <= 1 for a, b in zip(seen, seen[1:], strict=False))
    # Each whole second 3,2,1 is actually shown.
    assert {3, 2, 1, 0}.issubset(set(seen))
