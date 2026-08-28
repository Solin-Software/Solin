"""Consistent notifications for JW media picker insertion outcomes."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QObject, QT_TRANSLATE_NOOP


class MediaInsertionFeedback(QObject):
    """Qt translation context for media-insertion feedback."""

    @staticmethod
    def already_added_count(count: int) -> str:
        return MediaInsertionFeedback.tr(
            "%n media item(s) were already added",
            "",
            max(0, int(count)),
        )


_TR_CONTEXT = MediaInsertionFeedback.__name__
_THIS_MEDIA_SOURCE = QT_TRANSLATE_NOOP("MediaInsertionFeedback", "This media")
_MEDIA_SOURCE = QT_TRANSLATE_NOOP("MediaInsertionFeedback", "Media")
_DUPLICATE_SOURCE = QT_TRANSLATE_NOOP(
    "MediaInsertionFeedback",
    "“{title}” is already added.",
)
_ADDED_SOURCE = QT_TRANSLATE_NOOP("MediaInsertionFeedback", "{title} added")
_FAILED_SOURCE = QT_TRANSLATE_NOOP(
    "MediaInsertionFeedback",
    "Could not add “{title}”.",
)


def _tr(source: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, source)


def tr_media_already_added_count(count: int) -> str:
    return MediaInsertionFeedback.already_added_count(count)


def notify_media_duplicate(notifications, title: str, identity_token: str) -> None:
    display_title = title or _tr(_THIS_MEDIA_SOURCE)
    notifications.warning(
        _tr(_DUPLICATE_SOURCE).format(title=display_title),
        dedupe_key=f"media-duplicate:{identity_token}",
    )


def connect_media_picker_feedback(bridge, notifications) -> None:
    if notifications is None:
        return

    def added(title: str) -> None:
        display_title = title or _tr(_MEDIA_SOURCE)
        notifications.success(
            _tr(_ADDED_SOURCE).format(title=f"“{display_title}”")
        )

    def duplicate(title: str, identity_token: str) -> None:
        notify_media_duplicate(notifications, title, identity_token)

    def failed(title: str) -> None:
        display_title = title or _tr(_THIS_MEDIA_SOURCE)
        notifications.error(
            _tr(_FAILED_SOURCE).format(title=display_title)
        )

    bridge.mediaAdded.connect(added)
    bridge.mediaAlreadyAdded.connect(duplicate)
    bridge.mediaInsertionFailed.connect(failed)


__all__ = [
    "connect_media_picker_feedback",
    "notify_media_duplicate",
    "tr_media_already_added_count",
]
