"""Playlist manifest operations for watched-folder workflows."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import logging
import os
from pathlib import Path
import threading
from typing import Any, TypeVar

from solin.core.foundation.resource_keys import (
    ResourceClaim,
    child_folder_resource_claim,
    folder_read_resource_claim,
)
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.ingest.local_files import (
    LocalFileAvailabilitySignature,
    local_file_availability_signature,
)
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.playlists.linked_folder import playlist_sync, register_playlist_sync


_T = TypeVar("_T")
from solin.core.ingest.watched_folder import (
    WatchedFolderSyncThread,
    get_pending_files,
    load_manifest_playlist,
    remove_item_from_manifest,
    save_manifest_playlist,
    stage_manifest_playlist,
    scan_root,
)


class WatchedFolderPlaylistStore:
    """Facade for watched-folder playlist manifests and sync workers."""

    def __init__(
        self,
        resource_lanes: ResourceLaneRegistry | None = None,
    ) -> None:
        self._listeners: set[Callable[[], None]] = set()
        self._listener_lock = threading.RLock()
        self._resource_lanes = resource_lanes or ResourceLaneRegistry()
        self._resource_lanes_used = False

    def bind_resource_lanes(self, resource_lanes: ResourceLaneRegistry) -> None:
        """Bind the application-wide registry before the store starts serving work."""

        if not isinstance(resource_lanes, ResourceLaneRegistry):
            raise TypeError("resource_lanes must be a ResourceLaneRegistry")
        if self._resource_lanes_used and resource_lanes is not self._resource_lanes:
            raise RuntimeError("Cannot replace resource lanes after store use")
        self._resource_lanes = resource_lanes

    def scan_root(self, folder_path: str) -> list[dict[str, Any]]:
        return self._run_claimed(
            folder_read_resource_claim(folder_path),
            lambda: scan_root(folder_path),
        )

    def load_all_playlists(self, folder_path: str) -> list[dict[str, Any]]:
        """Load one consistent linked-playlist catalog under a root read claim."""

        def load_all() -> list[dict[str, Any]]:
            return [
                self.load_playlist(str(entry["path"]))
                for entry in scan_root(folder_path)
                if entry.get("path")
            ]

        return self._run_claimed(folder_read_resource_claim(folder_path), load_all)

    def load_playlist(self, folder_path: str) -> dict[str, Any]:
        return self._run_claimed(
            child_folder_resource_claim(folder_path),
            lambda: load_manifest_playlist(folder_path),
        )

    def save_playlist(self, folder_path: str, playlist: dict[str, Any]) -> None:
        self._run_claimed(
            child_folder_resource_claim(folder_path),
            lambda: save_manifest_playlist(folder_path, playlist),
        )
        self._publish_changed()

    def stage_playlist(self, folder_path: str, playlist: dict[str, Any]) -> None:
        """Persist accepted user intent locally, without reading cloud files."""
        stage_manifest_playlist(folder_path, playlist)

    def reset_sync(self, folder_path: str) -> None:
        def reset() -> None:
            service = playlist_sync(folder_path)
            with service.lock, service.intent_lock:
                service.reset_document()
        self._run_claimed(child_folder_resource_claim(folder_path), reset)
        self._publish_changed()

    def rename_folder(self, folder_path: str, new_name: str,
                      file_store: WatchedFolderFileStore) -> str:
        def rename() -> str:
            service = playlist_sync(folder_path)
            with service.lock, service.intent_lock:
                old_folder = service.folder
                destination = Path(file_store.rename_folder(old_folder, new_name))
                try:
                    service.rebind_folder(destination)
                except BaseException:  # noqa: BLE001 - Restore the physical folder after any failed binding transaction.
                    file_store.rename_folder(destination, old_folder.name)
                    raise
                register_playlist_sync(destination, service)
                return os.fspath(destination)
        result = self._run_claimed(child_folder_resource_claim(folder_path), rename)
        self._publish_changed()
        return result

    def delete_folder(self, folder_path: str, file_store: WatchedFolderFileStore) -> None:
        def delete() -> None:
            service = playlist_sync(folder_path)
            with service.lock, service.intent_lock:
                with service.replica.retiring_binding():
                    file_store.delete_folder(folder_path)
        self._run_claimed(child_folder_resource_claim(folder_path), delete)
        self._publish_changed()

    def remove_item(
        self,
        folder_path: str,
        item: dict[str, Any],
        *,
        remaining_items: Iterable[dict[str, Any]] = (),
    ) -> bool:
        remaining_snapshot = tuple(remaining_items)
        removed = self._run_claimed(
            child_folder_resource_claim(folder_path),
            lambda: remove_item_from_manifest(
                folder_path,
                item,
                remaining_items=remaining_snapshot,
            ),
        )
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
        return self._run_claimed(
            child_folder_resource_claim(folder_path),
            lambda: get_pending_files(folder_path),
        )

    def file_availability_signature(
        self,
        urls: Iterable[str],
    ) -> LocalFileAvailabilitySignature:
        return local_file_availability_signature(urls)

    def create_sync_thread(
        self,
        folder_path: str,
        *,
        media_lang: str,
        fallback_lang_code: str,
        parent: Any,
    ) -> WatchedFolderSyncThread:
        self._resource_lanes_used = True
        return WatchedFolderSyncThread(
            folder_path,
            media_lang=media_lang,
            fallback_lang_code=fallback_lang_code,
            resource_lanes=self._resource_lanes,
            resource_claim=child_folder_resource_claim(folder_path),
            parent=parent,
        )

    def _run_claimed(
        self,
        claim: ResourceClaim,
        action: Callable[[], _T],
    ) -> _T:
        self._resource_lanes_used = True
        return self._resource_lanes.run(claim, action)
