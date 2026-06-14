"""Concrete media-service composition for the application process."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

from solin.core.media.browser_downloads import BrowserDownloadService
from solin.core.media.cache import MediaCacheManager
from solin.core.media.downloader import SongDownloader
from solin.core.media.playback import MediaController
from solin.core.media.qt_contracts import PlaybackDownloader
from solin.core.media.settings import MediaPlaybackSettings

if TYPE_CHECKING:
    from solin.widgets.media_info_extractor import MediaInfoQueue, MediaInfoService


class MediaComposition:
    """Builds media adapters while keeping concrete wiring in bootstrap."""

    def __init__(
        self,
        app: QObject,
        media_cache_dir: Path,
        thumb_cache_dir: Path,
    ) -> None:
        self._media_cache_dir = media_cache_dir
        self._thumb_cache_dir = thumb_cache_dir
        self.cache_manager = MediaCacheManager(
            media_cache_dir,
            downloader_factory=self.create_downloader,
            parent=app,
        )

    def create_downloader(self, parent: QObject) -> PlaybackDownloader:
        return SongDownloader(self._media_cache_dir, parent)

    def create_playback(
        self,
        settings: MediaPlaybackSettings,
        parent: QObject | None = None,
    ) -> MediaController:
        return MediaController(
            settings,
            self.cache_manager,
            downloader_factory=self.create_downloader,
            parent=parent,
        )

    def create_browser_download_service(self) -> BrowserDownloadService:
        return BrowserDownloadService(
            self._media_cache_dir,
            notify_cached=self.cache_manager.notify_cached_threadsafe,
        )

    def create_info_queue(self, parent: QObject) -> MediaInfoQueue:
        from solin.widgets.media_info_extractor import MediaInfoQueue

        return MediaInfoQueue(
            self._media_cache_dir,
            self._thumb_cache_dir,
            parent,
        )

    def create_info_service(self, parent: QObject) -> MediaInfoService:
        from solin.widgets.media_info_extractor import MediaInfoService

        return MediaInfoService(
            self.create_info_queue,
            parent,
        )
