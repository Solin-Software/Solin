"""
catalog.py - Solin
====================================
Reusable JW.org media catalog service.

This module is intentionally independent from playlists and QML.  It provides
one normalized contract for future UI surfaces that need to browse JW videos,
show thumbnails, and later decide what to do with the selected item.

Public layers:
  - JWMediaQuery / JWMediaItem dataclasses for synchronous, testable code.
  - fetch_jw_videos() for direct callers.
  - JWMediaCatalogService for Qt async callers.

Thumbnail policy:
  API thumbnails are cached under cache/thumbs/jw_catalog/ using a SHA-256 of
  the source URL.  The original API URL remains on the item, while
  thumbnail_path points to the local cached file when available.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from solin.core.foundation.constants import (
    CACHE_TTL_DAYS,
    VIDEO_PREFERRED_QUALITY,
    VIDEO_QUALITY_FALLBACK_DIR,
    VIDEO_QUALITY_ORDER,
)
from solin.core.network.http import HttpDecodeError, HttpError, get_json, stream_get

log = logging.getLogger(__name__)

_USER_AGENT = "Solin/1.0"
_FETCH_TIMEOUT_S = 15
_THUMB_TIMEOUT_S = 15
_MAX_THUMB_BYTES = 5 * 1024 * 1024
_CATALOG_REFRESH_INTERVAL_S = 12 * 60 * 60
_CATALOG_PROGRESS_EMIT_INTERVAL_S = 0.25
# Set to False to use the previous full-refresh behavior for stale catalogs.
USE_LATEST_DELTA_CATALOG_REFRESH = True
_LATEST_CATEGORY_KEY = "LatestVideos"

_PUB_MEDIA_URL = "https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS"
_MEDIATOR_BASE_URL = "https://b.jw-cdn.org/apis/mediator"
_VIDEO_FORMATS = ("MP4", "M4V")
_QUALITY_TITLE_RE = re.compile(r"^\d+[pP]$|^\d+kbps$", re.I)

_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass(frozen=True)
class JWMediaCatalogCachePaths:
    """Filesystem roots used by the JW media catalog cache."""

    cache_dir: Path
    thumb_cache_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "cache_dir", Path(self.cache_dir))
        object.__setattr__(self, "thumb_cache_dir", Path(self.thumb_cache_dir))

    @property
    def catalog_dir(self) -> Path:
        return self.cache_dir / "jw_media_catalog"

    @property
    def thumbnail_dir(self) -> Path:
        return self.thumb_cache_dir / "jw_catalog"


class _FetchCancelled(Exception):
    """Internal sentinel used to stop cooperative background fetches."""


_EXPECTED_FETCH_ERRORS = (
    HttpError,
    OSError,
    UnicodeError,
    ValueError,
)


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
            raise _FetchCancelled()


@dataclass(frozen=True)
class JWMediaQuery:
    """Identifies a JW media request.

    Use pub for regular publication symbols (for example "sjjm", "osg").
    Use docid for document-addressed media.  If track is omitted, all tracks
    returned by GETPUBMEDIALINKS are grouped and normalized.
    """

    language: str
    pub: str | None = None
    docid: int | str | None = None
    issue: int | str | None = None
    track: int | str | None = None
    fileformat: str = "MP4"
    category: str | None = None
    max_items: int | None = None

    def normalized(self) -> "JWMediaQuery":
        return JWMediaQuery(
            language=(self.language or "E").upper(),
            pub=(self.pub or "").strip().lower() or None,
            docid=self.docid,
            issue=self.issue,
            track=self.track,
            fileformat=(self.fileformat or "MP4").upper(),
            category=(self.category or "").strip() or None,
            max_items=self.max_items,
        )


@dataclass(frozen=True)
class JWMediaItem:
    """Normalized JW video metadata for UI and playlist-adjacent callers."""

    id: str
    title: str
    language: str
    media_type: str
    source: str
    download_url: str
    thumbnail_url: str = ""
    thumbnail_path: str = ""
    subtitles_url: str = ""
    duration_seconds: float = 0.0
    duration_ticks: int = 0
    filesize: int = 0
    label: str = ""
    frame_width: int = 0
    frame_height: int = 0
    pub: str = ""
    docid: str = ""
    issue: str = ""
    track: str = ""
    guid: str = ""
    natural_key: str = ""
    primary_category: str = ""
    first_published: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fetch_jw_videos(
    query: JWMediaQuery,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool = False,
    cache_thumbnails: bool = True,
) -> tuple[list[JWMediaItem], float, bool]:
    """Fetch and normalize JW videos.

    Returns (items, fetched_at, from_cache).  The cache applies to API JSON
    responses.  Thumbnails have their own independent file cache and are reused
    whenever possible.
    """

    normalized = query.normalized()
    if normalized.category:
        raw, fetched_at, from_cache = _fetch_category(
            normalized,
            cache_paths=cache_paths,
            force=force,
        )
        items = _parse_category_media(raw, normalized)
    else:
        raw, fetched_at, from_cache = _fetch_pub_media_links(
            normalized,
            cache_paths=cache_paths,
            force=force,
        )
        items = _parse_pub_media_links(raw, normalized, cache_paths)

    if normalized.max_items is not None:
        items = items[: max(0, normalized.max_items)]

    if cache_thumbnails:
        items = [_with_cached_thumbnail(item, cache_paths) for item in items]
    else:
        items = [_with_existing_thumbnail(item, cache_paths) for item in items]

    return items, fetched_at, from_cache


def fetch_jw_video_catalog(
    language: str,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool = False,
    progress_callback: Callable[[list[JWMediaItem], int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[JWMediaItem], float, bool]:
    """Fetch the full JW video catalog for *language*.

    This mirrors the production flow used by M3:
      1. Start with LatestVideos.
      2. Discover first-level VideoOnDemand subcategories.
      3. Discover one additional subcategory level.
      4. Fetch every discovered category, dedupe by guid/id, and sort newest first.

    A partial snapshot is written after every category.  If the app closes mid
    fetch, the next run reuses completed category caches and the partial
    snapshot before continuing the remaining categories.
    """

    lang = (language or "E").upper()

    def raise_if_cancelled() -> None:
        if should_cancel and should_cancel():
            raise _FetchCancelled()

    raise_if_cancelled()

    snapshot = None if force else _load_catalog_snapshot(lang, cache_paths)
    items_by_id: dict[str, JWMediaItem] = {}
    completed: set[str] = set()
    fetched_at = time.time()
    from_cache = False

    loaded_completed: set[str] = set()
    snapshot_age_s = 0.0

    if snapshot:
        from_cache = True
        fetched_at = float(snapshot.get("_fetched_at", fetched_at) or fetched_at)
        snapshot_age_s = max(0.0, time.time() - fetched_at)
        for item_data in snapshot.get("items", []):
            if isinstance(item_data, dict):
                try:
                    item = JWMediaItem(**item_data)
                    items_by_id[item.guid or item.id] = item
                except TypeError:
                    continue
        loaded_completed = set(snapshot.get("completed_categories", []))

    raise_if_cancelled()
    categories = _discover_video_categories(
        lang,
        cache_paths=cache_paths,
        force=force,
        should_cancel=should_cancel,
    )
    total = len(categories)
    required_categories = set(categories)
    snapshot_complete = bool(required_categories) and required_categories.issubset(loaded_completed)
    refresh_required = bool(
        snapshot
        and snapshot_complete
        and snapshot_age_s >= _CATALOG_REFRESH_INTERVAL_S
    )

    if refresh_required and USE_LATEST_DELTA_CATALOG_REFRESH:
        latest_refresh = _try_refresh_catalog_from_latest(
            lang,
            items_by_id=items_by_id,
            loaded_completed=loaded_completed,
            categories=categories,
            cache_paths=cache_paths,
            should_cancel=should_cancel,
        )
        if latest_refresh is not None:
            sorted_items, latest_fetched_at = latest_refresh
            if progress_callback:
                progress_callback(sorted_items, total, total)
            return sorted_items, latest_fetched_at, True

    if refresh_required:
        raise_if_cancelled()
        categories = _discover_video_categories(
            lang,
            cache_paths=cache_paths,
            force=True,
            should_cancel=should_cancel,
        )
        total = len(categories)
        required_categories = set(categories)
        snapshot_complete = bool(required_categories) and required_categories.issubset(loaded_completed)

    # Partial snapshots are resumable checkpoints, not freshness snapshots.
    # Even if the checkpoint is older than the normal refresh interval, keep
    # its completed category set so the next launch continues where it stopped.
    completed = set() if refresh_required else set(loaded_completed)

    if snapshot and progress_callback and items_by_id:
        progress_callback(
            _sort_catalog_items(items_by_id.values()),
            0 if refresh_required else len(loaded_completed),
            total,
        )

    # If a previous snapshot was complete and fresh enough, return it immediately.
    if snapshot and snapshot_complete and not refresh_required:
        return _sort_catalog_items(items_by_id.values()), fetched_at, True

    for idx, category in enumerate(categories, start=1):
        raise_if_cancelled()
        if category in completed:
            continue

        try:
            raw, category_fetched_at, cached = _fetch_category_media(
                lang,
                category,
                cache_paths=cache_paths,
                force=force or refresh_required,
                should_cancel=should_cancel,
            )
            raise_if_cancelled()
            fetched_at = max(fetched_at, category_fetched_at)
            from_cache = from_cache or cached
            for item in _parse_category_media(raw, JWMediaQuery(language=lang, category=category)):
                items_by_id[item.guid or item.id] = item
            completed.add(category)
            sorted_items = _sort_catalog_items(items_by_id.values())
            _save_catalog_snapshot(
                lang,
                sorted_items,
                completed,
                categories,
                fetched_at,
                cache_paths,
            )
            if progress_callback:
                progress_callback(sorted_items, idx, total)
        except _FetchCancelled:
            raise
        except _EXPECTED_FETCH_ERRORS as exc:
            log.debug("[JWMediaCatalog] Skipping category %s: %s", category, exc)
            # Keep going. A single removed/empty category should not break the
            # whole catalog; this also handles transient 404s gracefully.
            continue

    raise_if_cancelled()
    sorted_items = _sort_catalog_items(items_by_id.values())
    _save_catalog_snapshot(
        lang,
        sorted_items,
        completed,
        categories,
        time.time(),
        cache_paths,
    )
    return sorted_items, time.time(), from_cache


def _try_refresh_catalog_from_latest(
    language: str,
    *,
    items_by_id: dict[str, JWMediaItem],
    loaded_completed: set[str],
    categories: list[str],
    cache_paths: JWMediaCatalogCachePaths,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[JWMediaItem], float] | None:
    def raise_if_cancelled() -> None:
        if should_cancel and should_cancel():
            raise _FetchCancelled()

    raise_if_cancelled()
    try:
        latest_raw, latest_fetched_at, latest_from_cache = _fetch_category_media(
            language,
            _LATEST_CATEGORY_KEY,
            cache_paths=cache_paths,
            force=True,
            should_cancel=should_cancel,
        )
        raise_if_cancelled()
        latest_items = _parse_category_media(
            latest_raw,
            JWMediaQuery(language=language, category=_LATEST_CATEGORY_KEY),
        )
    except _FetchCancelled:
        raise
    except _EXPECTED_FETCH_ERRORS as exc:
        log.debug("[JWMediaCatalog] Latest delta refresh unavailable: %s", exc)
        return None

    if latest_from_cache or not latest_items:
        return None

    known_identities = _catalog_identity_index(items_by_id.values())
    new_latest_items = [
        item for item in latest_items
        if _catalog_identity_values(item).isdisjoint(known_identities)
    ]

    if len(new_latest_items) == len(latest_items):
        log.debug(
            "[JWMediaCatalog] Latest delta found only new items; doing full refresh (%s)",
            len(new_latest_items),
        )
        return None

    for item in new_latest_items:
        items_by_id[item.guid or item.id] = item
        known_identities.update(_catalog_identity_values(item))

    sorted_items = _sort_catalog_items(items_by_id.values())
    checked_at = max(time.time(), latest_fetched_at)
    _save_catalog_snapshot(
        language,
        sorted_items,
        loaded_completed,
        categories,
        checked_at,
        cache_paths,
    )
    log.debug(
        "[JWMediaCatalog] Latest delta refresh added %s new items",
        len(new_latest_items),
    )
    return sorted_items, checked_at


def ensure_thumbnail_cached(
    thumbnail_url: str,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool = False,
) -> str:
    """Return a local cached thumbnail path, downloading it if needed."""

    if not thumbnail_url:
        return ""

    existing = cached_thumbnail_path(thumbnail_url, cache_paths=cache_paths)
    if existing and not force:
        return existing

    cache_dir = os.fspath(_thumbnail_cache_dir(cache_paths))
    os.makedirs(cache_dir, exist_ok=True)

    guessed_ext = _guess_image_ext(thumbnail_url, "")
    cache_key = hashlib.sha256(thumbnail_url.encode("utf-8")).hexdigest()
    temp_path = os.path.join(cache_dir, f"{cache_key}.tmp")

    try:
        with stream_get(
            thumbnail_url,
            timeout=_THUMB_TIMEOUT_S,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        ) as resp:
            content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].lower()
            ext = _guess_image_ext(thumbnail_url, content_type) or guessed_ext
            final_path = os.path.join(cache_dir, f"{cache_key}{ext}")
            if os.path.exists(final_path) and not force:
                return final_path

            total = 0
            with open(temp_path, "wb") as f:
                for chunk in resp.iter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > _MAX_THUMB_BYTES:
                        raise ValueError("Thumbnail is larger than the allowed cache limit")
                    f.write(chunk)

        if os.path.getsize(temp_path) <= 0:
            raise ValueError("Thumbnail response was empty")

        os.replace(temp_path, final_path)
        return final_path
    except _EXPECTED_FETCH_ERRORS as exc:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        log.debug("[JWMediaCatalog] Thumbnail cache failed for %s: %s", thumbnail_url, exc)
        return ""


def cached_thumbnail_path(
    thumbnail_url: str,
    *,
    cache_paths: JWMediaCatalogCachePaths,
) -> str:
    """Return an existing thumbnail cache path, or an empty string."""

    if not thumbnail_url:
        return ""
    cache_dir = os.fspath(_thumbnail_cache_dir(cache_paths))
    cache_key = hashlib.sha256(thumbnail_url.encode("utf-8")).hexdigest()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        path = os.path.join(cache_dir, f"{cache_key}{ext}")
        if os.path.isfile(path):
            return path
    return ""


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
        except _FetchCancelled:
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
                    raise _FetchCancelled()
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
                    _item_for_emit(
                        item,
                        self.cache_thumbnails,
                        self.cache_paths,
                    ).to_dict()
                    for item in items
                ]
                if self.cancel_token.is_cancelled():
                    raise _FetchCancelled()
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
                _item_for_emit(
                    item,
                    self.cache_thumbnails,
                    self.cache_paths,
                ).to_dict()
                for item in items
            ]
            self.signals.succeeded.emit(self.request_id, emit_items, fetched_at, from_cache)
        except _FetchCancelled:
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
        parent: Optional[QObject] = None,
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
        rid = request_id or _stable_item_id("catalog", lang, str(time.time_ns()))
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

    def stop(self, wait_ms: int = 0) -> None:
        """Compatibility alias for host widgets that expose cleanup hooks."""
        self.cancel_all(wait_ms=wait_ms)

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


def _fetch_pub_media_links(
    query: JWMediaQuery,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool,
) -> tuple[dict[str, Any], float, bool]:
    if not query.pub and not query.docid:
        raise ValueError("JWMediaQuery requires pub or docid unless category is set")

    fileformat = query.fileformat.upper()
    if fileformat in _VIDEO_FORMATS:
        fileformat_param = ",".join(_VIDEO_FORMATS)
    else:
        fileformat_param = fileformat

    params = {
        "output": "json",
        "alllangs": "0",
        "langwritten": query.language,
        "txtCMSLang": query.language,
        "fileformat": fileformat_param,
    }
    if query.pub:
        params["pub"] = query.pub
    if query.docid is not None:
        params["docid"] = str(query.docid)
    if query.issue not in (None, "", 0, "0"):
        params["issue"] = str(query.issue)
    if query.track not in (None, ""):
        params["track"] = str(query.track)

    url = f"{_PUB_MEDIA_URL}?{urllib.parse.urlencode(params)}"
    return _fetch_json_cached(
        url,
        "pub_media",
        cache_paths=cache_paths,
        force=force,
    )


def _fetch_category(
    query: JWMediaQuery,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool,
) -> tuple[dict[str, Any], float, bool]:
    if not query.category:
        raise ValueError("JWMediaQuery.category is required")
    category = urllib.parse.quote(query.category, safe="")
    lang = urllib.parse.quote(query.language, safe="")
    url = f"{_MEDIATOR_BASE_URL}/v1/categories/{lang}/{category}?detailed=1&clientType=www"
    return _fetch_json_cached(
        url,
        "mediator_category",
        cache_paths=cache_paths,
        force=force,
    )


def _fetch_category_media(
    language: str,
    category: str,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], float, bool]:
    lang = urllib.parse.quote(language, safe="")
    cat = urllib.parse.quote(category, safe="")
    url = f"{_MEDIATOR_BASE_URL}/v1/categories/{lang}/{cat}?clientType=www&detailed=0"
    return _fetch_json_cached(
        url,
        "mediator_category_media",
        cache_paths=cache_paths,
        force=force,
        should_cancel=should_cancel,
    )


def _fetch_category_tree(
    language: str,
    category: str,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], float, bool]:
    lang = urllib.parse.quote(language, safe="")
    cat = urllib.parse.quote(category, safe="")
    url = (
        f"{_MEDIATOR_BASE_URL}/v1/categories/{lang}/{cat}"
        "?clientType=www&detailed=1&mediaLimit=0"
    )
    return _fetch_json_cached(
        url,
        "mediator_category_tree",
        cache_paths=cache_paths,
        force=force,
        should_cancel=should_cancel,
    )


def _discover_video_categories(
    language: str,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool,
    should_cancel: Callable[[], bool] | None = None,
) -> list[str]:
    categories = ["LatestVideos"]

    def raise_if_cancelled() -> None:
        if should_cancel and should_cancel():
            raise _FetchCancelled()

    def add_category(key: str) -> None:
        if key and key not in categories:
            categories.append(key)

    try:
        raise_if_cancelled()
        root, _, _ = _fetch_category_tree(
            language,
            "VideoOnDemand",
            cache_paths=cache_paths,
            force=force,
            should_cancel=should_cancel,
        )
        raise_if_cancelled()
        first_level = _subcategory_keys(root)
        for first in first_level:
            raise_if_cancelled()
            add_category(first)
            try:
                child, _, _ = _fetch_category_tree(
                    language,
                    first,
                    cache_paths=cache_paths,
                    force=force,
                    should_cancel=should_cancel,
                )
                raise_if_cancelled()
                for second in _subcategory_keys(child):
                    add_category(second)
            except _FetchCancelled:
                raise
            except _EXPECTED_FETCH_ERRORS as exc:
                log.debug("[JWMediaCatalog] Could not inspect category %s: %s", first, exc)
    except _FetchCancelled:
        raise
    except _EXPECTED_FETCH_ERRORS as exc:
        log.debug("[JWMediaCatalog] Could not inspect VideoOnDemand: %s", exc)

    return categories


def _subcategory_keys(data: dict[str, Any]) -> list[str]:
    category = data.get("category") if isinstance(data, dict) else None
    subs = category.get("subcategories", []) if isinstance(category, dict) else []
    keys: list[str] = []
    if isinstance(subs, list):
        for sub in subs:
            if isinstance(sub, dict) and sub.get("key"):
                keys.append(str(sub["key"]))
    return keys


def _fetch_media_item(
    query: JWMediaQuery,
    track: str,
    cache_paths: JWMediaCatalogCachePaths,
) -> dict[str, Any] | None:
    for item_id in _media_item_ids(query, track):
        url = (
            f"{_MEDIATOR_BASE_URL}/v1/media-items/"
            f"{urllib.parse.quote(query.language, safe='')}/"
            f"{urllib.parse.quote(item_id, safe='')}"
        )
        try:
            data, _, _ = _fetch_json_cached(
                url,
                "mediator_item",
                cache_paths=cache_paths,
                force=False,
            )
        except _EXPECTED_FETCH_ERRORS:
            continue
        media = data.get("media") if isinstance(data, dict) else None
        if isinstance(media, list) and media:
            first = media[0]
            if isinstance(first, dict):
                return first
    return None


def _media_item_ids(query: JWMediaQuery, track: str) -> Iterable[str]:
    source = f"pub-{query.pub}" if query.pub else f"docid-{query.docid}"
    issue = _normalize_issue(query.issue)
    media_type = _publication_type(query.fileformat)
    primary_parts = [
        source,
        issue if query.pub and issue else None,
        track if track not in ("", "None") else None,
        media_type,
    ]
    primary = "_".join(str(part) for part in primary_parts if part is not None)
    yield primary

    # Some mediator entries use "x", 0, or 1 when pub-media did not expose a
    # conventional track.  This mirrors the resilient M3 lookup strategy.
    for fallback_track in ("x", "0", "1"):
        parts = [source, issue if query.pub and issue else None, fallback_track, media_type]
        candidate = "_".join(str(part) for part in parts if part is not None)
        if candidate != primary:
            yield candidate


def _publication_type(fileformat: str) -> str:
    fmt = (fileformat or "").upper()
    if "MP4" in fmt or "M4V" in fmt:
        return "VIDEO"
    if "MP3" in fmt:
        return "AUDIO"
    return "VIDEO"


def _normalize_issue(issue: int | str | None) -> str:
    if issue in (None, "", 0, "0"):
        return ""
    raw = str(issue)
    return re.sub(r"(\d{6})00$", r"\1", raw)


def _parse_pub_media_links(
    data: dict[str, Any],
    query: JWMediaQuery,
    cache_paths: JWMediaCatalogCachePaths,
) -> list[JWMediaItem]:
    entries = _pub_media_entries(data, query.language, query.fileformat)
    if not entries:
        return []

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = _entry_url(entry)
        if not url:
            continue
        track = str(entry.get("track") or query.track or "")
        grouped.setdefault(track, []).append(entry)

    items: list[JWMediaItem] = []
    for track, track_entries in sorted(grouped.items(), key=lambda pair: _track_sort_key(pair[0])):
        best = _select_best_entry(track_entries, query.fileformat)
        if not best:
            continue
        media_info = _fetch_media_item(query, track, cache_paths) if track else None
        items.append(_make_item_from_pub_entry(best, query, track, media_info))
    return items


def _parse_category_media(data: dict[str, Any], query: JWMediaQuery) -> list[JWMediaItem]:
    category = data.get("category") if isinstance(data, dict) else None
    media = category.get("media", []) if isinstance(category, dict) else []
    if not isinstance(media, list):
        return []

    items: list[JWMediaItem] = []
    for idx, raw_item in enumerate(media):
        if not isinstance(raw_item, dict):
            continue
        files = raw_item.get("files", [])
        if not isinstance(files, list):
            continue
        best = _select_best_entry(files, query.fileformat)
        if not best:
            continue
        items.append(_make_item_from_category(raw_item, best, query, idx))
    return items


def _pub_media_entries(data: dict[str, Any], language: str, fileformat: str) -> list[dict[str, Any]]:
    files = data.get("files", {}) if isinstance(data, dict) else {}
    if not isinstance(files, dict):
        return []

    formats = _VIDEO_FORMATS if fileformat.upper() in _VIDEO_FORMATS else (fileformat.upper(),)
    entries: list[dict[str, Any]] = []

    lang_section = files.get(language, {})
    if isinstance(lang_section, dict):
        for fmt in formats:
            candidates = lang_section.get(fmt, [])
            if isinstance(candidates, list):
                entries.extend(c for c in candidates if isinstance(c, dict))

    if entries:
        return entries

    for section in files.values():
        if not isinstance(section, dict):
            continue
        for fmt in formats:
            candidates = section.get(fmt, [])
            if isinstance(candidates, list):
                entries.extend(c for c in candidates if isinstance(c, dict))
        if entries:
            break

    return entries


def _select_best_entry(entries: list[dict[str, Any]], fileformat: str) -> dict[str, Any] | None:
    usable = [entry for entry in entries if _entry_url(entry)]
    if not usable:
        return None

    fmt = fileformat.upper()
    if "MP3" in fmt:
        for label in ("320kbps", "256kbps", "192kbps", "128kbps", "96kbps", "64kbps", "32kbps"):
            for entry in usable:
                if entry.get("label") == label and entry.get("subtitled") is not True:
                    return entry
        return usable[0]

    labels = {entry.get("label") for entry in usable if entry.get("label")}
    order = list(VIDEO_QUALITY_ORDER)
    if VIDEO_PREFERRED_QUALITY in order:
        idx = order.index(VIDEO_PREFERRED_QUALITY)
        above = [q for q in order[:idx] if q in labels]
        below = [q for q in order[idx + 1 :] if q in labels]
    else:
        above = [q for q in order if q in labels]
        below = []

    quality_order = [VIDEO_PREFERRED_QUALITY]
    quality_order += above + below if VIDEO_QUALITY_FALLBACK_DIR == "above" else below + above
    quality_order += [label for label in labels if label not in quality_order]

    for quality in quality_order:
        for entry in usable:
            if entry.get("label") == quality and entry.get("subtitled") is not True:
                return entry

    for quality in quality_order:
        for entry in usable:
            if entry.get("label") == quality:
                return entry

    return usable[0]


def _make_item_from_pub_entry(
    entry: dict[str, Any],
    query: JWMediaQuery,
    track: str,
    media_info: dict[str, Any] | None,
) -> JWMediaItem:
    url = _entry_url(entry)
    title = _clean_title(
        _first_str(
            media_info.get("title") if media_info else "",
            entry.get("title"),
            entry.get("label"),
            Path(url.split("?", 1)[0]).stem,
        )
    )
    thumb_url = _best_image_url(media_info.get("images", {})) if media_info else ""
    best_media_file = _best_file_from_media_item(media_info, query.fileformat) if media_info else None
    subtitles_url = _subtitles_url(best_media_file)
    duration = _float_or_zero(
        (media_info or {}).get("duration")
        or entry.get("duration")
        or (entry.get("file") or {}).get("duration")
    )
    item_id = _stable_item_id("pub", query.language, query.pub, query.docid, query.issue, track, url)

    return JWMediaItem(
        id=item_id,
        title=title,
        language=query.language,
        media_type="video" if _publication_type(query.fileformat) == "VIDEO" else "audio",
        source="pub-media",
        download_url=url,
        thumbnail_url=thumb_url,
        subtitles_url=subtitles_url,
        duration_seconds=duration,
        duration_ticks=int(duration * 10_000_000) if duration else 0,
        filesize=_int_or_zero(entry.get("filesize") or (entry.get("file") or {}).get("filesize")),
        label=str(entry.get("label") or ""),
        frame_width=_int_or_zero(entry.get("frameWidth") or entry.get("frame_width")),
        frame_height=_int_or_zero(entry.get("frameHeight") or entry.get("frame_height")),
        pub=query.pub or "",
        docid=str(query.docid or ""),
        issue=str(query.issue or ""),
        track=track,
    )


def _make_item_from_category(
    raw_item: dict[str, Any],
    file_entry: dict[str, Any],
    query: JWMediaQuery,
    index: int,
) -> JWMediaItem:
    url = _entry_url(file_entry)
    duration = _float_or_zero(raw_item.get("duration") or file_entry.get("duration"))
    title = _clean_title(_first_str(raw_item.get("title"), file_entry.get("title"), Path(url).stem))
    guid = str(raw_item.get("guid") or raw_item.get("naturalKey") or "")
    item_id = _stable_item_id("cat", query.language, guid or query.category, index, url)

    return JWMediaItem(
        id=item_id,
        title=title,
        language=query.language,
        media_type="video",
        source=f"category:{query.category or ''}",
        download_url=url,
        thumbnail_url=_best_image_url(raw_item.get("images", {})),
        subtitles_url=_subtitles_url(file_entry),
        duration_seconds=duration,
        duration_ticks=int(duration * 10_000_000) if duration else 0,
        filesize=_int_or_zero(file_entry.get("filesize")),
        label=str(file_entry.get("label") or ""),
        frame_width=_int_or_zero(file_entry.get("frameWidth") or file_entry.get("frame_width")),
        frame_height=_int_or_zero(file_entry.get("frameHeight") or file_entry.get("frame_height")),
        track=str(raw_item.get("track") or ""),
        guid=guid,
        natural_key=str(raw_item.get("naturalKey") or ""),
        primary_category=str(raw_item.get("primaryCategory") or ""),
        first_published=str(raw_item.get("firstPublished") or ""),
    )


def _best_file_from_media_item(media_info: dict[str, Any] | None, fileformat: str) -> dict[str, Any] | None:
    if not isinstance(media_info, dict):
        return None
    files = media_info.get("files", [])
    if not isinstance(files, list):
        return None
    return _select_best_entry([f for f in files if isinstance(f, dict)], fileformat)


def _subtitles_url(entry: dict[str, Any] | None) -> str:
    if not isinstance(entry, dict):
        return ""
    subtitles = entry.get("subtitles")
    if isinstance(subtitles, dict):
        return str(subtitles.get("url") or "")
    return ""


def _best_image_url(images: Any, min_size: str = "md", square: bool = False) -> str:
    if not isinstance(images, dict):
        return ""
    preferred_order = ["wss", "lsr", "sqr", "pnr"]
    if square and "sqr" in preferred_order:
        preferred_order.remove("sqr")
        preferred_order.insert(0, "sqr")

    sizes = ["sm", "md", "lg", "xl"]
    start = sizes.index(min_size) if min_size in sizes else 0
    preferred_sizes = sizes[start:]

    for image_type in preferred_order:
        variants = images.get(image_type)
        if not isinstance(variants, dict):
            continue
        for size in preferred_sizes:
            value = variants.get(size)
            if value:
                return str(value)
        for value in variants.values():
            if value:
                return str(value)
    return ""


def _entry_url(entry: dict[str, Any]) -> str:
    return str(
        (entry.get("file") or {}).get("url")
        or entry.get("progressiveDownloadURL")
        or entry.get("url")
        or ""
    )


def _clean_title(title: str) -> str:
    title = (title or "").strip()
    if _QUALITY_TITLE_RE.match(title):
        return ""
    return title


def _first_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_or_zero(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _track_sort_key(track: str) -> tuple[int, str]:
    try:
        return (0, f"{int(track):08d}")
    except (TypeError, ValueError):
        return (1, str(track))


def _stable_item_id(*parts: Any) -> str:
    raw = "|".join(str(part) for part in parts if part not in (None, ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _item_for_emit(
    item: JWMediaItem,
    cache_thumbnails: bool,
    cache_paths: JWMediaCatalogCachePaths,
) -> JWMediaItem:
    if cache_thumbnails:
        return _with_cached_thumbnail(item, cache_paths)
    return _with_existing_thumbnail(item, cache_paths)


def _catalog_identity_index(items: Iterable[JWMediaItem]) -> set[str]:
    identities: set[str] = set()
    for item in items:
        identities.update(_catalog_identity_values(item))
    return identities


def _catalog_identity_values(item: JWMediaItem) -> set[str]:
    identities: set[str] = set()
    for prefix, value in (
        ("guid", item.guid),
        ("natural", item.natural_key),
        ("url", item.download_url),
        ("id", item.id),
    ):
        if value:
            identities.add(f"{prefix}:{value}")
    return identities


def _sort_catalog_items(items: Iterable[JWMediaItem]) -> list[JWMediaItem]:
    return sorted(
        items,
        key=lambda item: (item.first_published or "", item.title.lower()),
        reverse=True,
    )


def _catalog_snapshot_path(
    language: str,
    cache_paths: JWMediaCatalogCachePaths,
) -> str:
    return os.fspath(
        cache_paths.catalog_dir / f"all_videos_{language.upper()}.json"
    )


def _load_catalog_snapshot(
    language: str,
    cache_paths: JWMediaCatalogCachePaths,
) -> dict[str, Any] | None:
    path = _catalog_snapshot_path(language, cache_paths)
    payload = _load_json_cache(path)
    return payload["data"] if payload else None


def _save_catalog_snapshot(
    language: str,
    items: list[JWMediaItem],
    completed_categories: set[str],
    all_categories: list[str],
    fetched_at: float,
    cache_paths: JWMediaCatalogCachePaths,
) -> None:
    data = {
        "_fetched_at": fetched_at,
        "completed_categories": sorted(completed_categories),
        "all_categories": list(all_categories),
        "items": [item.to_dict() for item in items],
    }
    _save_json_cache(_catalog_snapshot_path(language, cache_paths), data, fetched_at)


def _with_cached_thumbnail(
    item: JWMediaItem,
    cache_paths: JWMediaCatalogCachePaths,
) -> JWMediaItem:
    path = (
        ensure_thumbnail_cached(item.thumbnail_url, cache_paths=cache_paths)
        if item.thumbnail_url
        else ""
    )
    if not path:
        return item
    data = item.to_dict()
    data["thumbnail_path"] = path
    return JWMediaItem(**data)


def _with_existing_thumbnail(
    item: JWMediaItem,
    cache_paths: JWMediaCatalogCachePaths,
) -> JWMediaItem:
    path = (
        cached_thumbnail_path(item.thumbnail_url, cache_paths=cache_paths)
        if item.thumbnail_url
        else ""
    )
    if not path:
        return item
    data = item.to_dict()
    data["thumbnail_path"] = path
    return JWMediaItem(**data)


def _fetch_json_cached(
    url: str,
    namespace: str,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], float, bool]:
    def raise_if_cancelled() -> None:
        if should_cancel and should_cancel():
            raise _FetchCancelled()

    raise_if_cancelled()
    cache_path = _json_cache_path(namespace, url, cache_paths)
    if not force:
        cached = _load_json_cache(cache_path)
        if cached is not None:
            return cached["data"], float(cached["_fetched_at"]), True

    try:
        raise_if_cancelled()
        data = get_json(
            url,
            timeout=_FETCH_TIMEOUT_S,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
            },
        )
        raise_if_cancelled()
        if not isinstance(data, dict):
            raise HttpDecodeError(f"GET {url} returned a non-object JSON payload")
    except HttpError as exc:
        raise_if_cancelled()
        cached = _load_json_cache(cache_path, ignore_ttl=True)
        if cached is not None:
            log.debug("[JWMediaCatalog] Using stale cache for %s after fetch error: %s", url, exc)
            return cached["data"], float(cached["_fetched_at"]), True
        raise

    raise_if_cancelled()
    fetched_at = time.time()
    _save_json_cache(cache_path, data, fetched_at)
    return data, fetched_at, False


def _load_json_cache(path: str, *, ignore_ttl: bool = False) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not ignore_ttl:
            age_days = (time.time() - float(payload.get("_fetched_at", 0))) / 86400
            if age_days >= CACHE_TTL_DAYS:
                return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        return {"_fetched_at": payload.get("_fetched_at", 0), "data": data}
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return None


def _save_json_cache(path: str, data: dict[str, Any], fetched_at: float) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump({"_fetched_at": fetched_at, "data": data}, f, ensure_ascii=False)
    os.replace(temp_path, path)


def _json_cache_path(
    namespace: str,
    url: str,
    cache_paths: JWMediaCatalogCachePaths,
) -> str:
    cache_dir = cache_paths.catalog_dir / namespace
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.fspath(cache_dir / f"{digest}.json")


def _thumbnail_cache_dir(cache_paths: JWMediaCatalogCachePaths) -> Path:
    return cache_paths.thumbnail_dir


def _guess_image_ext(url: str, content_type: str) -> str:
    if content_type in _MIME_EXT:
        return _MIME_EXT[content_type]
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return suffix
    return ".jpg"


def _request_id(query: JWMediaQuery) -> str:
    raw = json.dumps(asdict(query), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "JWMediaCatalogService",
    "JWMediaItem",
    "JWMediaQuery",
    "USE_LATEST_DELTA_CATALOG_REFRESH",
    "cached_thumbnail_path",
    "ensure_thumbnail_cached",
    "fetch_jw_video_catalog",
    "fetch_jw_videos",
]
