"""User-facing notifications for foreground playback failures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QCoreApplication, QObject, QT_TRANSLATE_NOOP, Slot

from ..ui.notifications import NotificationCenter

_TR_CONTEXT = "MediaPlaybackNotifications"
_UNKNOWN_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "MediaPlaybackNotifications",
    "Unknown error",
)
_MEDIA_SOURCE = QT_TRANSLATE_NOOP("MediaPlaybackNotifications", "media")
_PLAYBACK_FAILED_SOURCE = QT_TRANSLATE_NOOP(
    "MediaPlaybackNotifications",
    "Playback failed",
)
_PLAYBACK_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "MediaPlaybackNotifications",
    "Could not play {name}.\n{error}",
)
_PLAYBACK_INTERRUPTED_SOURCE = QT_TRANSLATE_NOOP(
    "MediaPlaybackNotifications",
    "Playback interrupted",
)
_INTERRUPTED_DETAIL_SOURCE = QT_TRANSLATE_NOOP(
    "MediaPlaybackNotifications",
    "Playback was interrupted for {name}.\n"
    "Solin will keep trying to reconnect from the current position.\n"
    "{error}",
)


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
        self._media_controller.playback_interrupted.connect(
            self.on_playback_interrupted
        )
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._media_controller.error_occurred.disconnect(self.on_playback_error)
        except (RuntimeError, TypeError):
            pass
        try:
            self._media_controller.playback_interrupted.disconnect(
                self.on_playback_interrupted
            )
        except (RuntimeError, TypeError):
            pass
        self._started = False

    @Slot(str)
    def on_playback_error(self, message: str) -> None:
        media_name = self._media_name()
        error_detail = (message or "").strip() or self._tr(_UNKNOWN_ERROR_SOURCE)
        detail = self._tr(_PLAYBACK_ERROR_SOURCE).format(
            name=media_name,
            error=error_detail,
        )
        self._notifications.error(
            detail,
            title=self._tr(_PLAYBACK_FAILED_SOURCE),
            dedupe_key=f"media-playback:{self._media_controller.current_url}:{error_detail}",
        )
        self._stop_projection()

    @Slot(str, str)
    def on_playback_interrupted(self, url: str, message: str) -> None:
        media_name = self._media_name(url)
        error_detail = (message or "").strip() or self._tr(_UNKNOWN_ERROR_SOURCE)
        detail = self._tr(_INTERRUPTED_DETAIL_SOURCE).format(
            name=media_name,
            error=error_detail,
        )
        self._notifications.warning(
            detail,
            title=self._tr(_PLAYBACK_INTERRUPTED_SOURCE),
            dedupe_key=f"media-playback-interrupted:{url}",
        )

    def _media_name(self, url: str | None = None) -> str:
        title = (self._current_title() or "").strip()
        if title:
            return title
        return self._display_name(
            url or self._media_controller.current_url,
            self._tr(_MEDIA_SOURCE),
        )

    @staticmethod
    def _display_name(url: str, fallback: str) -> str:
        parsed = urlparse(url)
        filename = unquote(PurePosixPath(parsed.path).name).strip()
        return filename or parsed.netloc or url or fallback

    @staticmethod
    def _tr(text: str) -> str:
        return QCoreApplication.translate(_TR_CONTEXT, text)
