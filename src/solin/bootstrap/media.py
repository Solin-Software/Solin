"""Concrete media-service composition for the application process."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject

from solin.core.foundation.thread_workers import ThreadedWorkerPool
from solin.core.media.browser_downloads import BrowserDownloadService
from solin.core.media.cache import MediaCacheManager
from solin.core.media.downloader import SongDownloader
from solin.core.media.playback import MediaController
from solin.core.media.qt_contracts import PlaybackDownloader
from solin.core.media.settings import MediaPlaybackSettings
from solin.core.network.browser_images import BrowserImageFetchService

if TYPE_CHECKING:
    from solin.ui.media_info import MediaInfoQueue, MediaInfoService

log = logging.getLogger(__name__)


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
        self._media_info_workers = ThreadedWorkerPool()
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
        *,
        projection: bool = True,
    ):
        """Build a playback controller.

        The engine is selected by ``SOLIN_MEDIA_ENGINE``: ``obs`` uses the
        libobs-backed :class:`ObsMediaController`; anything else (default) uses
        the Qt :class:`MediaController`. Both expose the same public surface.

        ``projection`` selects how an ``obs`` engine emits video: the default
        routes the source through the projection program (channel-0 crossfade);
        ``projection=False`` keeps it on a private audio-only channel (for the
        background song player, which is never shown).
        """
        engine = os.environ.get("SOLIN_MEDIA_ENGINE", "").strip().lower()
        if engine == "obs":
            from solin.core.media.obs_playback import ObsMediaController

            log.info("Using libobs media engine (SOLIN_MEDIA_ENGINE=obs)")
            return ObsMediaController(
                settings,
                self.cache_manager,
                downloader_factory=self.create_downloader,
                projection=projection,
                parent=parent,
            )
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

    @staticmethod
    def create_browser_image_fetch_service() -> BrowserImageFetchService:
        return BrowserImageFetchService()

    def create_info_queue(self, parent: QObject) -> MediaInfoQueue:
        from solin.ui.media_info import MediaInfoQueue

        return MediaInfoQueue(
            self._media_cache_dir,
            self._thumb_cache_dir,
            self._media_info_workers,
            parent,
        )

    def create_info_service(self, parent: QObject) -> MediaInfoService:
        from solin.ui.media_info import MediaInfoService

        return MediaInfoService(
            self.create_info_queue,
            parent,
        )

    def shutdown(self) -> None:
        alive = self._media_info_workers.shutdown()
        if alive:
            log.warning("Media info workers still alive after shutdown: %s", alive)
        # obs engine: tear the native runtime down deterministically on app exit
        # (projection program → channels → OBS context, in that order). No-op for
        # the default Qt engine, or if the libobs runtime never started.
        if os.environ.get("SOLIN_MEDIA_ENGINE", "").strip().lower() == "obs":
            try:
                from solin.core.media.obs_runtime import obs_runtime

                obs_runtime().shutdown()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.warning("libobs runtime shutdown errored", exc_info=True)
