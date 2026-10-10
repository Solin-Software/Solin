"""
MediaCacheManager — manage remote media caching and prefetching.

Centralize the global prefetch queue so batch actions do not create dozens
of simultaneous SongDownloader instances. UIs query active/queued states
separately: active shows progress; queued shows waiting without a misleading spinner.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from PySide6.QtCore import QObject, Signal, Slot

from .application import (
    MediaPrefetchQueue,
    PrefetchAction,
    PrefetchPlan,
    QueuedPrefetch,
)
from .qt_contracts import PrefetchDownloader, PrefetchDownloaderFactory
from .download_storage import completed_cached_path, is_remote_url, is_url_cached


def remove_cached_entry(
    media_cache_dir: str | os.PathLike[str],
    path: str | os.PathLike[str],
) -> bool:
    """Safely remove one regular cache file and its completion sidecar."""

    if not path:
        return False
    cache_root = os.path.normcase(os.path.abspath(os.fspath(media_cache_dir)))
    media_path = os.path.normcase(os.path.abspath(os.fspath(path)))
    try:
        inside_cache = os.path.commonpath((cache_root, media_path)) == cache_root
    except ValueError:
        inside_cache = False
    if not inside_cache or media_path == cache_root:
        raise ValueError("Cached media path is outside the media cache directory")

    existing_targets: list[str] = []
    # Remove the sidecar first so a sidecar permission failure cannot leave a
    # deleted media file represented as a failed, stale row in the UI.
    for target in (f"{media_path}.done", media_path):
        try:
            mode = os.lstat(target).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("Cached media targets must be regular files")
        existing_targets.append(target)

    for target in existing_targets:
        os.remove(target)
    return bool(existing_targets)


class MediaCacheManager(QObject):
    """Owns the application media-cache queue on the Qt main thread."""

    _notify_cached_requested = Signal(str)
    # URL now cached (via prefetch or a player notification)
    cache_changed      = Signal(str)
    # local path removed from cache (main file; .done is removed too)
    cache_removed      = Signal(str)
    _cache_removed_requested = Signal(str)
    # prefetch progress: (url, bytes_downloaded, bytes_total)
    prefetch_progress  = Signal(str, int, int)
    # erro no prefetch: (url, mensagem)
    prefetch_error     = Signal(str, str)
    # URL waiting for a queue slot
    prefetch_queued    = Signal(str)
    # URL left the pending queue (started, canceled, or discarded)
    prefetch_dequeued  = Signal(str)
    # status de lote: batch_id, queued, active, done, failed
    prefetch_batch_changed = Signal(str, int, int, int, int)
    # batch error emitted once when the remaining work is aborted
    prefetch_batch_error = Signal(str, str)

    def __init__(
        self,
        media_cache_dir: str | os.PathLike[str],
        *,
        downloader_factory: PrefetchDownloaderFactory,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cache_removed_requested.connect(self.cache_removed.emit)
        self.media_cache_dir = Path(media_cache_dir)
        self._prefetch_queue = MediaPrefetchQueue(self)
        self._downloaders: dict[str, PrefetchDownloader] = {}
        self._downloader_factory = downloader_factory
        self._notify_cached_requested.connect(self.notify_cached)

    @property
    def max_concurrent_prefetches(self) -> int:
        return self._prefetch_queue.max_concurrent

    @max_concurrent_prefetches.setter
    def max_concurrent_prefetches(self, value: int) -> None:
        self._prefetch_queue.max_concurrent = max(0, int(value))

    # Public API

    def is_cached(self, url: str) -> bool:
        return is_url_cached(url, self.media_cache_dir)

    def cached_path(self, url: str) -> str | None:
        """Return the verified completed cache path for an exact URL."""
        return completed_cached_path(url, self.media_cache_dir)

    @staticmethod
    def is_remote(url: str) -> bool:
        """True if the URL uses HTTP/HTTPS and is therefore cacheable."""
        return is_remote_url(url)

    def is_prefetching(self, url: str) -> bool:
        """True if a prefetch is active for the URL."""
        return self._prefetch_queue.is_prefetching(url)

    def is_queued(self, url: str) -> bool:
        """True if the URL is waiting for a prefetch queue slot."""
        return self._prefetch_queue.is_queued(url)

    @Slot(str)
    def prefetch(self, url: str, priority: bool = False) -> None:
        """
        Start a background download to pre-cache media.
        Do nothing if already cached; wait for a slot if the queue is full.
        """
        self._apply_plan(self._prefetch_queue.prefetch(url, priority=priority))

    def prefetch_many(self, urls: list[str], batch_id: str) -> int:
        """Enqueue a deduplicated batch and return the number of URLs added."""
        plan = self._prefetch_queue.prefetch_many(urls, batch_id)
        self._apply_plan(plan)
        return plan.added

    def batch_counts(self, batch_id: str) -> tuple[int, int, int, int]:
        return self._prefetch_queue.batch_counts(batch_id)

    def batch_is_active(self, batch_id: str) -> bool:
        return self._prefetch_queue.batch_is_active(batch_id)

    def cancel_prefetch(self, url: str) -> None:
        """
        Cancel the active prefetch for the URL, if any.
        MediaController must call this BEFORE starting its own download
        to avoid concurrent writes to the same .tmp file.
        """
        self._apply_plan(self._prefetch_queue.cancel_prefetch(url))

    def cancel_all(self) -> None:
        """Cancel all active prefetches (e.g. when closing the application)."""
        self._apply_plan(self._prefetch_queue.cancel_all())

    def cancel_batch(self, batch_id: str) -> None:
        self._apply_plan(self._prefetch_queue.cancel_batch(batch_id))

    def notify_cached(self, url: str) -> None:
        """
        Called by MediaController when ITS download finishes.
        Ensure any concurrent prefetch is removed from the dict and emit
        cache_changed to update the UI.
        """
        self._downloaders.pop(url, None)
        self._apply_plan(self._prefetch_queue.notify_cached(url))

    def notify_cached_threadsafe(self, url: str) -> None:
        """Queue a cache notification onto the manager's owning Qt thread."""
        self._notify_cached_requested.emit(url)

    def remove_cached_file(self, path: str) -> bool:
        """
        Remove a cached file and its .done marker, emitting cache_removed.

        Return True if the main file or marker was removed. Centralizing this
        flow prevents stale UI state after manual deletion.
        """
        removed = remove_cached_entry(self.media_cache_dir, path)

        if removed:
            self.cache_removed.emit(path)
        return removed

    def notify_cache_removed_threadsafe(self, path: str) -> None:
        """Publish a worker-thread cache removal on this object's Qt thread."""

        if path:
            self._cache_removed_requested.emit(path)

    # ── Slots internos ────────────────────────────────────────────────────

    def _create_downloader(self) -> PrefetchDownloader:
        return self._downloader_factory(self)

    def _pump_queue(self) -> None:
        self._apply_plan(self._prefetch_queue.pump())

    def _start_entry(self, entry: QueuedPrefetch) -> None:
        dl = self._create_downloader()
        dl.progress.connect(
            lambda d, t, u=entry.url: self._on_prefetch_progress(u, d, t)
        )
        dl.finished.connect(
            lambda path, u=entry.url: self._on_prefetch_done(u)
        )
        dl.error.connect(
            lambda msg, u=entry.url: self._on_prefetch_error(u, msg)
        )
        self._downloaders[entry.url] = dl
        dl.start(entry.url)

    def _on_prefetch_progress(self, url: str, downloaded: int, total: int) -> None:
        self._apply_plan(
            self._prefetch_queue.start_progress(url, downloaded, total)
        )

    def _on_prefetch_done(self, url: str) -> None:
        self._downloaders.pop(url, None)
        self._apply_plan(self._prefetch_queue.complete(url))

    def _on_prefetch_error(self, url: str, msg: str) -> None:
        self._downloaders.pop(url, None)
        self._apply_plan(self._prefetch_queue.fail(url, msg))

    def _emit_batch_changed(self, batch_id: str) -> None:
        if not batch_id:
            return
        queued, active, done, failed = self.batch_counts(batch_id)
        self.prefetch_batch_changed.emit(batch_id, queued, active, done, failed)

    def _apply_plan(self, plan: PrefetchPlan) -> None:
        for action in plan.actions:
            self._apply_action(action)

    def _apply_action(self, action: PrefetchAction) -> None:
        if action.kind == "queued":
            self.prefetch_queued.emit(action.url)
        elif action.kind == "dequeued":
            self.prefetch_dequeued.emit(action.url)
        elif action.kind == "cache_changed":
            self.cache_changed.emit(action.url)
        elif action.kind == "progress":
            self.prefetch_progress.emit(action.url, action.downloaded, action.total)
        elif action.kind == "error":
            self.prefetch_error.emit(action.url, action.message)
        elif action.kind == "batch_changed":
            self._emit_batch_changed(action.batch_id)
        elif action.kind == "batch_error":
            self.prefetch_batch_error.emit(action.batch_id, action.message)
        elif action.kind == "cancel_active":
            downloader = self._downloaders.pop(action.url, None)
            if downloader is not None:
                downloader.cancel()
        elif action.kind == "start" and action.entry is not None:
            self._start_entry(action.entry)
