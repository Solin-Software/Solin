from solin.core.media.duration import (
    TICKS_PER_SECOND,
    effective_duration_ticks,
    format_duration_ticks,
    format_effective_duration_ticks,
)


def test_format_duration_ticks_returns_empty_text_for_missing_duration():
    assert format_duration_ticks(0) == ""
    assert format_duration_ticks(-1) == ""


def test_format_duration_ticks_formats_minutes_and_seconds():
    assert format_duration_ticks(65 * TICKS_PER_SECOND) == "1:05"


def test_format_duration_ticks_formats_hours_minutes_and_seconds():
    assert format_duration_ticks(3661 * TICKS_PER_SECOND) == "1:01:01"


def test_effective_duration_subtracts_start_and_end_offsets():
    assert effective_duration_ticks(
        120 * TICKS_PER_SECOND,
        10 * TICKS_PER_SECOND,
        20 * TICKS_PER_SECOND,
    ) == 90 * TICKS_PER_SECOND
    assert format_effective_duration_ticks(
        120 * TICKS_PER_SECOND,
        10 * TICKS_PER_SECOND,
        20 * TICKS_PER_SECOND,
    ) == "1:30"


def test_effective_duration_is_safe_for_invalid_persisted_values():
    assert effective_duration_ticks(10 * TICKS_PER_SECOND, -1, 0) == (
        10 * TICKS_PER_SECOND
    )
    assert effective_duration_ticks(10, 20, 30) == 0
    assert effective_duration_ticks("invalid") == 0  # type: ignore[arg-type]
