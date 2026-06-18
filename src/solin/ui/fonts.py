"""Shared presentation font selection helpers."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF


_TIMER_DIGIT_FONT_CANDIDATES: tuple[str, ...] = (
    "Roboto",
    "Calibri",
    "SF Pro Display",
    "SF Pro Text",
    "Helvetica Neue",
    "Tahoma",
    "Trebuchet MS",
    "Verdana",
    "Microsoft Sans Serif",
    "Arial",
    "Consolas",
)


@lru_cache(maxsize=1)
def timer_digit_font_family() -> str:
    """Return a clean sans font family with stable timer digit widths."""
    available = set(QFontDatabase.families())
    for family in _TIMER_DIGIT_FONT_CANDIDATES:
        if family in available and _has_stable_timer_widths(family):
            return family

    fixed_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    fallback = fixed_font.family().strip()
    return fallback or "monospace"


def _has_stable_timer_widths(family: str) -> bool:
    """True when same-length timer values have identical measured widths."""
    font = QFont(family)
    font.setPixelSize(100)
    font.setWeight(QFont.Weight.DemiBold)
    metrics = QFontMetricsF(font)
    expected_by_format: dict[str, float] = {}

    for format_key, text in _timer_width_samples():
        width = metrics.horizontalAdvance(text)
        expected = expected_by_format.setdefault(format_key, width)
        if abs(width - expected) > 0.5:
            return False
    return True


def _timer_width_samples():
    for minutes in range(100):
        for seconds in range(60):
            value = f"{minutes:02d}:{seconds:02d}"
            yield "mm:ss", value
            yield "-mm:ss", f"-{value}"

    for hours in range(1, 13):
        hour_digits = len(str(hours))
        for minutes in (0, 11, 59):
            for seconds in (0, 11, 59):
                value = f"{hours}:{minutes:02d}:{seconds:02d}"
                yield f"h{hour_digits}:mm:ss", value
                yield f"-h{hour_digits}:mm:ss", f"-{value}"
