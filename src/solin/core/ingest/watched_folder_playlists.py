"""Playlist manifest operations for watched-folder workflows."""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Callable
import logging
import threading
from typing import Any

from solin.core.ingest.local_files import local_file_availability_signature
from solin.core.ingest.watched_folder import (
    WatchedFolderSyncThread,
    get_pending_files,
    load_manifest_playlist,
    remove_item_from_manifest,
    save_manifest_playlist,
    scan_root,
)


class WatchedFolderPlaylistStore:
    """Facade for watched-folder playlist manifests and sync workers."""

    def __init__(self) -> None:
        self._listeners: set[Callable[[], None]] = set()
        self._listener_lock = threading.RLock()

    def scan_root(self, folder_path: str) -> list[dict[str, Any]]:
        return scan_root(folder_path)

    def load_playlist(self, folder_path: str) -> dict[str, Any]:
        return load_manifest_playlist(folder_path)

    def save_playlist(self, folder_path: str, playlist: dict[str, Any]) -> None:
        save_manifest_playlist(folder_path, playlist)
        self._publish_changed()

    def remove_item(self, folder_path: str, item: dict[str, Any]) -> bool:
        removed = remove_item_from_manifest(folder_path, item)
        if removed:
            self._publish_changed()
        return removed

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        with self._listener_lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._listener_lock:
                self._listeners.discard(listener)

        return unsubscribe

    def notify_external_change(self) -> None:
        """Publish a filesystem-watcher change after its debounce boundary."""
        self._publish_changed()

    def _publish_changed(self) -> None:
        with self._listener_lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001 - repository observer boundary
                logging.getLogger(__name__).warning(
                    "Watched-folder playlist listener failed",
                    exc_info=True,
                )

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
