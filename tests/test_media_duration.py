from solin.core.media.duration import TICKS_PER_SECOND, format_duration_ticks


def test_format_duration_ticks_returns_empty_text_for_missing_duration():
    assert format_duration_ticks(0) == ""
    assert format_duration_ticks(-1) == ""


def test_format_duration_ticks_formats_minutes_and_seconds():
    assert format_duration_ticks(65 * TICKS_PER_SECOND) == "1:05"


def test_format_duration_ticks_formats_hours_minutes_and_seconds():
    assert format_duration_ticks(3661 * TICKS_PER_SECOND) == "1:01:01"
