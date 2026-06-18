"""
catalog_service.py - Solin
====================================
Qt async adapter for the JW.org media catalog policies.

The synchronous catalog fetch/cache rules live in ``catalog.py``. This module
owns only the Qt worker lifecycle and signal contract used by presentation
surfaces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from solin.core.jw.catalog import (
    JWMediaCatalogCachePaths,
    JWMediaCatalogFetchCancelled,
    JWMediaItem,
    JWMediaQuery,
    catalog_item_for_delivery,
    fetch_jw_video_catalog,
    fetch_jw_videos,
)

log = logging.getLogger(__name__)

_CATALOG_PROGRESS_EMIT_INTERVAL_S = 0.25


class _CancelToken:
    """Small thread-safe cancellation token shared with QRunnable workers."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise JWMediaCatalogFetchCancelled()


class _FetchSignals(QObject):
    succeeded = Signal(str, list, float, bool)  # request_id, list[dict], fetched_at, from_cache
    progress = Signal(str, list, int, int)      # request_id, partial items, completed, total
    failed = Signal(str, str)                   # request_id, error


class _FetchWorker(QRunnable):
    def __init__(
        self,
        request_id: str,
        query: JWMediaQuery,
        *,
        cache_paths: JWMediaCatalogCachePaths,
        force: bool,
        cache_thumbnails: bool,
        cancel_token: _CancelToken,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.query = query
        self.cache_paths = cache_paths
        self.force = force
        self.cache_thumbnails = cache_thumbnails
        self.cancel_token = cancel_token
        self.signals = _FetchSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            self.cancel_token.raise_if_cancelled()
            items, fetched_at, from_cache = fetch_jw_videos(
                self.query,
                cache_paths=self.cache_paths,
                force=self.force,
                cache_thumbnails=self.cache_thumbnails,
            )
            self.cancel_token.raise_if_cancelled()
            self.signals.succeeded.emit(
                self.request_id,
                [item.to_dict() for item in items],
                fetched_at,
                from_cache,
            )
        except JWMediaCatalogFetchCancelled:
            return
        except Exception as exc:  # noqa: BLE001 - QRunnable reports failures via signal
            log.exception("[JWMediaCatalog] Fetch worker failed")
            if not self.cancel_token.is_cancelled():
                self.signals.failed.emit(self.request_id, str(exc))


class _CatalogWorker(QRunnable):
    def __init__(
        self,
        request_id: str,
        language: str,
        *,
        cache_paths: JWMediaCatalogCachePaths,
        force: bool,
        cache_thumbnails: bool,
        cancel_token: _CancelToken,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.language = language
        self.cache_paths = cache_paths
        self.force = force
        self.cache_thumbnails = cache_thumbnails
        self.cancel_token = cancel_token
        self.signals = _FetchSignals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            last_progress_emit = 0.0

            def _progress(items: list[JWMediaItem], completed: int, total: int) -> None:
                nonlocal last_progress_emit
                if self.cancel_token.is_cancelled():
                    raise JWMediaCatalogFetchCancelled()
                now = time.monotonic()
                should_emit = (
                    last_progress_emit <= 0
                    or now - last_progress_emit >= _CATALOG_PROGRESS_EMIT_INTERVAL_S
                    or bool(total and completed >= total)
                )
                if not should_emit:
                    return
                last_progress_emit = now
                emit_items = [
                    catalog_item_for_delivery(
                        item,
                        self.cache_thumbnails,
                        self.cache_paths,
                    ).to_dict()
                    for item in items
                ]
                if self.cancel_token.is_cancelled():
                    raise JWMediaCatalogFetchCancelled()
                self.signals.progress.emit(self.request_id, emit_items, completed, total)

            self.cancel_token.raise_if_cancelled()
            items, fetched_at, from_cache = fetch_jw_video_catalog(
                self.language,
                cache_paths=self.cache_paths,
                force=self.force,
                progress_callback=_progress,
                should_cancel=self.cancel_token.is_cancelled,
            )
            self.cancel_token.raise_if_cancelled()
            emit_items = [
                catalog_item_for_delivery(
                    item,
                    self.cache_thumbnails,
                    self.cache_paths,
                ).to_dict()
                for item in items
            ]
            self.signals.succeeded.emit(self.request_id, emit_items, fetched_at, from_cache)
        except JWMediaCatalogFetchCancelled:
            return
        except Exception as exc:  # noqa: BLE001 - QRunnable reports failures via signal
            log.exception("[JWMediaCatalog] Catalog worker failed")
            if not self.cancel_token.is_cancelled():
                self.signals.failed.emit(self.request_id, str(exc))


class JWMediaCatalogService(QObject):
    """Qt async facade for JW media browsing surfaces."""

    fetch_started = Signal(str)                 # request_id
    videos_ready = Signal(str, list, float, bool)  # request_id, items, fetched_at, from_cache
    videos_progress = Signal(str, list, int, int)  # request_id, items, completed, total
    fetch_failed = Signal(str, str)             # request_id, error

    def __init__(
        self,
        cache_paths: JWMediaCatalogCachePaths,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._cache_paths = cache_paths
        self._active: set[str] = set()
        self._cancel_tokens: dict[str, _CancelToken] = {}
        self._workers: dict[str, QRunnable] = {}
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(2)

    def fetch_videos(
        self,
        query: JWMediaQuery | dict[str, Any],
        *,
        request_id: str = "",
        force: bool = False,
        cache_thumbnails: bool = True,
    ) -> str:
        """Start an async fetch and return the request id."""

        q = _coerce_query(query).normalized()
        rid = request_id or _request_id(q)
        if rid in self._active:
            return rid
        token = _CancelToken()

        worker = _FetchWorker(
            rid,
            q,
            cache_paths=self._cache_paths,
            force=force,
            cache_thumbnails=cache_thumbnails,
            cancel_token=token,
        )
        worker.signals.succeeded.connect(self._on_success)
        worker.signals.failed.connect(self._on_failed)

        self._active.add(rid)
        self._cancel_tokens[rid] = token
        self._workers[rid] = worker
        self.fetch_started.emit(rid)
        self._thread_pool.start(worker)
        return rid

    def fetch_all_videos(
        self,
        language: str,
        *,
        request_id: str = "",
        force: bool = False,
        cache_thumbnails: bool = False,
    ) -> str:
        """Start an async full-catalog fetch and return the request id."""

        lang = (language or "E").upper()
        rid = request_id or _catalog_request_id(lang)
        if rid in self._active:
            return rid
        token = _CancelToken()

        worker = _CatalogWorker(
            rid,
            lang,
            cache_paths=self._cache_paths,
            force=force,
            cache_thumbnails=cache_thumbnails,
            cancel_token=token,
        )
        worker.signals.progress.connect(self._on_progress)
        worker.signals.succeeded.connect(self._on_success)
        worker.signals.failed.connect(self._on_failed)

        self._active.add(rid)
        self._cancel_tokens[rid] = token
        self._workers[rid] = worker
        self.fetch_started.emit(rid)
        self._thread_pool.start(worker)
        return rid

    def cancel(self, request_id: str) -> None:
        """Cancel one active request and suppress any late worker signals."""
        if not request_id:
            return
        token = self._cancel_tokens.pop(request_id, None)
        if token is not None:
            token.cancel()
        self._workers.pop(request_id, None)
        self._active.discard(request_id)

    def cancel_all(self, *, wait_ms: int = 0) -> None:
        """Cancel all active catalog/media fetches owned by this service."""
        for request_id in list(self._active):
            self.cancel(request_id)
        self._thread_pool.clear()
        if wait_ms < 0:
            self._thread_pool.waitForDone()
        elif wait_ms > 0:
            self._thread_pool.waitForDone(wait_ms)

    def _on_success(self, request_id: str, items: list, fetched_at: float, from_cache: bool) -> None:
        if request_id not in self._active:
            return
        self._active.discard(request_id)
        self._cancel_tokens.pop(request_id, None)
        self._workers.pop(request_id, None)
        self.videos_ready.emit(request_id, items, fetched_at, from_cache)

    def _on_progress(self, request_id: str, items: list, completed: int, total: int) -> None:
        if request_id not in self._active:
            return
        self.videos_progress.emit(request_id, items, completed, total)

    def _on_failed(self, request_id: str, error: str) -> None:
        if request_id not in self._active:
            return
        self._active.discard(request_id)
        self._cancel_tokens.pop(request_id, None)
        self._workers.pop(request_id, None)
        self.fetch_failed.emit(request_id, error)


def _coerce_query(query: JWMediaQuery | dict[str, Any]) -> JWMediaQuery:
    if isinstance(query, JWMediaQuery):
        return query
    return JWMediaQuery(**dict(query))


def _request_id(query: JWMediaQuery) -> str:
    raw = json.dumps(asdict(query), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _catalog_request_id(language: str) -> str:
    raw = f"catalog|{language}|{time.time_ns()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


__all__ = ["JWMediaCatalogService"]
