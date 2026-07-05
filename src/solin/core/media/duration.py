from __future__ import annotations


TICKS_PER_SECOND = 10_000_000


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
        base_duration_ticks
        - valid_trim(start_trim_ticks)
        - valid_trim(end_trim_ticks),
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
