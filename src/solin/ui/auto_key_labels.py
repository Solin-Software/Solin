"""Presentation labels for automatic shortcut events."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from solin.core.integrations.automation.auto_key_actions import (
    AUTO_KEY_EVENTS,
    EVENT_MEDIA_ENDED,
    EVENT_MEDIA_PAUSED,
    EVENT_MEDIA_RESUMED,
    EVENT_MEDIA_STARTED,
)

_TR_CONTEXT = "AutoKeyEvents"
AUTO_KEY_EVENT_LABEL_SOURCES = {
    EVENT_MEDIA_STARTED: QT_TRANSLATE_NOOP("AutoKeyEvents", "Media starts"),
    EVENT_MEDIA_ENDED: QT_TRANSLATE_NOOP("AutoKeyEvents", "Media ends"),
    EVENT_MEDIA_PAUSED: QT_TRANSLATE_NOOP("AutoKeyEvents", "Video pauses"),
    EVENT_MEDIA_RESUMED: QT_TRANSLATE_NOOP("AutoKeyEvents", "Video resumes"),
}


def auto_key_event_label(event: str) -> str:
    source = AUTO_KEY_EVENT_LABEL_SOURCES.get(event)
    return QCoreApplication.translate(_TR_CONTEXT, source) if source else event


assert set(AUTO_KEY_EVENT_LABEL_SOURCES) == set(AUTO_KEY_EVENTS)
