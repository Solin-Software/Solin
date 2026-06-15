"""Playlist manifest operations for watched-folder workflows."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from solin.core.ingest.watched_folder import (
    WatchedFolderSyncThread,
    get_pending_files,
    load_manifest_playlist,
    local_file_availability_signature,
    remove_item_from_manifest,
    save_manifest_playlist,
    scan_root,
)


class WatchedFolderPlaylistStore:
    """Facade for watched-folder playlist manifests and sync workers."""

    def scan_root(self, folder_path: str) -> list[dict[str, Any]]:
        return scan_root(folder_path)

    def load_playlist(self, folder_path: str) -> dict[str, Any]:
        return load_manifest_playlist(folder_path)

    def save_playlist(self, folder_path: str, playlist: dict[str, Any]) -> None:
        save_manifest_playlist(folder_path, playlist)

    def remove_item(self, folder_path: str, item: dict[str, Any]) -> bool:
        return remove_item_from_manifest(folder_path, item)

    def pending_files(self, folder_path: str) -> list[str]:
        return get_pending_files(folder_path)

    def file_availability_signature(
        self,
        urls: Iterable[str],
    ) -> tuple[tuple[str, bool], ...]:
        return local_file_availability_signature(urls)

    def create_sync_thread(
        self,
        folder_path: str,
        *,
        media_lang: str,
        fallback_lang_code: str,
        parent: Any,
    ) -> WatchedFolderSyncThread:
        return WatchedFolderSyncThread(
            folder_path,
            media_lang=media_lang,
            fallback_lang_code=fallback_lang_code,
            parent=parent,
        )
