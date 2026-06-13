from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING

from solin.core.foundation import paths as _paths
from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.playlists.storage import (
    PlaylistStoragePaths,
    load_playlists,
    load_pending_deletions,
    save_pending_deletions,
)
from solin.core.playlists.thumbnails import playlist_thumb_path

if TYPE_CHECKING:
    from solin.core.meetings.tree_store import MeetingTreeStore


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

    pending = load_pending_deletions(storage_paths)
    if path not in pending:
        pending.append(path)
        save_pending_deletions(pending, storage_paths)
    return False


def flush_pending_deletions(storage_paths: PlaylistStoragePaths) -> None:
    pending = load_pending_deletions(storage_paths)
    if not pending:
        return

    still_pending = []
    for path in pending:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            still_pending.append(path)
    save_pending_deletions(still_pending, storage_paths)


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


def flush_thumbs_dir(storage_paths: PlaylistStoragePaths) -> None:
    if not os.path.isdir(_paths.THUMB_CACHE_DIR):
        return

    referenced_ids: set[str] = set()
    for playlist in load_playlists(storage_paths):
        for item in playlist.get("items", []):
            item_id = item.get("id", "")
            if item_id:
                referenced_ids.add(item_id)

    for fname in os.listdir(_paths.THUMB_CACHE_DIR):
        fpath = os.path.join(_paths.THUMB_CACHE_DIR, fname)
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
) -> None:
    from solin.core.rendering.pdf import flush_pdf_pages_dir as _flush_pdf

    referenced = _referenced_playlist_urls(storage_paths)
    referenced.update(_meeting_tree_referenced_urls(meeting_tree_store))
    _flush_pdf(referenced)


def flush_pptx_pages(
    storage_paths: PlaylistStoragePaths,
    meeting_tree_store: MeetingTreeStore,
) -> None:
    from solin.core.rendering.libreoffice import flush_docx_pages_dir as _flush_docx
    from solin.core.rendering.libreoffice import flush_pptx_pages_dir as _flush_pptx

    referenced = _referenced_playlist_urls(storage_paths)
    referenced.update(_meeting_tree_referenced_urls(meeting_tree_store))
    _flush_pptx(referenced)
    _flush_docx(referenced)


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


def try_remove_file_async(
    path: str,
    storage_paths: PlaylistStoragePaths,
) -> None:
    def _worker() -> None:
        try_remove_file(path, storage_paths, retries=4, delay=0.6)

    threading.Thread(target=_worker, daemon=True).start()


def cleanup_playlist_files(
    playlist: dict,
    storage_paths: PlaylistStoragePaths,
    profile_paths: ProfilePaths,
) -> None:
    embedded_dir = os.fspath(profile_paths.embedded_dir)
    for item in playlist.get("items", []):
        item_id = item.get("id", "")
        if item_id:
            thumb = playlist_thumb_path(item_id)
            if thumb.exists():
                try_remove_file(os.fspath(thumb), storage_paths)

        url = item.get("url", "")
        if url and os.path.isabs(url):
            norm = os.path.normpath(url)
            norm_embedded = os.path.normpath(embedded_dir)
            if norm.startswith(norm_embedded + os.sep) and os.path.isfile(norm):
                try_remove_file(norm, storage_paths)


def cleanup_item_files(
    item: dict,
    all_playlists: list,
    storage_paths: PlaylistStoragePaths,
    profile_paths: ProfilePaths,
) -> None:
    embedded_dir = os.fspath(profile_paths.embedded_dir)
    item_id = item.get("id", "")
    url = item.get("url", "")

    all_ids: set[str] = set()
    all_urls: set[str] = set()
    for playlist in all_playlists:
        for other in playlist.get("items", []):
            if other.get("id"):
                all_ids.add(other["id"])
            if other.get("url"):
                all_urls.add(other["url"])

    if item_id and item_id not in all_ids:
        thumb = playlist_thumb_path(item_id)
        if thumb.exists():
            try_remove_file(os.fspath(thumb), storage_paths)

    if url and os.path.isabs(url) and url not in all_urls:
        norm = os.path.normpath(url)
        norm_embedded = os.path.normpath(embedded_dir)
        if norm.startswith(norm_embedded + os.sep) and os.path.isfile(norm):
            try_remove_file_async(norm, storage_paths)


def _referenced_playlist_urls(
    storage_paths: PlaylistStoragePaths,
    *,
    normalize: bool = False,
) -> set[str]:
    referenced: set[str] = set()
    for playlist in load_playlists(storage_paths):
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
