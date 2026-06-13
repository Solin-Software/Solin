from __future__ import annotations

import os
import threading
import time

from ...core.foundation import paths as _paths
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.playlists.storage import (
    load_playlists,
    load_pending_deletions,
    save_pending_deletions,
)
from ...core.playlists.thumbnails import playlist_thumb_path


def _try_remove_file(path: str, retries: int = 3, delay: float = 0.5) -> bool:
    for attempt in range(retries):
        try:
            if os.path.isfile(path):
                os.remove(path)
            return True
        except OSError:
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))

    pending = load_pending_deletions()
    if path not in pending:
        pending.append(path)
        save_pending_deletions(pending)
    return False


def flush_pending_deletions() -> None:
    pending = load_pending_deletions()
    if not pending:
        return

    still_pending = []
    for path in pending:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            still_pending.append(path)
    save_pending_deletions(still_pending)


def flush_images_dir() -> None:
    if not os.path.isdir(_paths.IMAGES_DIR):
        return

    referenced = _referenced_playlist_urls(normalize=True)
    referenced.update(_meeting_tree_referenced_urls())

    for fname in os.listdir(_paths.IMAGES_DIR):
        fpath = os.path.join(_paths.IMAGES_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        if os.path.normpath(fpath) not in referenced:
            try:
                os.remove(fpath)
            except OSError:
                pass


def flush_thumbs_dir() -> None:
    if not os.path.isdir(_paths.THUMB_CACHE_DIR):
        return

    referenced_ids: set[str] = set()
    for playlist in load_playlists():
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


def flush_pdf_pages() -> None:
    from ...core.rendering.pdf import flush_pdf_pages_dir as _flush_pdf

    referenced = _referenced_playlist_urls()
    referenced.update(_meeting_tree_referenced_urls())
    _flush_pdf(referenced)


def flush_pptx_pages() -> None:
    from ...core.rendering.libreoffice import flush_docx_pages_dir as _flush_docx
    from ...core.rendering.libreoffice import flush_pptx_pages_dir as _flush_pptx

    referenced = _referenced_playlist_urls()
    referenced.update(_meeting_tree_referenced_urls())
    _flush_pptx(referenced)
    _flush_docx(referenced)


def flush_embedded_dir() -> None:
    if not os.path.isdir(_paths.EMBEDDED_DIR):
        return

    referenced = _referenced_playlist_urls(normalize=True)
    referenced.update(_meeting_tree_referenced_urls())

    for fname in os.listdir(_paths.EMBEDDED_DIR):
        fpath = os.path.join(_paths.EMBEDDED_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        if os.path.normpath(fpath) not in referenced:
            try:
                os.remove(fpath)
            except OSError:
                pass


def _try_remove_file_async(path: str) -> None:
    def _worker():
        _try_remove_file(path, retries=4, delay=0.6)

    threading.Thread(target=_worker, daemon=True).start()


def _cleanup_playlist_files(playlist: dict) -> None:
    embedded_dir = _paths.EMBEDDED_DIR
    for item in playlist.get("items", []):
        item_id = item.get("id", "")
        if item_id:
            thumb = playlist_thumb_path(item_id)
            if thumb.exists():
                _try_remove_file(os.fspath(thumb))

        url = item.get("url", "")
        if url and os.path.isabs(url):
            norm = os.path.normpath(url)
            norm_embedded = os.path.normpath(embedded_dir)
            if norm.startswith(norm_embedded + os.sep) and os.path.isfile(norm):
                _try_remove_file(norm)


def _cleanup_item_files(item: dict, all_playlists: list) -> None:
    embedded_dir = _paths.EMBEDDED_DIR
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
            _try_remove_file(os.fspath(thumb))

    if url and os.path.isabs(url) and url not in all_urls:
        norm = os.path.normpath(url)
        norm_embedded = os.path.normpath(embedded_dir)
        if norm.startswith(norm_embedded + os.sep) and os.path.isfile(norm):
            _try_remove_file_async(norm)


def _referenced_playlist_urls(*, normalize: bool = False) -> set[str]:
    referenced: set[str] = set()
    for playlist in load_playlists():
        for item in playlist.get("items", []):
            url = item.get("url", "")
            if url:
                referenced.add(os.path.normpath(url) if normalize else url)
    return referenced


def _meeting_tree_referenced_urls() -> set[str]:
    referenced: set[str] = set()
    try:
        from ...core.meetings.tree_store import MeetingTreeStore
        from ...core.meetings.tree_types import iter_nodes

        data = MeetingTreeStore().load_all()
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
