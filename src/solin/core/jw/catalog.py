"""JW.org media retrieval, normalization, and catalog caching.

This module provides the UI-independent data layer used to browse JW media. It
supports publication/document requests through GETPUBMEDIALINKS, category
requests through the Mediator API, and full video-catalog discovery across the
known category hierarchy.

Network responses, resumable catalog snapshots, and thumbnails use separate
caches rooted at :class:`JWMediaCatalogCachePaths`. Public callers receive
normalized :class:`JWMediaItem` objects while the original remote thumbnail URL
is preserved alongside any local cached path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from solin.core.foundation.constants import (
    CACHE_TTL_DAYS,
    VIDEO_PREFERRED_QUALITY,
    VIDEO_QUALITY_FALLBACK_DIR,
    VIDEO_QUALITY_ORDER,
)
from solin.core.network.http import HttpDecodeError, HttpError, get_json, stream_get

from .publication_links import mediator_media_item_ids

log = logging.getLogger(__name__)

_USER_AGENT = "Solin/1.0"
_FETCH_TIMEOUT_S = 15
_THUMB_TIMEOUT_S = 15
_MAX_THUMB_BYTES = 5 * 1024 * 1024
_CATALOG_REFRESH_INTERVAL_S = 12 * 60 * 60
_CATALOG_PROGRESS_INTERVAL_S = 0.25
_CATALOG_CHECKPOINT_INTERVAL_S = 1.0
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
    """Filesystem roots from which catalog and thumbnail cache paths are derived."""

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


class JWMediaCatalogFetchCancelled(Exception):
    """Raised when a cooperative catalog fetch is cancelled."""


_EXPECTED_FETCH_ERRORS = (
    HttpError,
    OSError,
    UnicodeError,
    ValueError,
)


def is_catalog_refresh_due(
    fetched_at: float,
    *,
    now: float | None = None,
) -> bool:
    """Return whether a completed catalog snapshot is due for revalidation.

    Missing, zero, or negative timestamps are treated as immediately stale.
    """
    checked_at = float(fetched_at or 0.0)
    if checked_at <= 0:
        return True
    current_time = time.time() if now is None else float(now)
    return max(0.0, current_time - checked_at) >= _CATALOG_REFRESH_INTERVAL_S


@dataclass(frozen=True)
class JWMediaQuery:
    """Describe a normalized JW media lookup.

    ``pub`` addresses a publication symbol such as ``sjjm`` or ``osg``;
    ``docid`` addresses document-based media. If ``track`` is omitted, all
    tracks returned by GETPUBMEDIALINKS are normalized. Setting ``category``
    selects category-based retrieval instead of the publication endpoint.
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
    """Normalized JW media metadata shared by catalog consumers."""

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
        # This is a hot path for catalogs with thousands of immutable,
        # primitive-only records. dataclasses.asdict() recursively deep-copies
        # every field; an explicit projection preserves the public schema
        # without that unnecessary cost.
        return {
            "id": self.id,
            "title": self.title,
            "language": self.language,
            "media_type": self.media_type,
            "source": self.source,
            "download_url": self.download_url,
            "thumbnail_url": self.thumbnail_url,
            "thumbnail_path": self.thumbnail_path,
            "subtitles_url": self.subtitles_url,
            "duration_seconds": self.duration_seconds,
            "duration_ticks": self.duration_ticks,
            "filesize": self.filesize,
            "label": self.label,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "pub": self.pub,
            "docid": self.docid,
            "issue": self.issue,
            "track": self.track,
            "guid": self.guid,
            "natural_key": self.natural_key,
            "primary_category": self.primary_category,
            "first_published": self.first_published,
        }


