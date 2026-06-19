"""User-facing notifications for background media cache failures."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from PySide6.QtCore import QCoreApplication, QObject, Slot

from ..core.media.cache import MediaCacheManager
from ..ui.notifications import NotificationCenter


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
        display_name = self._display_name(url)
        error_detail = (message or "").strip() or self._tr("Unknown error")
        detail = self._tr("Could not download {name}.\n{error}")
        detail = detail.replace("{name}", display_name).replace("{error}", error_detail)
        self._notifications.error(
            detail,
            title=self._tr("Download failed"),
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
        display_name = self._display_name(url)
        error_detail = (message or "").strip() or self._tr("Unknown error")
        if self._is_disk_full_error(error_detail):
            template = self._tr(
                "Could not save {name} locally because there is not enough disk space.\n"
                "Playback will continue by streaming.\n"
                "{error}"
            )
        else:
            template = self._tr(
                "Could not save {name} locally.\n"
                "Playback will continue by streaming.\n"
                "{error}"
            )
        detail = template.replace("{name}", display_name).replace(
            "{error}",
            error_detail,
        )
        self._notifications.warning(
            detail,
            title=self._tr("Local copy failed"),
            dedupe_key=f"media-playback-download:{url}:{error_detail}",
        )

    @staticmethod
    def _display_name(url: str) -> str:
        parsed = urlparse(url)
        filename = unquote(PurePosixPath(parsed.path).name).strip()
        return filename or parsed.netloc or url or "media"

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
        return QCoreApplication.translate("MediaDownloadNotificationController", text)
