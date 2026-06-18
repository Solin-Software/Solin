"""Owned Qt session for downloading JW catalog thumbnails."""

from __future__ import annotations

import logging
from collections import deque

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from .catalog import JWMediaCatalogCachePaths, ensure_thumbnail_cached

log = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENCY = 4


class _ThumbnailFetchSignals(QObject):
    finished = Signal(int, str, str)  # generation, item_id, local_path


class _ThumbnailFetchWorker(QRunnable):
    def __init__(
        self,
        generation: int,
        item_id: str,
        thumbnail_url: str,
        cache_paths: JWMediaCatalogCachePaths,
    ) -> None:
        super().__init__()
        self._generation = generation
        self._item_id = item_id
        self._thumbnail_url = thumbnail_url
        self._cache_paths = cache_paths
        self.signals = _ThumbnailFetchSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            local_path = ensure_thumbnail_cached(
                self._thumbnail_url,
                cache_paths=self._cache_paths,
            )
        except Exception:  # noqa: BLE001 - QRunnable reports failure as an empty result
            log.debug(
                "JW catalog thumbnail download failed for %s",
                self._thumbnail_url,
                exc_info=True,
            )
            local_path = ""
        self.signals.finished.emit(
            self._generation,
            self._item_id,
            local_path,
        )


class JWCatalogThumbnailSession(QObject):
    """Own a bounded thumbnail queue and fence stale results after reset."""

    ready = Signal(str, str)  # item_id, local_path

    def __init__(
        self,
        cache_paths: JWMediaCatalogCachePaths,
        *,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._cache_paths = cache_paths
        self._max_concurrency = max(1, int(max_concurrency))
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(self._max_concurrency)
        self._queue: deque[tuple[int, str, str]] = deque()
        self._pending: set[str] = set()
        self._active_count = 0
        self._generation = 0
        self._closed = False

    def enqueue(self, item_id: str, thumbnail_url: str) -> bool:
        item_id = str(item_id or "")
        thumbnail_url = str(thumbnail_url or "")
        if self._closed or not item_id or not thumbnail_url or item_id in self._pending:
            return False
        self._pending.add(item_id)
        self._queue.append((self._generation, item_id, thumbnail_url))
        self._pump()
        return True

    def reset(self) -> None:
        """Discard queued work and ignore results from the previous generation."""
        if self._closed:
            return
        self._generation += 1
        self._pending.clear()
        self._queue.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self._pending.clear()
        self._queue.clear()
        self._pool.clear()
        self._pool.waitForDone()

    def _pump(self) -> None:
        while (
            not self._closed
            and self._active_count < self._max_concurrency
            and self._queue
        ):
            generation, item_id, thumbnail_url = self._queue.popleft()
            worker = _ThumbnailFetchWorker(
                generation,
                item_id,
                thumbnail_url,
                self._cache_paths,
            )
            worker.signals.finished.connect(self._on_worker_finished)
            self._active_count += 1
            self._pool.start(worker)

    def _on_worker_finished(
        self,
        generation: int,
        item_id: str,
        local_path: str,
    ) -> None:
        self._active_count = max(0, self._active_count - 1)
        if not self._closed and generation == self._generation:
            self._pending.discard(item_id)
            self.ready.emit(item_id, local_path)
        self._pump()


class JWCatalogThumbnailSessionFactory:
    """Create thumbnail sessions bound to the catalog cache roots."""

    def __init__(
        self,
        cache_paths: JWMediaCatalogCachePaths,
        *,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self._cache_paths = cache_paths
        self._max_concurrency = max(1, int(max_concurrency))

    def create(self, *, parent: QObject | None = None) -> JWCatalogThumbnailSession:
        return JWCatalogThumbnailSession(
            self._cache_paths,
            max_concurrency=self._max_concurrency,
            parent=parent,
        )
