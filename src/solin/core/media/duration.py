from __future__ import annotations

import math
from typing import Any


TICKS_PER_SECOND = 10_000_000


def normalize_duration_ticks(
    *,
    ticks: Any = None,
    seconds: Any = None,
) -> int:
    """Return one validated duration in 100 ns ticks.

    Persisted media uses ticks as its single duration unit. Seconds are accepted
    only at external API boundaries and are converted here exactly once.
    """

    if ticks is not None and not isinstance(ticks, bool):
        if type(ticks) is int:
            normalized_ticks = ticks
        else:
            try:
                normalized_ticks = int(ticks)
            except (TypeError, ValueError, OverflowError):
                normalized_ticks = 0
        if normalized_ticks > 0:
            return normalized_ticks

    if seconds is None or isinstance(seconds, bool):
        return 0
    if type(seconds) in (int, float):
        normalized_seconds = seconds
    else:
        try:
            normalized_seconds = float(seconds)
        except (TypeError, ValueError, OverflowError):
            return 0
    if not math.isfinite(normalized_seconds) or normalized_seconds <= 0:
        return 0
    return round(normalized_seconds * TICKS_PER_SECOND)


def effective_duration_ticks(
    base_duration_ticks: int,
    start_trim_ticks: int = 0,
    end_trim_ticks: int = 0,
) -> int:
    """Return the playable duration without changing the source duration."""
    if (
        isinstance(base_duration_ticks, bool)
        or not isinstance(base_duration_ticks, int)
        or base_duration_ticks <= 0
    ):
        return 0

    def valid_trim(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    return max(
        0,
        base_duration_ticks - valid_trim(start_trim_ticks) - valid_trim(end_trim_ticks),
    )


def format_duration_ticks(ticks: int) -> str:
    if not ticks or ticks <= 0:
        return ""
    total_seconds = ticks // TICKS_PER_SECOND
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_effective_duration_ticks(
    base_duration_ticks: int,
    start_trim_ticks: int = 0,
    end_trim_ticks: int = 0,
) -> str:
    return format_duration_ticks(
        effective_duration_ticks(
            base_duration_ticks,
            start_trim_ticks,
            end_trim_ticks,
        )
    )
