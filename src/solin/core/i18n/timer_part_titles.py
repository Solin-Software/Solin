"""Translation helpers for canonical timer part titles."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from ..timer.models import MeetingPart
from ..timer.part_titles import is_indexed_part_title_source, is_static_part_title_source

_TR_CTX = "_TimerPart"

# Literal source markers for lupdate. Keep this mirrored with
# app.core.timer.part_titles; tests guard that the canonical source set stays
# covered.
TIMER_PART_TITLE_SOURCES = (
    QT_TRANSLATE_NOOP("_TimerPart", "Treasures Talk"),
    QT_TRANSLATE_NOOP("_TimerPart", "Spiritual Gems"),
    QT_TRANSLATE_NOOP("_TimerPart", "Bible Reading"),
    QT_TRANSLATE_NOOP("_TimerPart", "Public Talk"),
    QT_TRANSLATE_NOOP("_TimerPart", "Watchtower Study"),
    QT_TRANSLATE_NOOP("_TimerPart", "Congregation Bible Study"),
    QT_TRANSLATE_NOOP("_TimerPart", "Opening Comments"),
    QT_TRANSLATE_NOOP("_TimerPart", "Concluding Comments"),
    QT_TRANSLATE_NOOP("_TimerPart", "Part {number}"),
    QT_TRANSLATE_NOOP("_TimerPart", "Treasures Part {number}"),
    QT_TRANSLATE_NOOP("_TimerPart", "Public Talk {number}"),
    QT_TRANSLATE_NOOP("_TimerPart", "Watchtower Study {number}"),
)


def _translated_title(source: str) -> str:
    return QCoreApplication.translate(_TR_CTX, source) or source


def format_indexed_part_title(source: str, number: int) -> str:
    template = _translated_title(source)
    if "{number}" not in template:
        template = source
    return template.replace("{number}", str(max(1, int(number))))


def display_part_title(part: MeetingPart, *, indexed_number: int | None = None) -> str:
    if is_indexed_part_title_source(part.title) and indexed_number is not None:
        return format_indexed_part_title(part.title, indexed_number)

    if is_static_part_title_source(part.title):
        return _translated_title(part.title)

    return part.title
