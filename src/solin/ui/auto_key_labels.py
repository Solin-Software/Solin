"""Presentation labels for automatic shortcut events."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from solin.core.integrations.automation.shortcuts import (
    EVENT_MEDIA_ENDED,
    EVENT_MEDIA_PAUSED,
    EVENT_MEDIA_RESUMED,
    EVENT_MEDIA_STARTED,
)

_TR_CONTEXT = "AutoKeyEvents"


def auto_key_event_label(event: str) -> str:
    if event == EVENT_MEDIA_STARTED:
        return QCoreApplication.translate(_TR_CONTEXT, "Media starts")
    if event == EVENT_MEDIA_ENDED:
        return QCoreApplication.translate(_TR_CONTEXT, "Media ends")
    if event == EVENT_MEDIA_PAUSED:
        return QCoreApplication.translate(_TR_CONTEXT, "Video pauses")
    if event == EVENT_MEDIA_RESUMED:
        return QCoreApplication.translate(_TR_CONTEXT, "Video resumes")
    return event

