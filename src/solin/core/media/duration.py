from __future__ import annotations


TICKS_PER_SECOND = 10_000_000


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
