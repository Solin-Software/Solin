from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.media.thumbnail_store import ThumbnailStore
from solin.core.playlists.storage import (
    PendingDeletionRepository,
    PlaylistRepository,
    PlaylistStoragePaths,
)

if TYPE_CHECKING:
    from solin.core.meetings.tree_store import MeetingTreeStore


_REFERENCE_STATE_ERRORS = (OSError, UnicodeError, TypeError, ValueError)
_MISSING = object()


def _playlist_repository(paths: PlaylistStoragePaths) -> PlaylistRepository:
    return PlaylistRepository.from_paths(paths)


def _pending_deletion_repository(paths: PlaylistStoragePaths) -> PendingDeletionRepository:
    return PendingDeletionRepository.from_paths(paths)


def try_remove_file(
    path: str,
    storage_paths: PlaylistStoragePaths,
    retries: int = 3,
    delay: float = 0.5,
) -> bool:
    for attempt in range(retries):
        try:
            if os.path.isfile(path):
                os.remove(path)
            return True
        except OSError:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))

    pending_repo = _pending_deletion_repository(storage_paths)
    try:
        pending = pending_repo.load_strict()
    except _REFERENCE_STATE_ERRORS:
        log_ignored_exception(
            __name__,
            "Could not load pending deletions queue",
        )
        return False
    if path not in pending:
        pending.append(path)
        pending_repo.save(pending)
    return False


def flush_pending_deletions(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
) -> None:
    pending_repo = _pending_deletion_repository(storage_paths)
    try:
        pending = pending_repo.load_strict()
    except _REFERENCE_STATE_ERRORS:
        log_ignored_exception(
            __name__,
            "Could not validate pending deletions queue",
        )
        return
    if not pending:
        return

    try:
        playlists = _playlist_repository(storage_paths).load_strict()
        playlist_items = _playlist_items_for_cleanup(playlists)
        meeting_references = _meeting_tree_referenced_urls(meeting_tree_store)
    except _REFERENCE_STATE_ERRORS:
        log_ignored_exception(
            __name__,
            "Could not validate media references for pending deletions",
        )
        return
    referenced_urls = {
        os.path.normcase(os.path.normpath(item["url"]))
        for item in playlist_items
        if isinstance(item.get("url"), str)
        and os.path.isabs(item["url"])
    }
    referenced_ids = {
        item["id"]
        for item in playlist_items
        if isinstance(item.get("id"), str) and item["id"]
    }
    referenced_urls.update(os.path.normcase(path) for path in meeting_references)
    still_pending = []
    for path in pending:
        normalized = os.path.normcase(os.path.normpath(path))
        if (
            normalized in referenced_urls
            or os.path.splitext(os.path.basename(path))[0] in referenced_ids
        ):
            continue
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            still_pending.append(path)
    pending_repo.save(still_pending)


def flush_images_dir(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
    profile_paths: ProfilePaths,
) -> None:
    images_dir = os.fspath(profile_paths.images_dir)
    if not os.path.isdir(images_dir):
        return

    try:
        referenced = _referenced_media_urls(
            storage_paths,
            meeting_tree_store,
            normalize=True,
        )
    except _REFERENCE_STATE_ERRORS:
        log_ignored_exception(__name__, "Could not validate image references")
        return

    for fname in os.listdir(images_dir):
        fpath = os.path.join(images_dir, fname)
        if not os.path.isfile(fpath):
            continue
        if os.path.normpath(fpath) not in referenced:
            try:
                os.remove(fpath)
            except OSError:
                pass


def flush_thumbs_dir(
    storage_paths: PlaylistStoragePaths,
    thumb_cache_dir: str | os.PathLike[str],
) -> None:
    thumb_dir = os.fspath(thumb_cache_dir)
    if not os.path.isdir(thumb_dir):
        return

    try:
        playlists = _playlist_repository(storage_paths).load_strict()
        playlist_items = _playlist_items_for_cleanup(playlists)
    except _REFERENCE_STATE_ERRORS:
        log_ignored_exception(__name__, "Could not validate thumbnail references")
        return

    referenced_ids: set[str] = set()
    for item in playlist_items:
        item_id = item.get("id", "")
        if item_id:
            referenced_ids.add(item_id)

    for fname in os.listdir(thumb_dir):
        fpath = os.path.join(thumb_dir, fname)
        if not os.path.isfile(fpath):
            continue
        stem = os.path.splitext(fname)[0]
        if stem not in referenced_ids:
            try:
                os.remove(fpath)
            except OSError:
                pass


def flush_pdf_pages(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
    pdf_pages_dir: str | os.PathLike[str],
) -> None:
    try:
        referenced = _referenced_media_urls(storage_paths, meeting_tree_store)
    except _REFERENCE_STATE_ERRORS:
        log_ignored_exception(__name__, "Could not validate PDF page references")
        return

    from solin.core.rendering.pdf import flush_pdf_pages_dir

    flush_pdf_pages_dir(referenced, pdf_pages_dir)


