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
    pending = pending_repo.load()
    if path not in pending:
        pending.append(path)
        pending_repo.save(pending)
    return False


def flush_pending_deletions(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
) -> None:
    pending_repo = _pending_deletion_repository(storage_paths)
    pending = pending_repo.load()
    if not pending:
        return

    try:
        playlists = _playlist_repository(storage_paths).load_strict()
    except (OSError, UnicodeError, ValueError):
        log_ignored_exception(
            __name__,
            "Could not validate playlist references for pending deletions",
        )
        return
    referenced_urls = {
        os.path.normcase(os.path.normpath(item["url"]))
        for playlist in playlists
        for item in playlist.get("items", [])
        if isinstance(item.get("url"), str)
        and os.path.isabs(item["url"])
    }
    referenced_ids = {
        item["id"]
        for playlist in playlists
        for item in playlist.get("items", [])
        if isinstance(item.get("id"), str) and item["id"]
    }
    referenced_urls.update(
        os.path.normcase(path)
        for path in _meeting_tree_referenced_urls(meeting_tree_store)
    )
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

    referenced = _referenced_playlist_urls(storage_paths, normalize=True)
    referenced.update(_meeting_tree_referenced_urls(meeting_tree_store))

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

    referenced_ids: set[str] = set()
    for playlist in _playlist_repository(storage_paths).load():
        for item in playlist.get("items", []):
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
    from solin.core.rendering.pdf import flush_pdf_pages_dir as _flush_pdf

    referenced = _referenced_playlist_urls(storage_paths)
    referenced.update(_meeting_tree_referenced_urls(meeting_tree_store))
    _flush_pdf(referenced, pdf_pages_dir)


def flush_pptx_pages(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
    pptx_pages_dir: str | os.PathLike[str],
    docx_pages_dir: str | os.PathLike[str],
) -> None:
    from solin.core.rendering.libreoffice import flush_docx_pages_dir as _flush_docx
    from solin.core.rendering.libreoffice import flush_pptx_pages_dir as _flush_pptx

    referenced = _referenced_playlist_urls(storage_paths)
    referenced.update(_meeting_tree_referenced_urls(meeting_tree_store))
    _flush_pptx(referenced, pptx_pages_dir)
    _flush_docx(referenced, docx_pages_dir)


def flush_embedded_dir(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
    profile_paths: ProfilePaths,
) -> None:
    embedded_dir = os.fspath(profile_paths.embedded_dir)
    if not os.path.isdir(embedded_dir):
        return

    referenced = _referenced_playlist_urls(storage_paths, normalize=True)
    referenced.update(_meeting_tree_referenced_urls(meeting_tree_store))

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
        except (OSError, UnicodeError, ValueError):
            log_ignored_exception(
                __name__,
                "Could not validate playlist references for cleanup",
            )
            return
        referenced_ids: set[str] = set()
        for playlist in playlists:
            for item in playlist.get("items", []):
                item_id = item.get("id", "")
                if isinstance(item_id, str) and item_id:
                    referenced_ids.add(item_id)

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



def _referenced_playlist_urls(
    storage_paths: PlaylistStoragePaths,
    *,
    normalize: bool = False,
) -> set[str]:
    referenced: set[str] = set()
    for playlist in _playlist_repository(storage_paths).load():
        for item in playlist.get("items", []):
            url = item.get("url", "")
            if url:
                referenced.add(os.path.normpath(url) if normalize else url)
    return referenced


def _meeting_tree_referenced_urls(
    meeting_tree_store: MeetingTreeStore,
) -> set[str]:
    referenced: set[str] = set()
    try:
        from solin.core.meetings.tree_types import iter_nodes

        data = meeting_tree_store.load_all()
        for record in data.get("trees", {}).values():
            if not isinstance(record, dict):
                continue
            nodes = record.get("nodes", [])
            if not isinstance(nodes, list):
                continue
            for node in iter_nodes(nodes):
                if node.get("type") != "media":
                    continue
                ref = node.get("media_ref") or {}
                url = str(ref.get("file_path") or "")
                if url:
                    referenced.add(os.path.normpath(url))
    except Exception:  # noqa: BLE001 - meeting model adapter boundary
        log_ignored_exception(__name__, "Could not collect meeting thumbnail references")
    return referenced
