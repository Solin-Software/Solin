"""Shared translations for meeting-schedule labels used across UI surfaces."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from solin.core.meetings.schedule import MIDWEEK, WEEKEND

_CONTEXT = "_MeetingSchedule"

_WEEKDAY_SOURCES = (
    QT_TRANSLATE_NOOP(_CONTEXT, "Monday"),
    QT_TRANSLATE_NOOP(_CONTEXT, "Tuesday"),
    QT_TRANSLATE_NOOP(_CONTEXT, "Wednesday"),
    QT_TRANSLATE_NOOP(_CONTEXT, "Thursday"),
    QT_TRANSLATE_NOOP(_CONTEXT, "Friday"),
    QT_TRANSLATE_NOOP(_CONTEXT, "Saturday"),
    QT_TRANSLATE_NOOP(_CONTEXT, "Sunday"),
)
_KIND_SOURCES = {
    MIDWEEK: QT_TRANSLATE_NOOP(_CONTEXT, "Midweek meeting"),
    WEEKEND: QT_TRANSLATE_NOOP(_CONTEXT, "Weekend meeting"),
}
_NOT_CONFIGURED_SOURCE = QT_TRANSLATE_NOOP(_CONTEXT, "Not configured")


def meeting_weekday_names() -> tuple[str, ...]:
    return tuple(
        QCoreApplication.translate(_CONTEXT, source)
        for source in _WEEKDAY_SOURCES
    )


def meeting_kind_label(kind: str) -> str:
    source = _KIND_SOURCES.get(kind)
    if source is None:
        return str(kind)
    return QCoreApplication.translate(_CONTEXT, source)


def meeting_not_configured_label() -> str:
    return QCoreApplication.translate(_CONTEXT, _NOT_CONFIGURED_SOURCE)


__all__ = [
    "meeting_kind_label",
    "meeting_not_configured_label",
    "meeting_weekday_names",
]
