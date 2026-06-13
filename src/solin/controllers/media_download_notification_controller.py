"""User-facing notifications for background media cache failures."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QCoreApplication, QObject, Slot

from ..core.media.cache import MediaCacheManager
from ..core.ui.notifications import NotificationCenter


class MediaDownloadNotificationController(QObject):
    def __init__(
        self,
        notifications: NotificationCenter,
        parent: QObject | None = None,
        *,
        cache_manager: MediaCacheManager | None = None,
    ) -> None:
        super().__init__(parent)
        self._notifications = notifications
        self._cache_manager = cache_manager or MediaCacheManager.instance()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._cache_manager.prefetch_error.connect(self.on_prefetch_error)
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._cache_manager.prefetch_error.disconnect(self.on_prefetch_error)
        except (RuntimeError, TypeError):
            pass
        self._started = False

    @Slot(str, str)
    def on_prefetch_error(self, url: str, message: str) -> None:
        display_name = self._display_name(url)
        error_detail = (message or "").strip() or self._tr("Unknown error")
        detail = self._tr("Could not download {name}.\n{error}")
        detail = detail.replace("{name}", display_name).replace("{error}", error_detail)
        self._notifications.error(
            detail,
            title=self._tr("Download failed"),
            dedupe_key=f"media-prefetch:{url}",
        )

    @staticmethod
    def _display_name(url: str) -> str:
        parsed = urlparse(url)
        filename = unquote(PurePosixPath(parsed.path).name).strip()
        return filename or parsed.netloc or url or "media"

    @staticmethod
    def _tr(text: str) -> str:
        return QCoreApplication.translate("MediaDownloadNotificationController", text)
