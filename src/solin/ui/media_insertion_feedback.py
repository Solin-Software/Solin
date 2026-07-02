"""Consistent notifications for JW media picker insertion outcomes."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication


def _tr(source: str) -> str:
    return QCoreApplication.translate("MediaInsertionFeedback", source)


def notify_media_duplicate(notifications, title: str, identity_token: str) -> None:
    display_title = title or _tr("This media")
    notifications.warning(
        _tr("“{title}” is already added.").replace("{title}", display_title),
        dedupe_key=f"media-duplicate:{identity_token}",
    )


def connect_media_picker_feedback(bridge, notifications) -> None:
    if notifications is None:
        return

    def added(title: str) -> None:
        display_title = title or _tr("Media")
        notifications.success(
            _tr("{title} added").replace("{title}", f"“{display_title}”")
        )

    def duplicate(title: str, identity_token: str) -> None:
        notify_media_duplicate(notifications, title, identity_token)

    def failed(title: str) -> None:
        display_title = title or _tr("This media")
        notifications.error(
            _tr("Could not add “{title}”.").replace("{title}", display_title)
        )

    bridge.mediaAdded.connect(added)
    bridge.mediaAlreadyAdded.connect(duplicate)
    bridge.mediaInsertionFailed.connect(failed)


__all__ = ["connect_media_picker_feedback", "notify_media_duplicate"]
