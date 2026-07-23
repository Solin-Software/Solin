from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.media.thumbnail_store import ThumbnailStore
from solin.core.media.thumbnail_identity import thumbnail_storage_id
from solin.core.playlists.storage import (
    PendingDeletionRepository,
    PlaylistRepository,
    PlaylistStoragePaths,
)
from solin.core.foundation.resource_keys import (
    ResourceClaim,
    file_resource_key,
    folder_resource_key,
)
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.meetings.thumbnails import meeting_thumb_cache_key, meeting_thumb_dir

if TYPE_CHECKING:
    from solin.core.meetings.tree_store import MeetingTreeStore


_REFERENCE_STATE_ERRORS = (OSError, UnicodeError, TypeError, ValueError)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class _FileGeneration:
    exists: bool
    size: int
    modified_ns: int
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class ProfileCleanupReferences:
    media_paths: frozenset[str]
    playlist_thumbnail_ids: frozenset[str]
    meeting_thumbnail_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class _InventoryEntry:
    path: Path
    generation: _FileGeneration
    contained_media_paths: frozenset[str] = frozenset()
    children: tuple[tuple[Path, _FileGeneration], ...] = ()


@dataclass(frozen=True, slots=True)
class ProfileMaintenanceInventory:
    files: tuple[_InventoryEntry, ...]
    directories: tuple[_InventoryEntry, ...]


class MaintenanceSnapshotChanged(RuntimeError):
    """Raised when persisted references changed during orphan cleanup."""


class ProfileMaintenanceCancelled(RuntimeError):
    """Raised when startup cancellation stops profile housekeeping."""


class ProfileMaintenanceService:
    """Fail-closed orphan cleanup from one immutable repository snapshot."""

    def __init__(
        self,
        *,
        storage_paths: PlaylistStoragePaths,
        playlist_repository: PlaylistRepository,
        meeting_tree_store: MeetingTreeStore,
        profile_paths: ProfilePaths,
        resource_lanes: ResourceLaneRegistry,
    ) -> None:
        self._storage_paths = storage_paths
        self._playlist_repository = playlist_repository
        self._meeting_tree_store = meeting_tree_store
        self._profile_paths = profile_paths
        self._resource_lanes = resource_lanes
        self._pending_repository = PendingDeletionRepository.from_paths(
            storage_paths,
            resource_lanes=resource_lanes,
        )
        self._inventory = capture_profile_maintenance_inventory(profile_paths)

    @property
    def resource_claim(self) -> ResourceClaim:
        return ResourceClaim(
            exclusive_key=file_resource_key(self._storage_paths.pending_deletions_file),
            shared_keys=(
                file_resource_key(self._playlist_repository.path),
                file_resource_key(self._meeting_tree_store.path),
            ),
        )

    def run(self, cancellation: CancellationFlag | None = None) -> None:
        paths = (
            self._playlist_repository.path,
            self._meeting_tree_store.path,
            self._storage_paths.pending_deletions_file,
        )
        generation = tuple(_file_generation(path) for path in paths)
        playlists = self._playlist_repository.load_strict()
        meeting_data = self._meeting_tree_store.load_all_strict()
        pending = self._pending_repository.load_strict()
        _raise_if_maintenance_cancelled(cancellation)
        self._ensure_generation(paths, generation)
        references = build_profile_cleanup_references(playlists, meeting_data)
        file_candidates, directory_candidates = self._candidates(references)

        for candidate in file_candidates:
            self._ensure_generation(paths, generation)
            self._resource_lanes.run(
                file_resource_key(candidate.path),
                lambda current=candidate: _run_maintenance_action(
                    cancellation,
                    lambda: _unlink_if_unchanged(current),
                ),
            )
        for candidate in directory_candidates:
            self._ensure_generation(paths, generation)
            self._resource_lanes.run(
                folder_resource_key(candidate.path),
                lambda current=candidate: _run_maintenance_action(
                    cancellation,
                    lambda: _rmtree_if_unchanged(current),
                ),
            )

        still_pending = []
        for raw_path in pending:
            candidate = Path(raw_path)
            normalized = _normalized_path(candidate)
            thumbnail_id = ThumbnailStore.storage_id_from_filename(candidate.name)
            if (
                normalized in references.media_paths
                or thumbnail_id in references.playlist_thumbnail_ids
            ):
                continue
            self._ensure_generation(paths, generation)
            try:
                self._resource_lanes.run(
                    file_resource_key(candidate),
                    lambda current=candidate: _run_maintenance_action(
                        cancellation,
                        lambda: current.unlink(missing_ok=True),
                    ),
                )
            except OSError:
                still_pending.append(raw_path)
        self._ensure_generation(paths, generation)
        _run_maintenance_action(
            cancellation,
            lambda: self._pending_repository.save_strict(still_pending),
        )

    def _candidates(
        self,
        references: ProfileCleanupReferences,
    ) -> tuple[tuple[_InventoryEntry, ...], tuple[_InventoryEntry, ...]]:
        profile_paths = self._profile_paths
        media_roots = {
            Path(profile_paths.images_dir),
            Path(profile_paths.embedded_dir),
        }
        thumbnail_root = Path(profile_paths.thumb_cache_dir)
        meeting_root = Path(
            meeting_thumb_dir(
                meeting_thumb_cache_dir=profile_paths.meeting_thumb_cache_dir
            )
        )
        files: list[_InventoryEntry] = []
        for entry in self._inventory.files:
            path = entry.path
            if path.parent in media_roots:
                if _normalized_path(path) not in references.media_paths:
                    files.append(entry)
                continue
            if path.parent == thumbnail_root:
                storage_id = ThumbnailStore.storage_id_from_filename(path.name)
                if (
                    storage_id is None
                    or storage_id not in references.playlist_thumbnail_ids
                ):
                    files.append(entry)
                continue
            if (
                path.parent == meeting_root
                and path.name not in references.meeting_thumbnail_names
            ):
                files.append(entry)

        directories = tuple(
            entry
            for entry in self._inventory.directories
            if entry.contained_media_paths
            and entry.contained_media_paths.isdisjoint(references.media_paths)
        )
        return tuple(files), directories

    @staticmethod
    def _ensure_generation(
        paths: tuple[Path, ...],
        expected: tuple[_FileGeneration, ...],
    ) -> None:
        if tuple(_file_generation(path) for path in paths) != expected:
            raise MaintenanceSnapshotChanged(
                "Playlist or meeting references changed during maintenance"
            )


