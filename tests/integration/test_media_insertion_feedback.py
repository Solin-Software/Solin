from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from solin.ui.media_insertion_feedback import (
    connect_media_duplicate_feedback,
    connect_media_picker_feedback,
)


class _Bridge(QObject):
    mediaAdded = Signal(str)
    mediaAlreadyAdded = Signal(str, str)
    mediaInsertionFailed = Signal(str)


class _Notifications:
    def __init__(self) -> None:
        self.events = []

    def success(self, message, **kwargs):
        self.events.append(("success", message, kwargs))

    def warning(self, message, **kwargs):
        self.events.append(("warning", message, kwargs))

    def error(self, message, **kwargs):
        self.events.append(("error", message, kwargs))


def test_media_picker_feedback_reports_real_outcomes_and_deduplicates_warning():
    bridge = _Bridge()
    notifications = _Notifications()
    connect_media_picker_feedback(bridge, notifications)

    bridge.mediaAdded.emit("Song 2")
    bridge.mediaAlreadyAdded.emit("Song 2", "identity")
    bridge.mediaInsertionFailed.emit("Song 3")

    assert notifications.events == [
        ("success", "“Song 2” added", {}),
        (
            "warning",
            "“Song 2” is already added.",
            {"dedupe_key": "media-duplicate:identity"},
        ),
        ("error", "Could not add “Song 3”.", {}),
    ]


def test_duplicate_only_feedback_uses_the_shared_warning_contract():
    source = _Bridge()
    notifications = _Notifications()
    connect_media_duplicate_feedback(source, notifications)

    source.mediaAlreadyAdded.emit("Local clip", "local-identity")

    assert notifications.events == [
        (
            "warning",
            "“Local clip” is already added.",
            {"dedupe_key": "media-duplicate:local-identity"},
        )
    ]