def flush_pptx_pages(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
    pptx_pages_dir: str | os.PathLike[str],
    docx_pages_dir: str | os.PathLike[str],
) -> None:
    try:
        referenced = _referenced_media_urls(storage_paths, meeting_tree_store)
    except _REFERENCE_STATE_ERRORS:
        log_ignored_exception(__name__, "Could not validate Office page references")
        return

    from solin.core.rendering.libreoffice import flush_docx_pages_dir
    from solin.core.rendering.libreoffice import flush_pptx_pages_dir

    flush_pptx_pages_dir(referenced, pptx_pages_dir)
    flush_docx_pages_dir(referenced, docx_pages_dir)


def flush_embedded_dir(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
    profile_paths: ProfilePaths,
) -> None:
    embedded_dir = os.fspath(profile_paths.embedded_dir)
    if not os.path.isdir(embedded_dir):
        return

    try:
        referenced = _referenced_media_urls(
            storage_paths,
            meeting_tree_store,
            normalize=True,
        )
    except _REFERENCE_STATE_ERRORS:
        log_ignored_exception(__name__, "Could not validate embedded media references")
        return

    for fname in os.listdir(embedded_dir):
        fpath = os.path.join(embedded_dir, fname)
        if not os.path.isfile(fpath):
            continue
        if os.path.normpath(fpath) not in referenced:
            try:
                os.remove(fpath)
            except OSError:
                pass


class PlaylistCleanupQueue:
    """Defers and deduplicates cleanup until playlist writes have settled."""

    def __init__(
        self,
        storage_paths: PlaylistStoragePaths,
        thumbnail_store: ThumbnailStore,
    ) -> None:
        self._storage_paths = storage_paths
        self._thumbnail_store = thumbnail_store
        self._pending_ids: set[str] = set()

    @property
    def pending_count(self) -> int:
        return len(self._pending_ids)

    def enqueue_items(self, items: list[dict]) -> None:
        for item in items:
            item_id = item.get("id", "")
            if isinstance(item_id, str) and item_id:
                self._pending_ids.add(item_id)

    def flush(self) -> None:
        if not self.pending_count:
            return

        try:
            playlists = _playlist_repository(self._storage_paths).load_strict()
            playlist_items = _playlist_items_for_cleanup(playlists)
        except _REFERENCE_STATE_ERRORS:
            log_ignored_exception(
                __name__,
                "Could not validate playlist references for cleanup",
            )
            return
        referenced_ids = {
            str(item_id)
            for item in playlist_items
            if isinstance((item_id := item.get("id", "")), str) and item_id
        }

        pending_ids = self._pending_ids
        self._pending_ids = set()

        for item_id in pending_ids - referenced_ids:
            thumb = self._thumbnail_store.path(item_id)
            if thumb.exists():
                try_remove_file(
                    os.fspath(thumb),
                    self._storage_paths,
                    retries=1,
                    delay=0,
                )


def _referenced_media_urls(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
    *,
    normalize: bool = False,
) -> set[str]:
    referenced = _referenced_playlist_urls(storage_paths, normalize=normalize)
    referenced.update(_meeting_tree_referenced_urls(meeting_tree_store))
    return referenced


def _referenced_playlist_urls(
    storage_paths: PlaylistStoragePaths,
    *,
    normalize: bool = False,
) -> set[str]:
    referenced: set[str] = set()
    playlists = _playlist_repository(storage_paths).load_strict()
    for item in _playlist_items_for_cleanup(playlists):
        url = item.get("url", "")
        if isinstance(url, str) and url:
            referenced.add(os.path.normpath(url) if normalize else url)
    return referenced


def _playlist_items_for_cleanup(playlists: list[dict]) -> list[dict]:
    items: list[dict] = []
    for playlist_index, playlist in enumerate(playlists):
        if not isinstance(playlist, dict):
            raise ValueError(f"Playlist #{playlist_index} must be an object")
        playlist_items = playlist.get("items", _MISSING)
        if not isinstance(playlist_items, list):
            raise ValueError(f"Playlist #{playlist_index} must contain an items list")
        for item_index, item in enumerate(playlist_items):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Playlist #{playlist_index} item #{item_index} must be an object"
                )
            item_id = item.get("id", _MISSING)
            if not isinstance(item_id, str):
                raise ValueError(
                    f"Playlist #{playlist_index} item #{item_index} must contain a string id"
                )
            url = item.get("url", _MISSING)
            if not isinstance(url, str):
                raise ValueError(
                    f"Playlist #{playlist_index} item #{item_index} must contain a string url"
                )
            items.append(item)
    return items


def _meeting_tree_referenced_urls(
    meeting_tree_store: MeetingTreeStore,
) -> set[str]:
    referenced: set[str] = set()
    from solin.core.meetings.tree_types import iter_nodes_strict

    data = meeting_tree_store.load_all_strict()
    for tree_key, record in data.get("trees", {}).items():
        if not isinstance(record, dict):
            raise ValueError(f"Meeting tree '{tree_key}' must be an object")
        if "nodes" not in record:
            raise ValueError(f"Meeting tree '{tree_key}' must contain nodes")
        for node in iter_nodes_strict(
            record["nodes"],
            context=f"trees[{tree_key!r}].nodes",
        ):
            if node.get("type") != "media":
                continue
            ref = node.get("media_ref") or {}
            url = str(ref.get("file_path") or "")
            if url:
                referenced.add(os.path.normpath(url))
    return referenced