def build_profile_cleanup_references(
    playlists: list[dict],
    meeting_data: dict[str, Any],
) -> ProfileCleanupReferences:
    playlist_items = _playlist_items_for_cleanup(playlists)
    media_paths = {
        _normalized_path(item["url"])
        for item in playlist_items
        if item["url"] and os.path.isabs(item["url"])
    }
    playlist_thumbnail_ids = {
        thumbnail_storage_id(item["id"], item["url"])
        for item in playlist_items
        if item["id"]
    }
    meeting_thumbnail_names: set[str] = set()
    for _tree_key, node in _strict_meeting_nodes(meeting_data):
        if node.get("type") != "media":
            continue
        ref = node.get("media_ref")
        if ref is not None and not isinstance(ref, dict):
            raise ValueError("Meeting media_ref must be an object")
        location = str((ref or {}).get("file_path") or "")
        if location:
            media_paths.add(_normalized_path(location))
        item_id = str(node.get("id") or "")
        if item_id:
            meeting_thumbnail_names.add(meeting_thumb_cache_key(item_id))
        for field in ("thumbnail_cache_key", "thumbnail_local_path"):
            value = str(node.get(field) or "")
            if value:
                meeting_thumbnail_names.add(_stored_file_name(value))
    return ProfileCleanupReferences(
        media_paths=frozenset(media_paths),
        playlist_thumbnail_ids=frozenset(playlist_thumbnail_ids),
        meeting_thumbnail_names=frozenset(meeting_thumbnail_names),
    )


def capture_profile_maintenance_inventory(
    profile_paths: ProfilePaths,
) -> ProfileMaintenanceInventory:
    """Capture deletion candidates before the profile can accept user work."""

    files = tuple(
        _InventoryEntry(path, _file_generation(path))
        for directory in (
            profile_paths.images_dir,
            profile_paths.embedded_dir,
            profile_paths.thumb_cache_dir,
            meeting_thumb_dir(
                meeting_thumb_cache_dir=profile_paths.meeting_thumb_cache_dir
            ),
        )
        for path in _files_in(directory)
    )
    directories = tuple(
        _InventoryEntry(
            directory,
            _file_generation(directory),
            frozenset(_normalized_path(page) for page in directory.glob("page_*.jpg")),
            tuple(
                (child, _file_generation(child))
                for child in sorted(_files_in(directory))
            ),
        )
        for root in (
            profile_paths.pdf_pages_dir,
            profile_paths.pptx_pages_dir,
            profile_paths.docx_pages_dir,
        )
        for directory in _directories_in(root)
    )
    return ProfileMaintenanceInventory(files=files, directories=directories)


