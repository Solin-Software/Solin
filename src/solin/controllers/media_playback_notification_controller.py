"""User-facing notifications for foreground playback failures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QCoreApplication, QObject, Slot

from ..ui.notifications import NotificationCenter


class MediaPlaybackNotificationController(QObject):
    def __init__(
        self,
        notifications: NotificationCenter,
        media_controller,
        *,
        current_title: Callable[[], str],
        stop_projection: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._notifications = notifications
        self._media_controller = media_controller
        self._current_title = current_title
        self._stop_projection = stop_projection
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._media_controller.error_occurred.connect(self.on_playback_error)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._media_controller.error_occurred.disconnect(self.on_playback_error)
        except (RuntimeError, TypeError):
            pass
        self._started = False

    @Slot(str)
    def on_playback_error(self, message: str) -> None:
        media_name = self._media_name()
        error_detail = (message or "").strip() or self._tr("Unknown error")
        detail = self._tr("Could not play {name}.\n{error}")
        detail = detail.replace("{name}", media_name).replace("{error}", error_detail)
        self._notifications.error(
            detail,
            title=self._tr("Playback failed"),
            dedupe_key=f"media-playback:{self._media_controller.current_url}:{error_detail}",
        )
        self._stop_projection()

    def _media_name(self) -> str:
        title = (self._current_title() or "").strip()
        if title:
            return title
        return self._display_name(self._media_controller.current_url)

    @staticmethod
    def _display_name(url: str) -> str:
        parsed = urlparse(url)
        filename = unquote(PurePosixPath(parsed.path).name).strip()
        return filename or parsed.netloc or url or "media"

    @staticmethod
    def _tr(text: str) -> str:
        return QCoreApplication.translate("MediaPlaybackNotificationController", text)
