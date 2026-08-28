"""User-facing notifications for background media cache failures."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QCoreApplication, QObject, QT_TRANSLATE_NOOP, Slot

from ..core.media.cache import MediaCacheManager
from ..ui.notifications import NotificationCenter

_TR_CONTEXT = "MediaDownloadNotifications"
_UNKNOWN_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDownloadNotifications",
    "Unknown error",
)
_MEDIA_SOURCE = QT_TRANSLATE_NOOP("MediaDownloadNotifications", "media")
_DOWNLOAD_FAILED_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDownloadNotifications",
    "Download failed",
)
_DOWNLOAD_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDownloadNotifications",
    "Could not download {name}.\n{error}",
)
_LOCAL_COPY_FAILED_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDownloadNotifications",
    "Local copy failed",
)
_DISK_FULL_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDownloadNotifications",
    "Could not save {name} locally because there is not enough disk space.\n"
    "Playback will continue by streaming.\n"
    "{error}",
)
_LOCAL_COPY_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "MediaDownloadNotifications",
    "Could not save {name} locally.\n"
    "Playback will continue by streaming.\n"
    "{error}",
)


class MediaDownloadNotificationController(QObject):
    def __init__(
        self,
        notifications: NotificationCenter,
        cache_manager: MediaCacheManager,
        media_controller=None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._notifications = notifications
        self._cache_manager = cache_manager
        self._media_controller = media_controller
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._cache_manager.prefetch_error.connect(self.on_prefetch_error)
        if self._media_controller is not None:
            self._media_controller.playback_download_failed.connect(
                self.on_playback_download_error
            )
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._cache_manager.prefetch_error.disconnect(self.on_prefetch_error)
        except (RuntimeError, TypeError):
            pass
        if self._media_controller is not None:
            try:
                self._media_controller.playback_download_failed.disconnect(
                    self.on_playback_download_error
                )
            except (RuntimeError, TypeError):
                pass
        self._started = False

    @Slot(str, str)
    def on_prefetch_error(self, url: str, message: str) -> None:
        display_name = self._display_name(url, self._tr(_MEDIA_SOURCE))
        error_detail = (message or "").strip() or self._tr(_UNKNOWN_ERROR_SOURCE)
        detail = self._tr(_DOWNLOAD_ERROR_SOURCE).format(
            name=display_name,
            error=error_detail,
        )
        self._notifications.error(
            detail,
            title=self._tr(_DOWNLOAD_FAILED_SOURCE),
            dedupe_key=f"media-prefetch:{url}",
        )

    @Slot(str, str, bool)
    def on_playback_download_error(
        self,
        url: str,
        message: str,
        persist: bool,
    ) -> None:
        del persist
        display_name = self._display_name(url, self._tr(_MEDIA_SOURCE))
        error_detail = (message or "").strip() or self._tr(_UNKNOWN_ERROR_SOURCE)
        if self._is_disk_full_error(error_detail):
            template = self._tr(_DISK_FULL_SOURCE)
        else:
            template = self._tr(_LOCAL_COPY_ERROR_SOURCE)
        detail = template.format(
            name=display_name,
            error=error_detail,
        )
        self._notifications.warning(
            detail,
            title=self._tr(_LOCAL_COPY_FAILED_SOURCE),
            dedupe_key=f"media-playback-download:{url}:{error_detail}",
        )

    @staticmethod
    def _display_name(url: str, fallback: str) -> str:
        parsed = urlparse(url)
        filename = unquote(PurePosixPath(parsed.path).name).strip()
        return filename or parsed.netloc or url or fallback

    @staticmethod
    def _is_disk_full_error(message: str) -> bool:
        lowered = message.lower()
        return (
            "errno 28" in lowered
            or "winerror 112" in lowered
            or "no space left" in lowered
            or "espaço insuficiente" in lowered
            or "espaco insuficiente" in lowered
        )

    @staticmethod
    def _tr(text: str) -> str:
        return QCoreApplication.translate(_TR_CONTEXT, text)