def _unlink_if_unchanged(entry: _InventoryEntry) -> None:
    if _file_generation(entry.path) == entry.generation:
        entry.path.unlink(missing_ok=True)


def _rmtree_if_unchanged(entry: _InventoryEntry) -> None:
    current_children = tuple(
        (child, _file_generation(child))
        for child in sorted(_files_in(entry.path))
    )
    if (
        _file_generation(entry.path) == entry.generation
        and current_children == entry.children
    ):
        shutil.rmtree(entry.path, ignore_errors=True)


def _raise_if_maintenance_cancelled(
    cancellation: CancellationFlag | None,
) -> None:
    if cancellation is not None and cancellation.is_set():
        raise ProfileMaintenanceCancelled


def _run_maintenance_action(
    cancellation: CancellationFlag | None,
    action: Callable[[], None],
) -> None:
    if cancellation is None:
        action()
        return
    if not cancellation.run_if_active(action):
        raise ProfileMaintenanceCancelled


def _strict_meeting_nodes(meeting_data: dict[str, Any]):
    from solin.core.meetings.tree_types import iter_nodes_strict

    if not isinstance(meeting_data, dict):
        raise ValueError("Meeting tree storage root must be an object")
    trees = meeting_data.get("trees", {})
    if not isinstance(trees, dict):
        raise ValueError("Meeting tree storage 'trees' must be an object")
    for tree_key, record in trees.items():
        if not isinstance(record, dict) or "nodes" not in record:
            raise ValueError(f"Meeting tree '{tree_key}' must contain nodes")
        for node in iter_nodes_strict(
            record["nodes"],
            context=f"trees[{tree_key!r}].nodes",
        ):
            yield tree_key, node


def _file_generation(path: Path) -> _FileGeneration:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return _FileGeneration(False, 0, 0, 0, 0)
    return _FileGeneration(
        True,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_dev,
        stat.st_ino,
    )


def _normalized_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _stored_file_name(path: str) -> str:
    native = Path(path).name
    windows = PureWindowsPath(path).name
    return windows if len(windows) < len(native) else native


def _files_in(directory: str | os.PathLike[str]) -> tuple[Path, ...]:
    root = Path(directory)
    if not root.is_dir():
        return ()
    return tuple(path for path in root.iterdir() if path.is_file())


def _directories_in(directory: str | os.PathLike[str]) -> tuple[Path, ...]:
    root = Path(directory)
    if not root.is_dir():
        return ()
    return tuple(path for path in root.iterdir() if path.is_dir())


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
        thumbnail_storage_id(item["id"], str(item.get("url") or ""))
        for item in playlist_items
        if isinstance(item.get("id"), str) and item["id"]
    }
    referenced_urls.update(os.path.normcase(path) for path in meeting_references)
    still_pending = []
    for path in pending:
        normalized = os.path.normcase(os.path.normpath(path))
        thumbnail_storage_id_from_path = ThumbnailStore.storage_id_from_filename(
            os.path.basename(path)
        )
        if (
            normalized in referenced_urls
            or thumbnail_storage_id_from_path in referenced_ids
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
            referenced_ids.add(
                thumbnail_storage_id(str(item_id), str(item.get("url") or ""))
            )

    for fname in os.listdir(thumb_dir):
        fpath = os.path.join(thumb_dir, fname)
        if not os.path.isfile(fpath):
            continue
        storage_id = ThumbnailStore.storage_id_from_filename(fname)
        if storage_id is None or storage_id not in referenced_ids:
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
        self._pending_storage_ids: set[str] = set()

    @property
    def pending_count(self) -> int:
        return len(self._pending_storage_ids)

    def enqueue_items(self, items: list[dict]) -> None:
        for item in items:
            item_id = item.get("id", "")
            if isinstance(item_id, str) and item_id:
                self._pending_storage_ids.add(
                    thumbnail_storage_id(item_id, str(item.get("url") or ""))
                )

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
            thumbnail_storage_id(
                str(item_id),
                str(item.get("url") or ""),
            )
            for item in playlist_items
            if isinstance((item_id := item.get("id", "")), str) and item_id
        }

        pending_ids = self._pending_storage_ids
        self._pending_storage_ids = set()

        for item_id in pending_ids - referenced_ids:
            paths = (
                self._thumbnail_store.path(item_id),
                self._thumbnail_store.source_signature_path(item_id),
            )
            for thumb in paths:
                if not thumb.exists():
                    continue
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