def fetch_jw_videos(
    query: JWMediaQuery,
    *,
    cache_paths: JWMediaCatalogCachePaths,
    force: bool = False,
    cache_thumbnails: bool = True,
) -> tuple[list[JWMediaItem], float, bool]:
    """Fetch and normalize media for a single JW query.

    Category queries use the Mediator API; publication and document queries use
    GETPUBMEDIALINKS. The return value is ``(items, fetched_at, from_cache)``,
    where ``from_cache`` reports whether the API payload came from the JSON
    cache. Thumbnail files use an independent cache and are reused whenever
    possible.
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
    """Build or refresh the complete JW video catalog for *language*.

    Category discovery follows a bounded hierarchy:
      1. Seed the catalog with LatestVideos.
      2. Enumerate first-level VideoOnDemand subcategories.
      3. Inspect those categories for one additional nested level.
      4. Fetch all discovered categories and merge their media entries.

    Results are deduplicated by stable media identities and ordered newest
    first. Complete snapshots are reused within the catalog freshness window;
    stale snapshots remain available while refresh work runs. Partial snapshots
    act as resumable checkpoints so interrupted fetches can continue from the
    completed category set instead of starting over.

    ``should_cancel`` is checked cooperatively throughout network and category
    work. Cancellation raises :class:`JWMediaCatalogFetchCancelled`.
    """

    lang = (language or "E").upper()

    def raise_if_cancelled() -> None:
        if should_cancel and should_cancel():
            raise JWMediaCatalogFetchCancelled()

    raise_if_cancelled()

    snapshot = None if force else _load_catalog_snapshot(lang, cache_paths)
    items_by_id: dict[str, JWMediaItem] = {}
    completed: set[str] = set()
    fetched_at = time.time()
    from_cache = False

    loaded_completed: set[str] = set()
    stored_categories: list[str] = []

    if snapshot:
        from_cache = True
        fetched_at = float(snapshot.get("_fetched_at", fetched_at) or fetched_at)
        for item_data in snapshot.get("items", []):
            if isinstance(item_data, dict):
                try:
                    item = JWMediaItem(**item_data)
                    items_by_id[item.guid or item.id] = item
                except TypeError:
                    continue
        loaded_completed = set(snapshot.get("completed_categories", []))
        stored_categories = [
            str(category)
            for category in snapshot.get("all_categories", [])
            if category
        ]

    stored_required = set(stored_categories)
    stored_complete = bool(stored_required) and stored_required.issubset(
        loaded_completed
    )
    snapshot_refresh_required = bool(
        snapshot
        and stored_complete
        and is_catalog_refresh_due(fetched_at)
    )
    sorted_cached_items = (
        _sort_catalog_items(items_by_id.values())
        if snapshot and items_by_id
        else []
    )

    # A complete snapshot is authoritative for its freshness window. Category
    # discovery is part of refresh, not a prerequisite for displaying a catalog
    # that was already built successfully.
    if snapshot and stored_complete and not snapshot_refresh_required:
        return sorted_cached_items, fetched_at, True

    # Stale-while-revalidate: make a valid previous snapshot available before
    # any category discovery or network request. The caller can render known
    # videos immediately while this worker refreshes in the background.
    if sorted_cached_items and progress_callback:
        stored_total = len(stored_categories)
        progress_callback(
            sorted_cached_items,
            0 if snapshot_refresh_required else min(
                len(loaded_completed),
                stored_total,
            ),
            stored_total,
        )

    # On a cold cache, LatestVideos is useful content and does not depend on
    # traversing the category tree. Publish it first so category discovery
    # cannot delay the first usable page.
    prefetched_categories: set[str] = set()
    if snapshot is None:
        try:
            latest_raw, latest_fetched_at, latest_from_cache = _fetch_category_media(
                lang,
                _LATEST_CATEGORY_KEY,
                cache_paths=cache_paths,
                force=force,
                should_cancel=should_cancel,
            )
            raise_if_cancelled()
            fetched_at = max(fetched_at, latest_fetched_at)
            from_cache = from_cache or latest_from_cache
            for item in _parse_category_media(
                latest_raw,
                JWMediaQuery(language=lang, category=_LATEST_CATEGORY_KEY),
            ):
                items_by_id[item.guid or item.id] = item
            prefetched_categories.add(_LATEST_CATEGORY_KEY)
            if progress_callback and items_by_id:
                progress_callback(
                    _sort_catalog_items(items_by_id.values()),
                    1,
                    0,
                )
        except JWMediaCatalogFetchCancelled:
            raise
        except _EXPECTED_FETCH_ERRORS as exc:
            log.debug(
                "[JWMediaCatalog] Could not prefetch %s: %s",
                _LATEST_CATEGORY_KEY,
                exc,
            )

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
        and is_catalog_refresh_due(fetched_at)
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
    completed.update(prefetched_categories)
    last_progress_at = time.monotonic()
    last_checkpoint_at = 0.0

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
            now = time.monotonic()
            checkpoint_due = (
                last_checkpoint_at <= 0
                or now - last_checkpoint_at >= _CATALOG_CHECKPOINT_INTERVAL_S
            )
            progress_due = bool(
                progress_callback
                and (
                    now - last_progress_at >= _CATALOG_PROGRESS_INTERVAL_S
                    or idx >= total
                )
            )
            if checkpoint_due or progress_due:
                sorted_items = _sort_catalog_items(items_by_id.values())
            if checkpoint_due:
                _save_catalog_snapshot(
                    lang,
                    sorted_items,
                    completed,
                    categories,
                    fetched_at,
                    cache_paths,
                )
                last_checkpoint_at = now
            if progress_due and progress_callback:
                progress_callback(sorted_items, idx, total)
                last_progress_at = now
        except JWMediaCatalogFetchCancelled:
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
            raise JWMediaCatalogFetchCancelled()

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
    except JWMediaCatalogFetchCancelled:
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
    """Return a local thumbnail path, downloading and caching it when needed.

    ``force`` bypasses an existing cached file. An empty URL or a recoverable
    download/cache failure returns an empty string.
    """

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
    """Return an existing cached thumbnail path without performing network I/O.

    Returns an empty string when the URL is empty or no cached file exists.
    """

    if not thumbnail_url:
        return ""
    cache_dir = os.fspath(_thumbnail_cache_dir(cache_paths))
    cache_key = hashlib.sha256(thumbnail_url.encode("utf-8")).hexdigest()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        path = os.path.join(cache_dir, f"{cache_key}{ext}")
        if os.path.isfile(path):
            return path
    return ""


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
            raise JWMediaCatalogFetchCancelled()

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
            except JWMediaCatalogFetchCancelled:
                raise
            except _EXPECTED_FETCH_ERRORS as exc:
                log.debug("[JWMediaCatalog] Could not inspect category %s: %s", first, exc)
    except JWMediaCatalogFetchCancelled:
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
    for item_id in mediator_media_item_ids(
        query.pub or "",
        track,
        query.issue,
        query.docid,
        _publication_type(query.fileformat),
    ):
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


def _publication_type(fileformat: str) -> str:
    fmt = (fileformat or "").upper()
    if "MP4" in fmt or "M4V" in fmt:
        return "VIDEO"
    if "MP3" in fmt:
        return "AUDIO"
    return "VIDEO"


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
    # Catalog freshness is governed by _CATALOG_REFRESH_INTERVAL_S. Keeping a
    # structurally valid older snapshot available enables stale-while-revalidate
    # and an offline catalog instead of discarding useful data at the generic
    # JSON-cache TTL boundary.
    payload = _load_json_cache(path, ignore_ttl=True)
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
            raise JWMediaCatalogFetchCancelled()

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


__all__ = [
    "JWMediaCatalogCachePaths",
    "JWMediaCatalogFetchCancelled",
    "JWMediaItem",
    "JWMediaQuery",
    "USE_LATEST_DELTA_CATALOG_REFRESH",
    "cached_thumbnail_path",
    "ensure_thumbnail_cached",
    "fetch_jw_video_catalog",
    "fetch_jw_videos",
    "is_catalog_refresh_due",
]
