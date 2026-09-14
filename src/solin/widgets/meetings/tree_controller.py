"""
meeting_tree_controller.py - Solin
==================================
QML controller for meeting trees.

Meetings keep their own persistence and merge rules while publishing the same
immutable media-tree snapshots used by playlists.
"""

from __future__ import annotations

import copy
import logging
import os
import random
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import (
    QObject,
    Property,
    QCoreApplication,
    QT_TRANSLATE_NOOP,
    QTimer,
    QUrl,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox, QDialog

from ...core.media.cache import MediaCacheManager
from ...core.media.destinations import create_media_destination_request
from ...core.i18n.strings import tr_document_page_title, tr_item_count
from ...core.media.profile_store import ProfileMediaStore
from ...core.media.thumbnail_store import ThumbnailStore
from ...core.media.thumbnail_identity import (
    thumbnail_source_fingerprint,
    thumbnail_storage_id,
)
from ...core.ingest.watched_folder_files import WatchedFolderFileStore
from ...core.foundation.constants import (
    DOCX_EXTS,
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
    PPTX_EXTS,
)
from ...core.media.formats import MEDIA_EXTS
from ...core.media.identity import partition_media_items
from ...core.media.insertion import MediaInsertPayload, MediaInsertResult
from ...core.media.operations import (
    MediaOperationCancelled,
    MediaOperationPresentation,
    MediaOperationProgress,
    MediaOperationSpec,
    MediaOperationState,
)
from ...core.media.playback_request import MediaTrim
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.foundation.resource_keys import (
    child_folder_resource_claim,
    file_resource_key,
    folder_read_resource_claim,
    folder_resource_key,
)
from ...core.foundation.qt_threads import stop_owned_qthread
from ...core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from ...core.i18n.meeting_sections import (
    display_meeting_section_title,
)
from ...core.ingest.manifest import (
    ManifestWriteError,
    cache_dir,
    retry_manifest_write,
)
from ...core.meetings.models import MemorialData
from ...core.meetings.tree_migrations import migrate_publication_subsections
from ...core.meetings.linked_folder_sync import (
    MeetingSyncRecord,
    MeetingSyncError,
    MeetingSyncIdentity,
)
from ...core.meetings.catalog_placement import (
    MeetingCatalogPlaylistRef,
    build_meeting_catalog_playlist_ref,
)
from ...core.meetings.canonical_restore import (
    CanonicalTreeDiff,
    canonical_source_keys,
    canonical_tree_diff,
    restore_canonical_tree,
)
from ...core.meetings.media_nodes import (
    create_jw_media_node,
    create_manual_media_node,
    create_playlist_media_node,
    meeting_media_from_ref,
    media_ref_title,
    playlist_item_from_meeting_node,
    playlist_item_media_url,
    should_accept_resolved_media_title,
)
from ...core.meetings.meeting_folder_imports import (
    find_meeting_folder_import_record,
    is_meeting_folder_source_supported,
    make_meeting_folder_import_record,
    meeting_folder_matches_tree,
    same_local_source,
    target_section_code_for_pub_type,
    upsert_meeting_folder_import_record,
)
from ...core.meetings.tree_builder import MeetingTreeBuilder
from ...core.meetings.tree_editing import (
    can_drop_tree_node,
    children_for_tree_target,
    find_tree_node,
    media_descendants,
    move_tree_node,
    parse_tree_list_id,
    replace_tree_node,
)
from ...core.meetings.tree_merger import (
    MeetingTreeMerger,
    include_manual_meeting_nodes,
    media_identity_signature,
    merge_persisted_meeting_trees,
)
from ...core.meetings.tree_store import (
    MeetingTreeOverview,
    MeetingTreeSnapshot,
    MeetingTreeStore,
)
from ...core.meetings.thumbnails import meeting_thumb_storage_id
from ...core.meetings.tree_types import Node, clone_nodes, count_media, iter_nodes, new_node_id
from ...core.projection.aspect_ratio import (
    DEFAULT_PROJECTION_ASPECT_RATIO,
    ProjectionAspectRatio,
)
from ...core.projection.idle_media import (
    create_idle_media_request,
    existing_idle_media_path,
)
from ...core.projection.image_framing import (
    ImageTransform,
    image_transform_from_record,
    image_transform_to_record,
    prepare_image_transform_for_aspect,
)
from ...core.playlists.jwl_import import playlist_items_from_jwl_document_items
from ...core.playlists.jwl_files import read_jwlplaylist_document
from ...core.meetings.colors import generate_section_hue
from ..playlist.dialogs import HuePickerDialog, NameDialog
from ...ui.media_info import MediaInfoQueue
from ...ui.qml.media_tree.meeting_session import MeetingTreeSession
from ...ui.qml.media_tree.state import MediaAvailability

if TYPE_CHECKING:
    from ...core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from ...core.meetings.linked_folder_sync import MeetingLinkedFolderSync
    from ...core.rendering.document_conversion import DocumentConversionService

_BIG_INDEX = 2**31 - 1
_DERIVED_MEDIA_SAVE_DEBOUNCE_MS = 200
_SYNC_SAVE_DEBOUNCE_MS = 180
_SYNC_SAVE_RETRY_DELAYS_MS = (100, 250, 500, 1_000, 2_000, 5_000, 10_000, 15_000)
_SYNC_SAVE_RETRY_BUDGET_SECONDS = 60.0

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingSyncSave:
    folder: Path
    identity: MeetingSyncIdentity
    nodes: list[Node]
    deleted_source_keys: set[str]
    linked_folder_files: dict[str, str]
    meeting_folder_imports: dict[str, dict[str, Any]]
    canonical_reset_generation: int
    hidden_canonical_media: dict[str, Node]
    expected_revision: int
    next_attempt_at: float
    generation: int = 1
    first_attempt_at: float | None = None
    retry_index: int = 0


@dataclass(frozen=True, slots=True)
class _DisabledSyncState:
    nodes: list[Node]
    hidden_canonical_media: dict[str, Node]
    linked_folder_files: dict[str, str]
    folder: Path


@dataclass(frozen=True, slots=True)
class _CanonicalRestoreState:
    nodes: list[Node]
    linked_folder_files: dict[str, str]
    generation: int


@dataclass(frozen=True, slots=True)
class _MeetingSyncDiscovery:
    available: bool
    folder: Path | None = None
    record: MeetingSyncRecord | None = None


@dataclass(frozen=True, slots=True)
class _PreparedMeetingNodes:
    nodes: list[Node]
    linked_files: dict[str, str]
    created_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _JwlMeetingImport:
    nodes: list[Node]
    created_paths: tuple[Path, ...]


def _reset_media_requests(controller: Any) -> None:
    info_queue = getattr(controller, "_info_queue", None)
    clear_info = getattr(info_queue, "clear", None)
    if callable(clear_info):
        clear_info()
    requests = getattr(controller, "_info_request_by_token", None)
    if requests is not None:
        requests.clear()
    active = getattr(controller, "_active_info_requests", None)
    if active is not None:
        active.clear()


def _emit_controller_state_changed(controller: Any) -> None:
    controller.stateChanged.emit()


def _tr(context: str, source: str) -> str:
    return QCoreApplication.translate(context, source)


_PLAYLIST_EDIT_CONTEXT = "PlaylistEditView"
_CANONICAL_RESTORE_CONTEXT = "MeetingCanonicalRestore"
_RESTORE_CONFIRM_SOURCE = QT_TRANSLATE_NOOP(
    "MeetingCanonicalRestore",
    "Restore the official meeting content?\n\nManually added content, trims, framing and expanded state will be kept.",
)
_RESTORE_TITLE_SOURCE = QT_TRANSLATE_NOOP(
    "MeetingCanonicalRestore",
    "Restore official meeting content",
)
_CANNOT_DELETE_OFFICIAL_SECTION_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistEditView",
    "Cannot delete section",
)
_MOVE_MANUAL_STRUCTURE_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistEditView",
    "Move or delete the manually added subsections and markers first.",
)


class MeetingTreeController(QObject):
    backRequested = Signal()
    projectRequested = Signal(object)
    addToDestinationRequested = Signal(object)
    setAsIdleRequested = Signal(object)
    pointerEntered = Signal()
    pointerCursorEntered = Signal(str, int)
    pointerCursorChanged = Signal(str, int)
    pointerCursorExited = Signal(str)
    pointerExited = Signal()

    stateChanged = Signal()
    chromeChanged = Signal()
    markerEditRequested = Signal(str)
    syncStateChanged = Signal()
    canonicalStateChanged = Signal()
    canonicalMediaRecoveryRequested = Signal(str, str)  # tree_key, missing local source
    storageSaved = Signal(str)  # tree_key
    storageSaveFailed = Signal(str, str)  # tree_key, error message
    _syncSaveCompleted = Signal(str, int, object, object)

    def __init__(
        self,
        service,
        *,
        meeting_type: str,
        language_code: str,
        store: MeetingTreeStore,
        profile_media_store: ProfileMediaStore,
        meeting_thumbnail_store: ThumbnailStore,
        watched_folder_file_store: WatchedFolderFileStore,
        jwpub_import_thread_factory: JwpubImportThreadFactory,
        document_conversion_service: DocumentConversionService,
        profile_paths: ProfilePaths,
        runtime_paths: RuntimePaths,
        cache_manager: MediaCacheManager,
        media_tree_runtime,
        linked_folder_sync: MeetingLinkedFolderSync,
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        projection_aspect_ratio_provider: Callable[[], object] | None = None,
        fallback_language_code: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._svc = service
        self._meeting_type = meeting_type
        self._language_code = language_code or "E"
        self._fallback_language_code = fallback_language_code or self._language_code
        self._store = store
        self._profile_media_store = profile_media_store
        self._meeting_thumbnail_store = meeting_thumbnail_store
        self._watched_folder_file_store = watched_folder_file_store
        self._jwpub_import_thread_factory = jwpub_import_thread_factory
        self._document_conversion_service = document_conversion_service
        self._profile_paths = profile_paths
        self._runtime_paths = runtime_paths
        self._media_cache_manager = cache_manager
        self._media_tree_runtime = media_tree_runtime
        self._projection_aspect_ratio_provider = projection_aspect_ratio_provider or (
            lambda: DEFAULT_PROJECTION_ASPECT_RATIO
        )
        self._builder = MeetingTreeBuilder(
            media_fallback_title=lambda: _tr("_MediaRow", "Media"),
        )
        self._sync_service = linked_folder_sync
        self._nodes: list[Node] = []
        self._tree_key = ""
        self._canonical_hash = ""
        self._canonical_nodes: list[Node] = []
        self._canonical_reset_generation = 0
        self._hidden_canonical_media: dict[str, Node] = {}
        self._canonical_restore_available = False
        self._canonical_media_recovery_requests: set[str] = set()
        self._sync_identity: MeetingSyncIdentity | None = None
        self._sync_root = ""
        self._sync_folder = ""
        self._sync_available = False
        self._sync_enabled = False
        self._sync_busy = False
        self._sync_busy_message = ""
        self._modal_operation_id = ""
        self._sync_revision = 0
        self._sync_refresh_generation = 0
        self._sync_refresh_operation_id = ""
        self._local_snapshot_generation = 0
        self._local_snapshot_operation_id = ""
        self._pending_sync_saves: dict[str, _PendingSyncSave] = {}
        self._sync_save_inflight: tuple[str, int] | None = None
        self._sync_save_future: Future[MeetingSyncRecord] | None = None
        self._sync_save_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="meeting-manifest",
        )
        self._syncSaveCompleted.connect(self._on_sync_save_completed)
        self._sync_save_timer = QTimer(self)
        self._sync_save_timer.setSingleShot(True)
        self._sync_save_timer.timeout.connect(self._drain_sync_manifest_saves)
        self._deleted_source_keys: set[str] = set()
        self._playlist_name = ""
        self._thumb_cache: dict[str, QPixmap] = {}
        self._thumb_versions: dict[str, int] = {}
        self._info_queue = media_info_queue_factory(self)
        self._info_queue.info_ready.connect(self._on_info_ready)
        self._info_queue.duration_ready.connect(self._on_duration_ready)
        self._info_queue.request_failed.connect(self._on_info_failed)
        self._info_request_by_token: dict[int, tuple[str, str, str, str]] = {}
        self._active_info_requests: set[tuple[str, str, str, str]] = set()
        self._next_token = 1
        self._resolve_to_node_id: dict[str, str] = {}
        self._resolve_to_identity: dict[str, tuple] = {}
        self._resolved_urls: dict[str, str] = {}
        self._cloud_progress_by_url: dict[str, float] = {}
        self._derived_media_patches: dict[
            str,
            tuple[tuple, dict[str, Any]],
        ] = {}
        self._derived_media_save_timer = QTimer(self)
        self._derived_media_save_timer.setSingleShot(True)
        self._derived_media_save_timer.setInterval(_DERIVED_MEDIA_SAVE_DEBOUNCE_MS)
        self._derived_media_save_timer.timeout.connect(self._flush_derived_media_patches)
        self._image_framing_save_pending = False
        self._image_framing_save_timer = QTimer(self)
        self._image_framing_save_timer.setSingleShot(True)
        self._image_framing_save_timer.setInterval(200)
        self._image_framing_save_timer.timeout.connect(self._flush_image_framing_save)
        self._pdf_threads: list[Any] = []
        self._jwpub_threads: list[Any] = []
        self._lo_threads: list[Any] = []
        self._linked_folder_files: dict[str, str] = {}  # file_path → node_id
        self._meeting_folder_imports: dict[str, dict[str, Any]] = {}
        self._meeting_folder_pending_sources: set[str] = set()
        self._meeting_folder_scan_generation = 0
        self._meeting_folder_scan_operation_id = ""
        self._overview: MeetingTreeOverview | None = None
        self._connect_services()
        self._tree_session = MeetingTreeSession(
            media_tree_runtime,
            self._thumbnail_local_path,
            badge_provider=lambda media_type: self._badge_for({}, media_type),
            thumbnail_ready=self._accept_probed_thumbnail,
            parent=self,
        )
        self.stateChanged.connect(self._schedule_tree_snapshot)
        media_tree_runtime.registry.stateChanged.connect(self._on_presentation_state_changed)
        media_tree_runtime.snapshots.writeCompleted.connect(self._on_snapshot_write_completed)
        media_tree_runtime.snapshots.writeFailed.connect(self._on_snapshot_write_failed)

    @Property(QObject, constant=True)
    def treeSource(self):  # noqa: N802 - QML API
        return self._tree_session.source

    def _schedule_tree_snapshot(self, *_args: object) -> None:
        if not self._tree_key:
            return
        expected_owner = f"meeting:{self._tree_key}"
        if self._tree_session.owner_id != expected_owner:
            self._tree_session.activate(
                self._tree_key,
                self._nodes,
                resolved_urls=self._resolved_urls,
            )
        else:
            self._tree_session.refresh(
                self._nodes,
                resolved_urls=self._resolved_urls,
            )

    @Slot(str)
    def cancelOperation(self, operation_id: str) -> None:  # noqa: N802 - QML API
        self._media_tree_runtime.operations.cancel(operation_id)

    @Slot(str)
    def retryOperation(self, operation_id: str) -> None:  # noqa: N802 - QML API
        self._media_tree_runtime.operations.retry(operation_id)

    def refresh_theme(self) -> None:
        _emit_controller_state_changed(self)
        self._emit_section_counts()

    def refresh_language(self) -> None:
        _emit_controller_state_changed(self)

    def _reset_media_requests(self) -> None:
        _reset_media_requests(self)

    @property
    def thumb_cache(self) -> dict[str, QPixmap]:
        return self._thumb_cache

    @Property(str, notify=stateChanged)
    def playlistName(self):
        return self._playlist_name

    @Property(bool, notify=chromeChanged)
    def isTemp(self):
        return False

    @Property(bool, notify=chromeChanged)
    def isWatched(self):
        return False

    @Property(bool, notify=syncStateChanged)
    def syncAvailable(self):
        return self._sync_available

    @Property(bool, notify=syncStateChanged)
    def syncEnabled(self):
        return self._sync_enabled

    @Property(bool, notify=syncStateChanged)
    def syncBusy(self):
        return self._sync_busy

    @Property(str, notify=syncStateChanged)
    def syncBusyMessage(self):
        return self._sync_busy_message

    @Property(bool, notify=syncStateChanged)
    def syncPending(self):
        if not self._sync_folder:
            return False
        key = os.path.normcase(os.path.abspath(self._sync_folder))
        return key in self._pending_sync_saves

    @Property(str, notify=syncStateChanged)
    def syncFolderPath(self):
        return self._sync_folder

    @Property(bool, notify=canonicalStateChanged)
    def canonicalRestoreAvailable(self):
        return self._canonical_restore_available

    def _canonical_diff(self) -> CanonicalTreeDiff:
        return canonical_tree_diff(
            getattr(self, "_canonical_nodes", []),
            getattr(self, "_nodes", []),
            getattr(self, "_deleted_source_keys", set()),
        )

    def _prune_deleted_source_keys(self) -> None:
        canonical_nodes = getattr(self, "_canonical_nodes", [])
        if canonical_nodes:
            valid_keys = canonical_source_keys(canonical_nodes)
            self._deleted_source_keys.intersection_update(valid_keys)
            hidden_media = getattr(self, "_hidden_canonical_media", {})
            self._hidden_canonical_media = {
                key: node for key, node in hidden_media.items() if key in valid_keys
            }

    def _refresh_canonical_restore_state(self) -> None:
        canonical_nodes = getattr(self, "_canonical_nodes", [])
        available = bool(
            canonical_nodes and MeetingTreeController._canonical_diff(self).has_changes
        )
        if available == getattr(self, "_canonical_restore_available", False):
            return
        self._canonical_restore_available = available
        signal = getattr(self, "canonicalStateChanged", None)
        if signal is not None:
            signal.emit()

    @Property(bool, notify=chromeChanged)
    def hasItems(self):
        return bool(self._nodes)

    @Property(str, notify=chromeChanged)
    def itemCountText(self):
        count = count_media(self._nodes)
        return tr_item_count(count)

    def placement_playlist_ref(self) -> MeetingCatalogPlaylistRef:
        return build_meeting_catalog_playlist_ref(
            self._nodes,
            section_title=display_meeting_section_title,
        )

    def add_external_media_items(
        self,
        items: list[dict[str, Any]],
        *,
        list_id: str = "root",
        insert_index: int = _BIG_INDEX,
    ) -> MediaInsertResult:
        """Insert prepared playlist-shaped media through the meeting save path."""

        if self._children_for_target(*self._parse_list_id(list_id)) is None:
            return MediaInsertResult(target_valid=False)
        candidates = [
            item for item in items if isinstance(item, dict) and playlist_item_media_url(item)
        ]
        partition = partition_media_items(self._media_identity_records(), candidates)
        nodes = [
            self._node_from_playlist_item(
                item,
                str(item.get("title") or _tr("_MediaRow", "Media")),
            )
            for item in partition.unique_items
        ]
        if not nodes:
            return MediaInsertResult(duplicate_items=partition.duplicate_items)
        if not self._insert_nodes(list_id, insert_index, nodes):
            return MediaInsertResult(
                duplicate_items=partition.duplicate_items,
                target_valid=False,
            )
        return MediaInsertResult(
            added_items=partition.unique_items,
            duplicate_items=partition.duplicate_items,
        )

    @Slot(str)
    def set_sync_root(self, watched_folder_path: str) -> None:
        self._flush_image_framing_save()
        self._sync_root = watched_folder_path or ""
        self._refresh_sync_availability()
        if self._tree_key:
            self._request_sync_refresh()

    def load_saved_tree(
        self,
        snapshot: MeetingTreeSnapshot,
        *,
        start_media_requests: bool = True,
    ) -> MeetingTreeSnapshot | None:
        """Load an already persisted meeting tree without building empty canonical data."""
        if not MeetingTreeController._flush_image_framing_save(self):
            return None
        if not MeetingTreeController._flush_derived_media_patches(self):
            return None
        cached_lookup = getattr(getattr(self, "_store", None), "cached_snapshot", None)
        latest = cached_lookup(snapshot.tree_key) if callable(cached_lookup) else None
        if latest is not None and latest.revision > snapshot.revision:
            snapshot = latest
        _reset_media_requests(self)
        self._meeting_type = snapshot.pub_type
        self._tree_key = snapshot.tree_key
        self._canonical_hash = snapshot.canonical_hash
        self._canonical_nodes = clone_nodes(snapshot.canonical_nodes)
        self._canonical_reset_generation = snapshot.canonical_reset_generation
        self._hidden_canonical_media = copy.deepcopy(snapshot.hidden_canonical_media)
        self._playlist_name = snapshot.overview.title or (
            _tr("_PubCard", "Life & Ministry")
            if snapshot.pub_type == "mwb"
            else _tr("_PubCard", "Watchtower Study")
        )
        self._overview = snapshot.overview
        self._sync_identity = MeetingSyncIdentity(
            tree_key=snapshot.tree_key,
            pub_type=snapshot.pub_type,
            monday=snapshot.monday,
            canonical_hash=snapshot.canonical_hash,
        )
        self._refresh_sync_availability()
        self._deleted_source_keys = set(snapshot.deleted_source_keys)
        self._linked_folder_files = dict(snapshot.linked_folder_files)
        self._meeting_folder_imports = copy.deepcopy(snapshot.meeting_folder_imports)
        pending_lookup = getattr(self, "_pending_sync_save_for_identity", None)
        pending_sync = pending_lookup(self._sync_identity) if callable(pending_lookup) else None
        if pending_sync is not None:
            self._nodes = MeetingTreeController._merge_snapshot_with_sync(
                self,
                snapshot,
                pending_sync.nodes,
                pending_sync.deleted_source_keys,
                getattr(pending_sync, "canonical_reset_generation", 0),
                getattr(pending_sync, "hidden_canonical_media", {}),
            )
            self._linked_folder_files = {
                **snapshot.linked_folder_files,
                **pending_sync.linked_folder_files,
            }
            self._meeting_folder_imports = {
                **copy.deepcopy(snapshot.meeting_folder_imports),
                **copy.deepcopy(pending_sync.meeting_folder_imports),
            }
            self._sync_folder = os.fspath(pending_sync.folder)
            self._sync_enabled = True
            self._sync_revision = pending_sync.expected_revision
            self._save()
        else:
            self._sync_enabled = False
            self._sync_revision = 0
            self._sync_folder = ""
            self._nodes = clone_nodes(snapshot.nodes)
        self._meeting_folder_pending_sources.clear()
        if start_media_requests:
            MeetingTreeController.start_media_enrichment(self)
        self.chromeChanged.emit()
        self.syncStateChanged.emit()
        MeetingTreeController._refresh_canonical_restore_state(self)
        _emit_controller_state_changed(self)
        if pending_sync is None:
            self._request_sync_refresh()
        return snapshot

    def start_media_enrichment(self) -> None:
        """Start local-only presentation metadata work after the detail is visible."""
        self._start_media_requests()

    def _merge_snapshot_with_sync(
        self,
        snapshot: MeetingTreeSnapshot,
        sync_nodes: list[Node],
        sync_deleted_source_keys: set[str],
        sync_generation: int,
        sync_hidden_canonical_media: dict[str, Node],
    ) -> list[Node]:
        sync_nodes, sync_deleted_source_keys = MeetingTreeController._migrate_sync_publications(
            self, sync_nodes, sync_deleted_source_keys,
        )
        local_generation = snapshot.canonical_reset_generation
        if sync_generation > local_generation:
            self._deleted_source_keys = set(sync_deleted_source_keys)
            self._canonical_reset_generation = sync_generation
            self._hidden_canonical_media = copy.deepcopy(sync_hidden_canonical_media)
            nodes = include_manual_meeting_nodes(sync_nodes, snapshot.nodes)
            MeetingTreeController._prune_deleted_source_keys(self)
            return nodes
        if local_generation > sync_generation:
            self._deleted_source_keys = set(snapshot.deleted_source_keys)
            self._canonical_reset_generation = local_generation
            self._hidden_canonical_media = copy.deepcopy(snapshot.hidden_canonical_media)
            nodes = include_manual_meeting_nodes(snapshot.nodes, sync_nodes)
            MeetingTreeController._prune_deleted_source_keys(self)
            return nodes
        self._deleted_source_keys = set(snapshot.deleted_source_keys) | set(
            sync_deleted_source_keys
        )
        self._canonical_reset_generation = local_generation
        self._hidden_canonical_media = {
            **copy.deepcopy(snapshot.hidden_canonical_media),
            **copy.deepcopy(sync_hidden_canonical_media),
        }
        nodes = merge_persisted_meeting_trees(
            snapshot.nodes,
            sync_nodes,
            self._deleted_source_keys,
        )
        MeetingTreeController._prune_deleted_source_keys(self)
        return nodes

    def load_memorial(self, md: MemorialData) -> None:
        canonical = self._builder.build_memorial(md)
        year = getattr(md, "year", 0) or (
            md.memorial_date.year if getattr(md, "memorial_date", None) else ""
        )
        date_key = md.memorial_date.isoformat() if md.memorial_date else str(year)
        self._playlist_name = _tr("_MemorialCard", "MEMORIAL")
        self._overview = None
        self._tree_key = f"memorial:{date_key}:{self._language_code}:{year}"
        self._sync_identity = None
        self._sync_enabled = False
        self._sync_folder = ""
        self._sync_revision = 0
        self._refresh_sync_availability()
        self._load_canonical(canonical)

    def _load_canonical(self, canonical: list[Node]) -> None:
        _reset_media_requests(self)
        self._canonical_nodes = clone_nodes(canonical)
        self._canonical_hash = self._builder.canonical_hash(canonical)
        if self._sync_identity is not None:
            self._sync_identity = MeetingSyncIdentity(
                tree_key=self._sync_identity.tree_key,
                pub_type=self._sync_identity.pub_type,
                monday=self._sync_identity.monday,
                canonical_hash=self._canonical_hash,
            )
        stored_snapshot = self._store.cached_snapshot(self._tree_key)
        if not self._store.loaded:
            self._apply_canonical_snapshot(canonical, None, persist=False)
            self._request_local_snapshot(canonical)
            return
        self._apply_canonical_snapshot(canonical, stored_snapshot)

    def _apply_canonical_snapshot(
        self,
        canonical: list[Node],
        stored_snapshot: MeetingTreeSnapshot | None,
        *,
        persist: bool = True,
    ) -> None:
        saved = clone_nodes(stored_snapshot.nodes) if stored_snapshot is not None else None
        self._canonical_reset_generation = (
            stored_snapshot.canonical_reset_generation if stored_snapshot is not None else 0
        )
        self._hidden_canonical_media = (
            copy.deepcopy(getattr(stored_snapshot, "hidden_canonical_media", {}))
            if stored_snapshot is not None
            else {}
        )
        self._deleted_source_keys = (
            set(stored_snapshot.deleted_source_keys) if stored_snapshot is not None else set()
        )
        self._linked_folder_files = (
            dict(stored_snapshot.linked_folder_files) if stored_snapshot is not None else {}
        )
        self._meeting_folder_imports = (
            copy.deepcopy(stored_snapshot.meeting_folder_imports)
            if stored_snapshot is not None
            else {}
        )
        pending_lookup = getattr(self, "_pending_sync_save_for_identity", None)
        pending_sync = pending_lookup(self._sync_identity) if callable(pending_lookup) else None
        if pending_sync is not None:
            pending_generation = getattr(
                pending_sync,
                "canonical_reset_generation",
                0,
            )
            if stored_snapshot is not None:
                saved = MeetingTreeController._merge_snapshot_with_sync(
                    self,
                    stored_snapshot,
                    pending_sync.nodes,
                    pending_sync.deleted_source_keys,
                    pending_generation,
                    getattr(pending_sync, "hidden_canonical_media", {}),
                )
            else:
                saved = clone_nodes(pending_sync.nodes)
                self._deleted_source_keys = set(pending_sync.deleted_source_keys)
                self._canonical_reset_generation = pending_generation
                self._hidden_canonical_media = copy.deepcopy(
                    getattr(pending_sync, "hidden_canonical_media", {})
                )
            self._linked_folder_files = {
                **self._linked_folder_files,
                **pending_sync.linked_folder_files,
            }
            self._meeting_folder_imports = {
                **self._meeting_folder_imports,
                **copy.deepcopy(pending_sync.meeting_folder_imports),
            }
            self._sync_folder = os.fspath(pending_sync.folder)
            self._sync_enabled = True
            self._sync_revision = pending_sync.expected_revision
        else:
            self._sync_enabled = False
            self._sync_revision = 0
            self._sync_folder = ""
        self._meeting_folder_pending_sources.clear()
        if saved is not None:
            migrate_publication_subsections(saved, canonical, self._deleted_source_keys)
        self._nodes = MeetingTreeMerger(
            canonical,
            self._deleted_source_keys,
        ).merge(saved)
        if persist:
            self._save()
        self._start_media_requests()
        self.chromeChanged.emit()
        self.syncStateChanged.emit()
        MeetingTreeController._refresh_canonical_restore_state(self)
        _emit_controller_state_changed(self)
        if pending_sync is None:
            self._request_sync_refresh()

    def _request_local_snapshot(self, canonical: list[Node]) -> None:
        self._local_snapshot_generation += 1
        generation = self._local_snapshot_generation
        if self._local_snapshot_operation_id:
            self._media_tree_runtime.operations.cancel(self._local_snapshot_operation_id)
        tree_key = self._tree_key
        canonical_hash = self._canonical_hash
        canonical_nodes = clone_nodes(canonical)
        operation_id = f"meeting-local-load:{uuid.uuid4().hex}"
        self._local_snapshot_operation_id = operation_id

        def run(_progress, _cancellation):
            return self._store.snapshot(tree_key)

        def commit(value: object) -> None:
            if (
                generation != self._local_snapshot_generation
                or tree_key != self._tree_key
                or canonical_hash != self._canonical_hash
            ):
                return
            self._local_snapshot_operation_id = ""
            if value is not None and not isinstance(value, MeetingTreeSnapshot):
                raise TypeError("Meeting snapshot load returned an invalid result")
            self._apply_canonical_snapshot(canonical_nodes, value)

        def finished_without_result(*_args) -> None:
            if generation == self._local_snapshot_generation:
                self._local_snapshot_operation_id = ""

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=f"meeting:{tree_key}",
                operation_type="meeting_local_snapshot_load",
                conflict_key=file_resource_key(self._store.path),
                presentation=MediaOperationPresentation.BACKGROUND,
                runner=run,
                commit=commit,
                priority=25,
                failed=finished_without_result,
                cancelled=finished_without_result,
            )
        )
        if not submitted:
            finished_without_result()

    def _cancel_local_snapshot(self) -> None:
        self._local_snapshot_generation = getattr(self, "_local_snapshot_generation", 0) + 1
        operation_id = getattr(self, "_local_snapshot_operation_id", "")
        if not operation_id:
            return
        self._media_tree_runtime.operations.cancel(operation_id)
        self._local_snapshot_operation_id = ""

    def _refresh_sync_availability(self) -> None:
        available = bool(self._sync_root and self._sync_identity is not None)
        if available == self._sync_available:
            return
        self._sync_available = available
        if not available:
            self._sync_enabled = False
            self._sync_folder = ""
            self._sync_revision = 0
        self.syncStateChanged.emit()

    def _pending_sync_save_for_identity(
        self,
        identity: MeetingSyncIdentity | None,
    ) -> _PendingSyncSave | None:
        if identity is None:
            return None
        return next(
            (
                request
                for request in getattr(self, "_pending_sync_saves", {}).values()
                if request.identity.tree_key == identity.tree_key
            ),
            None,
        )

    def _request_sync_refresh(self) -> None:
        root = self._sync_root
        identity = self._sync_identity
        tree_key = self._tree_key
        if not root or identity is None or not tree_key:
            self._refresh_sync_availability()
            return
        if self._pending_sync_save_for_identity(identity) is not None:
            return
        self._sync_refresh_generation += 1
        generation = self._sync_refresh_generation
        if self._sync_refresh_operation_id:
            self._media_tree_runtime.operations.cancel(self._sync_refresh_operation_id)
        operation_id = f"meeting-sync-refresh:{uuid.uuid4().hex}"
        self._sync_refresh_operation_id = operation_id

        def run(_progress, _cancellation):
            root_path = Path(root)
            if not root_path.is_dir():
                return _MeetingSyncDiscovery(False)
            folder = self._sync_service.locate_folder(
                root,
                identity,
                create=False,
            )
            record = self._sync_service.load_tree(root, identity)
            return _MeetingSyncDiscovery(True, folder, record)

        def commit(value: object) -> None:
            if (
                generation != self._sync_refresh_generation
                or self._tree_key != tree_key
                or self._sync_identity != identity
            ):
                return
            self._sync_refresh_operation_id = ""
            if not isinstance(value, _MeetingSyncDiscovery):
                raise TypeError("Meeting sync discovery returned an invalid result")
            self._apply_sync_discovery(value)

        def finished_without_result(*_args) -> None:
            if generation == self._sync_refresh_generation:
                self._sync_refresh_operation_id = ""

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=f"meeting:{tree_key}",
                operation_type="meeting_sync_refresh",
                conflict_key=folder_read_resource_claim(root),
                presentation=MediaOperationPresentation.BACKGROUND,
                runner=run,
                commit=commit,
                priority=-25,
                failed=finished_without_result,
                cancelled=finished_without_result,
            )
        )
        if not submitted:
            finished_without_result()

    def _cancel_sync_refresh(self) -> None:
        self._sync_refresh_generation += 1
        if not self._sync_refresh_operation_id:
            return
        self._media_tree_runtime.operations.cancel(self._sync_refresh_operation_id)
        self._sync_refresh_operation_id = ""

    def _apply_sync_discovery(self, discovery: _MeetingSyncDiscovery) -> None:
        self._sync_available = discovery.available
        if not discovery.available:
            self._sync_enabled = False
            self._sync_folder = ""
            self._sync_revision = 0
            self.syncStateChanged.emit()
            return
        record = discovery.record
        if record is None:
            self._sync_enabled = False
            self._sync_folder = str(discovery.folder or "")
            self._sync_revision = 0
            self.syncStateChanged.emit()
            return
        if self._sync_enabled and record.revision == self._sync_revision:
            return

        sync_nodes, sync_deleted = MeetingTreeController._migrate_sync_publications(
            self, record.nodes, record.deleted_source_keys,
        )
        local_generation = self._canonical_reset_generation
        remote_generation = record.canonical_reset_generation
        if remote_generation > local_generation:
            nodes = include_manual_meeting_nodes(sync_nodes, self._nodes)
            self._deleted_source_keys = sync_deleted
            self._canonical_reset_generation = remote_generation
            self._hidden_canonical_media = copy.deepcopy(record.hidden_canonical_media)
        elif local_generation > remote_generation:
            nodes = include_manual_meeting_nodes(self._nodes, sync_nodes)
        else:
            self._deleted_source_keys |= sync_deleted
            self._hidden_canonical_media = {
                **self._hidden_canonical_media,
                **copy.deepcopy(record.hidden_canonical_media),
            }
            nodes = merge_persisted_meeting_trees(
                self._nodes,
                sync_nodes,
                self._deleted_source_keys,
            )
        self._nodes = nodes
        self._linked_folder_files.update(record.linked_folder_files)
        self._meeting_folder_imports.update(copy.deepcopy(record.meeting_folder_imports))
        self._sync_folder = str(record.folder)
        self._sync_enabled = True
        self._sync_revision = record.revision
        MeetingTreeController._prune_deleted_source_keys(self)
        MeetingTreeController._refresh_canonical_restore_state(self)
        self._meeting_folder_pending_sources.clear()
        self._save_local_cache()
        self._schedule_sync_manifest_save()
        self._start_media_requests()
        self.chromeChanged.emit()
        self.syncStateChanged.emit()
        _emit_controller_state_changed(self)

    def _apply_sync_record(self, record: MeetingSyncRecord) -> None:
        self._nodes, self._deleted_source_keys = MeetingTreeController._migrate_sync_publications(
            self, record.nodes, record.deleted_source_keys,
        )
        self._linked_folder_files = record.linked_folder_files
        self._meeting_folder_imports = record.meeting_folder_imports
        self._canonical_reset_generation = getattr(
            record,
            "canonical_reset_generation",
            0,
        )
        self._hidden_canonical_media = copy.deepcopy(getattr(record, "hidden_canonical_media", {}))
        MeetingTreeController._prune_deleted_source_keys(self)
        self._sync_folder = str(record.folder)
        self._sync_enabled = True
        self._sync_revision = record.revision
        MeetingTreeController._refresh_canonical_restore_state(self)

    def _migrate_sync_publications(
        self, nodes: list[Node], deleted: set[str],
    ) -> tuple[list[Node], set[str]]:
        nodes = clone_nodes(nodes)
        deleted = set(deleted)
        migrate_publication_subsections(
            nodes, clone_nodes(getattr(self, "_canonical_nodes", [])), deleted,
        )
        return nodes, deleted

    # ── Meeting-folder autoimport ────────────────────────────────────────────

    def inject_linked_folder_media(self, watched_folder_path: str) -> None:
        """Scan meeting-targeted subfolders and import direct source files.

        - ``MW`` folders → ``lac`` section of the midweek tree
        - ``WE`` folders → ``public_talk`` section of the weekend tree

        Sources keep the same linked-folder meeting semantics as direct media:
        local items are tracked for availability and removal deletes their file.
        Processing state follows the active persistence target: local store when
        sync is off, linked-folder manifest when sync is on.
        """
        self.set_sync_root(watched_folder_path)
        if not watched_folder_path or not self._tree_key:
            return

        # Determine which monday this tree belongs to
        # tree_key format: "<pub_type>:<monday>:<lang>:<issue>"
        parts = self._tree_key.split(":")
        if len(parts) < 2:
            return
        tree_monday = parts[1]  # ISO date string
        tree_pub_type = parts[0]  # "mwb" or "wt"
        self._meeting_folder_scan_generation += 1
        generation = self._meeting_folder_scan_generation
        if self._meeting_folder_scan_operation_id:
            self._media_tree_runtime.operations.cancel(self._meeting_folder_scan_operation_id)
        operation_id = f"meeting-folder-scan:{uuid.uuid4().hex}"
        self._meeting_folder_scan_operation_id = operation_id
        tree_key = self._tree_key

        def run(_progress, _cancellation):
            return self._watched_folder_file_store.scan_meeting_sources(watched_folder_path)

        def commit(value: object) -> None:
            if generation != self._meeting_folder_scan_generation or self._tree_key != tree_key:
                return
            self._meeting_folder_scan_operation_id = ""
            if not isinstance(value, list):
                raise TypeError("Meeting-folder scan returned an invalid result")
            self._apply_meeting_folder_scan(value, tree_monday, tree_pub_type)

        def finished_without_result(*_args) -> None:
            if generation == self._meeting_folder_scan_generation:
                self._meeting_folder_scan_operation_id = ""

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=f"meeting:{tree_key}",
                operation_type="meeting_folder_scan",
                conflict_key=folder_read_resource_claim(watched_folder_path),
                presentation=MediaOperationPresentation.BACKGROUND,
                runner=run,
                commit=commit,
                priority=-25,
                failed=finished_without_result,
                cancelled=finished_without_result,
            )
        )
        if not submitted:
            finished_without_result()

    def _apply_meeting_folder_scan(
        self,
        folders: list[dict[str, Any]],
        tree_monday: str,
        tree_pub_type: str,
    ) -> None:
        touched = False
        for folder in folders:
            if not meeting_folder_matches_tree(
                folder,
                tree_monday=tree_monday,
                tree_pub_type=tree_pub_type,
            ):
                continue

            for source in folder.get("sources", []):
                office_conversion_available = (
                    self._document_conversion_service.office_conversion_available()
                    if str(source.get("kind") or "") == "lo"
                    else False
                )
                if not is_meeting_folder_source_supported(
                    source,
                    office_conversion_available=office_conversion_available,
                ):
                    continue
                source_key = str(source.get("source_key") or "")
                if not source_key or source_key in self._meeting_folder_pending_sources:
                    continue
                record = find_meeting_folder_import_record(
                    source,
                    self._meeting_folder_imports,
                )
                if not self._watched_folder_file_store.meeting_source_needs_processing(
                    source,
                    record,
                ):
                    continue

                if not record:
                    adopted_ids = self._adopt_existing_meeting_folder_source(
                        source, str(folder.get("path") or "")
                    )
                    if adopted_ids:
                        self._record_meeting_folder_import(source, adopted_ids)
                        touched = True
                        continue

                source_with_folder = dict(source)
                source_with_folder["folder_path"] = str(folder.get("path") or "")
                self._remove_previous_meeting_folder_nodes(record)
                target_list_id = self._meeting_folder_target_list_id(tree_pub_type)
                self._import_meeting_folder_source(source_with_folder, target_list_id, 0)
                touched = True

        self._start_media_requests()
        if touched:
            self.chromeChanged.emit()

    def _meeting_folder_target_list_id(self, tree_pub_type: str) -> str:
        target_section_code = target_section_code_for_pub_type(tree_pub_type)
        target_section = self._find_section_by_code(target_section_code)
        if target_section:
            return f"section:{target_section.get('id', '')}"
        return "root"

    def _adopt_existing_meeting_folder_source(
        self, source: dict[str, Any], folder_path: str
    ) -> list[str]:
        """Treat an already-present direct media file as processed."""
        if source.get("kind") != "media":
            return []
        source_path = str(source.get("path") or "")
        adopted: list[str] = []
        changed = False
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media":
                continue
            if not same_local_source(self._url_for_node(node), source_path):
                continue
            node_id = str(node.get("id") or "")
            if node_id:
                adopted.append(node_id)
            if node.get("linked_folder_source") != folder_path:
                node["linked_folder_source"] = folder_path
                changed = True
            if node_id and self._linked_folder_files.get(source_path) != node_id:
                self._linked_folder_files[source_path] = node_id
                changed = True
        if changed:
            self._save()
            _emit_controller_state_changed(self)
        return adopted

    def _remove_previous_meeting_folder_nodes(self, record: dict[str, Any] | None) -> None:
        if not isinstance(record, dict):
            return
        removed = False
        for node_id in list(record.get("node_ids", [])):
            if node_id and self._replace_node(str(node_id), []):
                removed = True
        if removed:
            self._save()
            _emit_controller_state_changed(self)
            self._emit_section_counts()

    def _record_meeting_folder_import(
        self,
        source: dict[str, Any],
        node_ids: list[str] | None = None,
        *,
        status: str = "processed",
        error: str = "",
    ) -> None:
        source_key = str(source.get("source_key") or "")
        record = make_meeting_folder_import_record(
            source,
            node_ids,
            status=status,
            error=error,
        )
        if not record:
            return
        self._meeting_folder_imports = upsert_meeting_folder_import_record(
            self._meeting_folder_imports,
            record,
        )
        self._meeting_folder_pending_sources.discard(source_key)
        self._save()

    def _record_meeting_folder_failure(self, source: dict[str, Any], name: str, error: str) -> None:
        self._record_meeting_folder_import(
            source,
            [],
            status="failed",
            error=error,
        )
        self._warn_import_failed(name, error)

    def _page_nodes(self, pages: list[str], stem: str) -> list[Node]:
        return [
            self._manual_media_node(
                page,
                title=tr_document_page_title(stem, idx + 1),
            )
            for idx, page in enumerate(pages)
        ]

    def _insert_meeting_folder_nodes(
        self,
        source: dict[str, Any],
        nodes: list[Node],
        list_id: str,
        insert_index: int,
    ) -> None:
        folder_path = str(source.get("folder_path") or "")
        linked_files: dict[str, str] = {}
        if folder_path:
            for node in nodes:
                node["linked_folder_source"] = folder_path
                url = self._url_for_node(node)
                node_id = str(node.get("id") or "")
                if url and node_id:
                    linked_files[url] = node_id
        if nodes:
            self._insert_nodes(list_id or "root", insert_index, nodes)
        if linked_files and not getattr(self, "_sync_enabled", False):
            self._linked_folder_files.update(linked_files)
        self._record_meeting_folder_import(
            source,
            [str(node.get("id") or "") for node in nodes if node.get("id")],
        )

    def _import_meeting_folder_source(
        self,
        source: dict[str, Any],
        list_id: str,
        insert_index: int,
    ) -> None:
        source_key = str(source.get("source_key") or "")
        path = str(source.get("path") or "")
        kind = str(source.get("kind") or "")
        if not source_key or not path:
            return
        self._meeting_folder_pending_sources.add(source_key)

        if kind == "media":
            self._insert_meeting_folder_nodes(
                source, [self._manual_media_node(path)], list_id, insert_index
            )
            return
        if kind == "pdf":
            self._import_meeting_folder_pdf(source, list_id, insert_index)
            return
        if kind == "lo":
            self._import_meeting_folder_lo(source, list_id, insert_index)
            return
        if kind == "jwpub":
            self._import_meeting_folder_jwpub(source, list_id, insert_index)
            return
        if kind == "jwlplaylist":
            self._import_meeting_folder_jwlplaylist(source, list_id, insert_index)
            return
        self._meeting_folder_pending_sources.discard(source_key)

    def _import_meeting_folder_pdf(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        path = str(source.get("path") or "")
        stem = Path(path).stem
        pages = self._document_conversion_service.cached_pdf_pages(path)
        if pages:
            self._insert_meeting_folder_nodes(
                source, self._page_nodes(pages, stem), list_id, insert_index
            )
            return
        thread = self._document_conversion_service.create_pdf_thread(path, parent=self)
        self._pdf_threads.append(thread)
        thread.pages_ready.connect(
            lambda pages, pdf_stem, _source=dict(source), _list=list_id, _index=insert_index: (
                self._insert_meeting_folder_nodes(
                    _source, self._page_nodes(pages, pdf_stem), _list, _index
                )
            )
        )
        thread.conversion_failed.connect(
            lambda error, _source=dict(source), _name=stem: self._record_meeting_folder_failure(
                _source, _name, error
            )
        )
        thread.finished.connect(
            lambda t=thread: self._pdf_threads.remove(t) if t in self._pdf_threads else None
        )
        thread.start()

    def _import_meeting_folder_lo(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        path = str(source.get("path") or "")
        stem = Path(path).stem
        pages = self._document_conversion_service.cached_office_pages(path)
        if pages:
            self._insert_meeting_folder_nodes(
                source, self._page_nodes(pages, stem), list_id, insert_index
            )
            return
        thread = self._document_conversion_service.create_office_thread(
            path,
            parent=self,
        )
        self._lo_threads.append(thread)
        thread.pages_ready.connect(
            lambda pages, doc_stem, _source=dict(source), _list=list_id, _index=insert_index: (
                self._insert_meeting_folder_nodes(
                    _source, self._page_nodes(pages, doc_stem), _list, _index
                )
            )
        )
        thread.conversion_failed.connect(
            lambda error, _source=dict(source), _name=stem: self._record_meeting_folder_failure(
                _source, _name, error
            )
        )
        thread.finished.connect(
            lambda t=thread: self._lo_threads.remove(t) if t in self._lo_threads else None
        )
        thread.start()

    def _import_meeting_folder_jwpub(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        path = str(source.get("path") or "")
        stem = Path(path).stem
        thread = self._jwpub_import_thread_factory.create(
            path,
            lang=self._language_code,
            parent=self,
        )
        self._jwpub_threads.append(thread)
        thread.items_ready.connect(
            lambda items, file_stem, _source=dict(source), _list=list_id, _index=insert_index: (
                self._insert_meeting_folder_nodes(
                    _source,
                    [self._node_from_playlist_item(raw, file_stem) for raw in items],
                    _list,
                    _index,
                )
            )
        )
        thread.failed.connect(
            lambda error, _source=dict(source), _name=stem: self._record_meeting_folder_failure(
                _source, _name, error
            )
        )
        thread.finished.connect(
            lambda t=thread: self._jwpub_threads.remove(t) if t in self._jwpub_threads else None
        )
        thread.start()

    def _import_meeting_folder_jwlplaylist(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        path = str(source.get("path") or "")
        self._queue_jwl_import(
            [path],
            list_id,
            insert_index,
            linked_source=source,
        )

    def _find_section_by_code(self, section_code: str) -> Node | None:
        """Find a section node by its ``section_code`` attribute."""
        for node in iter_nodes(self._nodes):
            if node.get("type") in ("section", "subsection"):
                if node.get("section_code") == section_code:
                    return node
                # Also match by meeting_source_key pattern
                source_key = node.get("meeting_source_key", "")
                if source_key.endswith(f":{section_code}"):
                    return node
        return None

    def cleanup(self) -> None:
        owner_id = self._tree_session.owner_id
        if owner_id:
            self._media_tree_runtime.operations.cancel_scope(owner_id)
        if self._modal_operation_id:
            self._media_tree_runtime.operations.cancel(self._modal_operation_id)
            self._modal_operation_id = ""
        self._cancel_local_snapshot()
        self._cancel_sync_refresh()
        self._meeting_folder_scan_generation += 1
        if self._meeting_folder_scan_operation_id:
            self._media_tree_runtime.operations.cancel(self._meeting_folder_scan_operation_id)
            self._meeting_folder_scan_operation_id = ""
        self._tree_session.close()
        self._flush_image_framing_save()
        self._flush_derived_media_patches()
        self._sync_save_timer.stop()
        for request in self._pending_sync_saves.values():
            pending = copy.deepcopy(request)
            self._media_tree_runtime.snapshots.request(
                f"meeting-manifest:{os.path.normcase(os.path.abspath(pending.folder))}",
                lambda current=pending: retry_manifest_write(
                    lambda: self._sync_service.save_tree(
                        current.folder,
                        current.identity,
                        nodes=current.nodes,
                        deleted_source_keys=current.deleted_source_keys,
                        linked_folder_files=current.linked_folder_files,
                        meeting_folder_imports=current.meeting_folder_imports,
                        expected_revision=current.expected_revision,
                        canonical_reset_generation=current.canonical_reset_generation,
                        hidden_canonical_media=current.hidden_canonical_media,
                    )
                ),
                conflict_key=child_folder_resource_claim(pending.folder),
            )
        if self._sync_save_future is not None:
            self._sync_save_future.cancel()
        self._sync_save_executor.shutdown(wait=False, cancel_futures=True)
        self._pending_sync_saves.clear()
        _reset_media_requests(self)
        for threads in (
            self._pdf_threads,
            self._lo_threads,
            self._jwpub_threads,
        ):
            for thread in list(threads):
                stop_owned_qthread(
                    thread,
                    wait_ms=3_000,
                    label="Meeting tree",
                )
            threads.clear()
        try:
            self._info_queue.info_ready.disconnect(self._on_info_ready)
            self._info_queue.duration_ready.disconnect(self._on_duration_ready)
            self._info_queue.request_failed.disconnect(self._on_info_failed)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect meeting tree info queue")
        try:
            self._svc.media_resolved.disconnect(self._on_media_resolved)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect meeting tree video resolver")
        try:
            mgr = self._media_cache_manager
            mgr.cache_changed.disconnect(self._on_cache_changed)
            mgr.cache_removed.disconnect(self._on_cache_removed)
            mgr.prefetch_progress.disconnect(self._on_prefetch_progress)
            mgr.prefetch_error.disconnect(self._on_prefetch_error)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect meeting tree cache signals")

    def _accept_probed_thumbnail(
        self,
        item_id: str,
        image: QImage | None,
    ) -> None:
        if image is None or image.isNull():
            self._thumb_cache.pop(item_id, None)
            return
        self._thumb_cache[item_id] = QPixmap.fromImage(image)

    def add_files(
        self, paths: list[str], list_id: str = "root", insert_index: int = _BIG_INDEX
    ) -> None:
        media_paths = []
        pdf_paths = []
        jwpub_paths = []
        jwl_paths = []
        lo_paths = []
        for path in paths:
            ext = Path(path).suffix.lower() if path else ""
            if ext in MEDIA_EXTS:
                media_paths.append(path)
            elif ext in PDF_EXTS:
                pdf_paths.append(path)
            elif ext in JWPUB_EXTS:
                jwpub_paths.append(path)
            elif ext in PLAYLIST_EXTS:
                jwl_paths.append(path)
            elif (
                ext in (PPTX_EXTS | DOCX_EXTS)
                and self._document_conversion_service.office_conversion_available()
            ):
                lo_paths.append(path)

        cursor = insert_index
        if media_paths:
            nodes = [self._manual_media_node(path) for path in media_paths]
            self._insert_nodes(list_id or "root", cursor, nodes)
            if cursor < _BIG_INDEX:
                cursor += len(nodes)
        if pdf_paths:
            self._import_pdfs(pdf_paths, list_id or "root", cursor)
        if jwpub_paths:
            self._import_jwpubs(jwpub_paths, list_id or "root", cursor)
        if jwl_paths:
            self._import_jwlplaylists(jwl_paths, list_id or "root", cursor)
        if lo_paths:
            self._import_lo_files(lo_paths, list_id or "root", cursor)

    def add_from_jw_catalog(
        self,
        item_data: MediaInsertPayload | dict[str, Any],
        list_id: str = "root",
        insert_index: int = _BIG_INDEX,
    ) -> MediaInsertResult:
        payload = (
            item_data
            if isinstance(item_data, MediaInsertPayload)
            else MediaInsertPayload.from_mapping(item_data)
        )
        node_id = new_node_id()
        node = create_jw_media_node(
            payload,
            node_id=node_id,
            fallback_title=_tr("_MediaRow", "Media"),
        )
        ref = node["media_ref"]
        partition = partition_media_items(self._media_identity_records(), [ref])
        if partition.duplicate_items:
            return MediaInsertResult(duplicate_items=partition.duplicate_items)
        thumb_path = payload.thumbnail_path
        thumbnail_copy: tuple[str, str] | None = None
        if thumb_path:
            source_key = self._thumbnail_provenance(node)
            storage_id = self._thumbnail_storage_id(node, node_id, source_key)
            target_path = self._meeting_thumbnail_store.path(storage_id)
            node["thumbnail_cache_key"] = target_path.name
            node["thumbnail_local_path"] = os.fspath(target_path)
            node["thumbnail_source_key"] = thumbnail_source_fingerprint(source_key)
            thumbnail_copy = (storage_id, thumb_path)
        if not self._insert_nodes(list_id or "root", insert_index, [node]):
            return MediaInsertResult(target_valid=False)
        if thumbnail_copy is not None:
            storage_id, source_path = thumbnail_copy
            self._media_tree_runtime.thumbnails.copy_file(
                owner_id=self._tree_session.owner_id,
                node_id=node_id,
                storage_id=storage_id,
                store=self._meeting_thumbnail_store,
                source=source_path,
                source_signature=(
                    thumbnail_source_fingerprint(payload.thumbnail_url)
                    if payload.thumbnail_url
                    else ""
                ),
            )
        return MediaInsertResult(added_items=(payload.to_identity_mapping(),))

    def _media_identity_records(self) -> list[dict[str, Any]]:
        return [
            node.get("media_ref") or {}
            for node in iter_nodes(self._nodes)
            if node.get("type") == "media"
        ]

    def _import_pdfs(self, paths: list[str], list_id: str, insert_index: int) -> None:
        for path in paths:
            stem = Path(path).stem
            pages = self._document_conversion_service.cached_pdf_pages(path)
            if pages:
                self._on_pdf_pages_ready(pages, stem, list_id, insert_index)
                if insert_index < _BIG_INDEX:
                    insert_index += len(pages)
                continue
            thread = self._document_conversion_service.create_pdf_thread(
                path,
                parent=self,
            )
            self._pdf_threads.append(thread)
            thread.pages_ready.connect(
                lambda pages, pdf_stem, _list=list_id, _index=insert_index: (
                    self._on_pdf_pages_ready(pages, pdf_stem, _list, _index)
                )
            )
            thread.conversion_failed.connect(
                lambda error, _name=stem: self._warn_import_failed(_name, error)
            )
            thread.finished.connect(
                lambda t=thread: self._pdf_threads.remove(t) if t in self._pdf_threads else None
            )
            thread.start()

    def _on_pdf_pages_ready(
        self,
        pages: list[str],
        pdf_stem: str,
        list_id: str,
        insert_index: int,
    ) -> None:
        nodes = [
            self._manual_media_node(
                page,
                title=tr_document_page_title(pdf_stem, idx + 1),
            )
            for idx, page in enumerate(pages)
        ]
        if nodes:
            self._insert_nodes(list_id or "root", insert_index, nodes)

    def _import_lo_files(self, paths: list[str], list_id: str, insert_index: int) -> None:
        for path in paths:
            stem = Path(path).stem
            pages = self._document_conversion_service.cached_office_pages(path)
            if pages:
                self._on_lo_pages_ready(pages, stem, list_id, insert_index)
                if insert_index < _BIG_INDEX:
                    insert_index += len(pages)
                continue
            thread = self._document_conversion_service.create_office_thread(
                path,
                parent=self,
            )
            self._lo_threads.append(thread)
            thread.pages_ready.connect(
                lambda pages, doc_stem, _list=list_id, _index=insert_index: self._on_lo_pages_ready(
                    pages, doc_stem, _list, _index
                )
            )
            thread.conversion_failed.connect(
                lambda error, _name=stem: self._warn_import_failed(_name, error)
            )
            thread.finished.connect(
                lambda t=thread: self._lo_threads.remove(t) if t in self._lo_threads else None
            )
            thread.start()

    def _on_lo_pages_ready(
        self,
        pages: list[str],
        stem: str,
        list_id: str,
        insert_index: int,
    ) -> None:
        nodes = [
            self._manual_media_node(
                page,
                title=tr_document_page_title(stem, idx + 1),
            )
            for idx, page in enumerate(pages)
        ]
        if nodes:
            self._insert_nodes(list_id or "root", insert_index, nodes)

    def _import_jwpubs(self, paths: list[str], list_id: str, insert_index: int) -> None:
        for path in paths:
            stem = Path(path).stem
            thread = self._jwpub_import_thread_factory.create(
                path,
                lang=self._language_code,
                dest_images_dir=self._jwpub_image_dir(),
                parent=self,
            )
            self._jwpub_threads.append(thread)
            thread.items_ready.connect(
                lambda items, file_stem, _list=list_id, _index=insert_index: (
                    self._on_playlist_items_ready(items, file_stem, _list, _index)
                )
            )
            thread.failed.connect(lambda error, _name=stem: self._warn_import_failed(_name, error))
            thread.finished.connect(
                lambda t=thread: self._jwpub_threads.remove(t) if t in self._jwpub_threads else None
            )
            thread.start()

    def _import_jwlplaylists(
        self,
        paths: list[str],
        list_id: str,
        insert_index: int,
    ) -> None:
        self._queue_jwl_import(paths, list_id, insert_index)

    def _queue_jwl_import(
        self,
        paths: list[str],
        list_id: str,
        insert_index: int,
        *,
        linked_source: dict[str, Any] | None = None,
    ) -> None:
        normalized = [str(Path(path)) for path in paths if path]
        owner_id = self._tree_session.owner_id
        tree_key = self._tree_key
        session_generation = self._tree_session.generation
        if not normalized or not owner_id or not tree_key:
            return
        operation_id = f"meeting-jwl-import:{uuid.uuid4().hex}"
        placeholders = [
            {
                "id": new_node_id(),
                "type": "media",
                "title": Path(path).stem,
                "media_type": "video",
                "media_ref": {"file_path": ""},
                "children": [],
                "meeting_generated": False,
            }
            for path in normalized
        ]
        self._tree_session.add_pending(
            operation_id,
            placeholders,
            target_list_id=list_id,
            insert_index=insert_index,
        )
        stage = _tr("PlaylistTransfer", "Opening playlist…")
        fallback_language = self._fallback_language_code
        profile_store = self._profile_media_store
        embedded_conflict = folder_resource_key(profile_store.embedded_dir)

        def run(report, cancellation):
            created_paths: list[Path] = []

            def save_embedded(
                data: bytes,
                filename: str,
                identifier: str,
                default_suffix: str,
            ) -> str:
                if cancellation.is_set():
                    raise MediaOperationCancelled("Playlist import cancelled")
                destination = profile_store.save_embedded(
                    data,
                    filename,
                    identifier=identifier,
                    default_suffix=default_suffix,
                )
                created_paths.append(Path(destination))
                return destination

            nodes: list[Node] = []
            try:
                for index, path in enumerate(normalized):
                    if cancellation.is_set():
                        raise MediaOperationCancelled("Playlist import cancelled")
                    report(
                        MediaOperationProgress(
                            MediaOperationState.PROCESSING,
                            stage=stage,
                            detail=Path(path).name,
                            completed=index,
                            total=len(normalized),
                        )
                    )
                    document = read_jwlplaylist_document(
                        path,
                        fallback_lang_code=fallback_language,
                    )
                    result = playlist_items_from_jwl_document_items(
                        document.items,
                        source_name=Path(path).name,
                        save_embedded=save_embedded,
                    )
                    for raw in result.items:
                        node_id = new_node_id()
                        nodes.append(
                            create_playlist_media_node(
                                raw,
                                Path(path).stem,
                                node_id=node_id,
                                url=playlist_item_media_url(raw),
                                media_fallback_title=_tr("_MediaRow", "Media"),
                            )
                        )
                if cancellation.is_set():
                    raise MediaOperationCancelled("Playlist import cancelled")
                return _JwlMeetingImport(nodes, tuple(created_paths))
            except BaseException:  # noqa: BLE001 - imported-media rollback boundary
                for created in reversed(created_paths):
                    if not profile_store.remove_file(created):
                        log.warning(
                            "Could not roll back meeting playlist import %s",
                            created,
                        )
                raise

        def commit(value: object) -> None:
            if not isinstance(value, _JwlMeetingImport):
                raise TypeError("Meeting playlist import returned an invalid result")
            if self._tree_key != tree_key or self._tree_session.generation != session_generation:
                discard(value)
                return
            if linked_source is not None:
                self._insert_meeting_folder_nodes(
                    linked_source,
                    value.nodes,
                    list_id,
                    insert_index,
                )
            elif value.nodes and not self._insert_nodes(
                list_id or "root",
                insert_index,
                value.nodes,
            ):
                raise RuntimeError("Meeting insertion target is no longer available")
            self._tree_session.remove_pending(operation_id)

        def discard(value: object | None) -> None:
            if linked_source is not None:
                self._meeting_folder_pending_sources.discard(
                    str(linked_source.get("source_key") or "")
                )
            if isinstance(value, _JwlMeetingImport):
                self._media_tree_runtime.schedule_artifact_cleanup(
                    value.created_paths,
                    conflict_key=embedded_conflict,
                )

        def failed(message: str, _retryable: bool) -> None:
            if linked_source is not None:
                self._record_meeting_folder_failure(
                    linked_source,
                    Path(normalized[0]).name,
                    message,
                )
                self._meeting_folder_pending_sources.add(str(linked_source.get("source_key") or ""))
            else:
                self._warn_import_failed(Path(normalized[0]).name, message)

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=owner_id,
                operation_type="meeting_jwl_import",
                conflict_key=embedded_conflict,
                presentation=MediaOperationPresentation.TREE_LOCAL,
                runner=run,
                commit=commit,
                priority=100,
                initial_stage=stage,
                retryable=True,
                failed=failed,
                discarded=discard,
            )
        )
        if not submitted:
            discard(None)
            self._tree_session.remove_pending(operation_id)

    def _on_playlist_items_ready(
        self,
        items: list[dict[str, Any]],
        file_stem: str,
        list_id: str,
        insert_index: int,
    ) -> None:
        nodes = [self._node_from_playlist_item(raw, file_stem) for raw in items]
        if nodes:
            self._insert_nodes(list_id or "root", insert_index, nodes)

    def _save_jwl_embedded(
        self,
        data: bytes,
        filename: str,
        identifier: str,
        default_suffix: str,
    ) -> str:
        return self._profile_media_store.save_embedded(
            data,
            filename,
            identifier=identifier,
            default_suffix=default_suffix,
        )

    def _node_from_playlist_item(self, raw: dict[str, Any], fallback_title: str) -> Node:
        node_id = new_node_id()
        url = playlist_item_media_url(raw)
        if raw.get("data") and not url:
            url = self._profile_media_store.save_embedded(
                raw["data"],
                str(raw.get("filename") or "media"),
                identifier=node_id,
            )
        return create_playlist_media_node(
            raw,
            fallback_title,
            node_id=node_id,
            url=url,
            media_fallback_title=_tr("_MediaRow", "Media"),
        )

    def _warn_import_failed(self, name: str, error: str) -> None:
        parent = self.parent()
        QMessageBox.warning(
            parent,
            _tr("_PlaylistEditView", "Add Media"),
            f"{name}\n{str(error)[:160]}",
        )

    @Slot()
    def backClicked(self):
        self._flush_image_framing_save()
        self.backRequested.emit()

    @Slot()
    def addClicked(self):
        parent = self.parent()
        filter_text = (
            "Media, PDF & Publication "
            "(*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.m4a *.wav "
            "*.jpg *.jpeg *.png *.gif *.webp *.pdf *.jwpub *.jwlplaylist);;"
            "JW Playlist (*.jwlplaylist);;"
            "JW Publication (*.jwpub);;"
            "PDF (*.pdf);;"
            "Video (*.mp4 *.mkv *.mov *.avi *.webm);;"
            "Audio (*.mp3 *.m4a *.wav);;"
            "Image (*.jpg *.jpeg *.png *.gif *.webp);;"
            "All (*)"
        )
        paths, _ = QFileDialog.getOpenFileNames(
            parent,
            _tr("_PlaylistEditView", "Add Media"),
            "",
            filter_text,
        )
        if paths:
            self.add_files(paths)

    @Slot()
    def toggleSync(self) -> None:
        if self._sync_busy:
            return
        if self._sync_enabled:
            self._disable_sync()
        else:
            self._enable_sync()

    @Slot()
    def restoreCanonicalContent(self) -> None:
        if not self._canonical_nodes:
            return
        if not MeetingTreeController._flush_image_framing_save(self):
            return
        if not MeetingTreeController._flush_derived_media_patches(self):
            return
        diff = self._canonical_diff()
        if not diff.has_changes:
            MeetingTreeController._refresh_canonical_restore_state(self)
            return

        message = _tr(
            _CANONICAL_RESTORE_CONTEXT,
            _RESTORE_CONFIRM_SOURCE,
        )
        reply = QMessageBox.question(
            self.parent(),
            _tr(_CANONICAL_RESTORE_CONTEXT, _RESTORE_TITLE_SOURCE),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        fallback_key = {
            "mwb": "section:mwb:lac",
            "wt": "section:wt:public_talk",
        }.get(self._meeting_type, "")
        tree_key = self._tree_key
        canonical_nodes = clone_nodes(self._canonical_nodes)
        current_nodes = clone_nodes(self._nodes)
        hidden_media = copy.deepcopy(self._hidden_canonical_media)
        orphan_media_ids = MeetingTreeController._meeting_folder_imported_node_ids(self)
        linked_files = dict(self._linked_folder_files)
        generation = self._canonical_reset_generation + 1
        sync_folder = self._sync_folder if self._sync_enabled else ""
        generated_roots = self._generated_asset_roots()
        stage = _tr("MediaDestinationDialog", "Preparing media")

        def run(report, cancellation):
            report(MediaOperationProgress(MediaOperationState.PROCESSING, stage=stage))
            candidate = restore_canonical_tree(
                canonical_nodes,
                current_nodes,
                hidden_canonical_media=hidden_media,
                orphan_section_source_key=fallback_key,
                orphan_media_ids=orphan_media_ids,
            )
            candidate_linked_files = linked_files
            if sync_folder:
                report(MediaOperationProgress(MediaOperationState.COPYING, stage=stage))
                candidate, candidate_linked_files = self._sync_service.materialize_tree_files(
                    candidate,
                    Path(sync_folder),
                    generated_roots=generated_roots,
                    cancellation=cancellation,
                )
            return _CanonicalRestoreState(
                candidate,
                candidate_linked_files,
                generation,
            )

        def commit(value: object) -> None:
            if not isinstance(value, _CanonicalRestoreState):
                raise TypeError("Canonical restore returned an invalid result")
            if self._tree_key != tree_key:
                raise RuntimeError("Meeting changed while content was being restored")
            self._nodes = value.nodes
            self._deleted_source_keys.clear()
            self._linked_folder_files = value.linked_folder_files
            self._canonical_reset_generation = value.generation
            self._hidden_canonical_media = {}
            _reset_media_requests(self)
            MeetingTreeController._refresh_canonical_restore_state(self)
            if self._sync_enabled:
                self._schedule_sync_manifest_save()
            self._save_local_cache()
            MeetingTreeController._resolve_unresolved_canonical_media(
                self,
                value.nodes,
            )
            self._start_media_requests()
            self.chromeChanged.emit()
            self._emit_section_counts()
            _emit_controller_state_changed(self)

        def failed(message: str) -> None:
            self.storageSaveFailed.emit(tree_key, message)

        self._submit_modal_operation(
            operation_type="canonical_restore",
            conflict_key=f"meeting-tree:{tree_key}",
            stage=stage,
            runner=run,
            commit=commit,
            failed=failed,
        )

    def _enable_sync(self) -> None:
        if not self._sync_available or self._sync_identity is None:
            return
        self._cancel_sync_refresh()
        tree_key = self._tree_key
        sync_root = self._sync_root
        identity = self._sync_identity
        nodes = clone_nodes(self._nodes)
        deleted_source_keys = set(self._deleted_source_keys)
        meeting_folder_imports = copy.deepcopy(self._meeting_folder_imports)
        canonical_generation = self._canonical_reset_generation
        hidden_media = copy.deepcopy(self._hidden_canonical_media)
        generated_roots = self._generated_asset_roots()
        stage = _tr("_PlaylistEditView", "Offline / Syncing")

        def run(report, cancellation):
            report(MediaOperationProgress(MediaOperationState.PREPARING, stage=stage))
            folder = self._sync_service.locate_folder(
                sync_root,
                identity,
                create=True,
            )
            if folder is None:
                raise MeetingSyncError("Linked folder is not available.")
            record = self._sync_service.load_tree(sync_root, identity)
            if record is not None:
                return record
            report(MediaOperationProgress(MediaOperationState.COPYING, stage=stage))
            materialized, linked_files = self._sync_service.materialize_tree_files(
                nodes,
                folder,
                generated_roots=generated_roots,
                cancellation=cancellation,
            )
            report(MediaOperationProgress(MediaOperationState.FINALIZING, stage=stage))
            return self._sync_service.save_tree(
                folder,
                identity,
                nodes=materialized,
                deleted_source_keys=deleted_source_keys,
                linked_folder_files=linked_files,
                meeting_folder_imports=meeting_folder_imports,
                expected_revision=0,
                canonical_reset_generation=canonical_generation,
                hidden_canonical_media=hidden_media,
            )

        def commit(value: object) -> None:
            if not isinstance(value, MeetingSyncRecord):
                raise TypeError("Meeting sync returned an invalid result")
            if self._tree_key != tree_key or self._sync_identity != identity:
                raise RuntimeError("Meeting changed while sync was being enabled")
            self._apply_sync_record(value)
            self._save_local_cache()
            self._start_media_requests()
            self.chromeChanged.emit()
            _emit_controller_state_changed(self)

        self._submit_modal_operation(
            operation_type="enable_meeting_sync",
            conflict_key=folder_resource_key(sync_root),
            stage=stage,
            runner=run,
            commit=commit,
            failed=self._warn_sync_failed,
        )

    def _disable_sync(self) -> None:
        if not self._sync_folder:
            return
        current_key = os.path.normcase(os.path.abspath(self._sync_folder))
        if current_key in self._pending_sync_saves:
            return
        reply = QMessageBox.question(
            self.parent(),
            _tr("MeetingSync", "Meeting sync"),
            _tr(
                "MeetingSync",
                "Turn off sync for this meeting?\nThe folder files will be kept.",
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._cancel_sync_refresh()

        folder = Path(self._sync_folder)
        folder_key = os.path.normcase(os.path.abspath(folder))
        self._pending_sync_saves.pop(folder_key, None)
        self._arm_sync_save_timer()
        tree_key = self._tree_key
        nodes = clone_nodes(self._nodes)
        hidden_media = copy.deepcopy(self._hidden_canonical_media)
        durable_dir = self._durable_detached_dir()
        stage = _tr("_PlaylistEditView", "Offline / Syncing")

        def run(report, cancellation):
            report(MediaOperationProgress(MediaOperationState.COPYING, stage=stage))
            detached_nodes = self._sync_service.detach_cache_references(
                nodes,
                folder,
                durable_dir,
                cancellation=cancellation,
            )
            hidden_keys = list(hidden_media)
            detached_hidden = self._sync_service.detach_cache_references(
                [hidden_media[key] for key in hidden_keys],
                folder,
                durable_dir,
                cancellation=cancellation,
            )
            report(MediaOperationProgress(MediaOperationState.FINALIZING, stage=stage))
            self._sync_service.delete_sync_metadata(folder)
            hidden = {key: node for key, node in zip(hidden_keys, detached_hidden, strict=True)}
            linked_files: dict[str, str] = {}
            for node in iter_nodes(detached_nodes):
                if node.get("type") != "media" or not node.get("linked_folder_source"):
                    continue
                node_id = str(node.get("id") or "")
                ref = node.get("media_ref") or {}
                url = str(node.get("resolved_url") or ref.get("file_path") or "")
                if node_id and url:
                    linked_files[url] = node_id
            return _DisabledSyncState(detached_nodes, hidden, linked_files, folder)

        def commit(value: object) -> None:
            if not isinstance(value, _DisabledSyncState):
                raise TypeError("Meeting sync detach returned an invalid result")
            if self._tree_key != tree_key or Path(self._sync_folder) != value.folder:
                raise RuntimeError("Meeting changed while sync was being disabled")
            self._nodes = value.nodes
            self._hidden_canonical_media = value.hidden_canonical_media
            self._linked_folder_files = value.linked_folder_files
            self._sync_enabled = False
            self._sync_revision = 0
            self._save_local_cache()
            self._sync_folder = str(value.folder)
            if self._sync_root:
                self.inject_linked_folder_media(self._sync_root)
            self.chromeChanged.emit()
            _emit_controller_state_changed(self)

        self._submit_modal_operation(
            operation_type="disable_meeting_sync",
            conflict_key=child_folder_resource_claim(folder),
            stage=stage,
            runner=run,
            commit=commit,
            failed=self._warn_sync_failed,
        )

    def _set_sync_busy(self, value: bool, message: str = "") -> None:
        normalized_message = message if value else ""
        if self._sync_busy == value and self._sync_busy_message == normalized_message:
            return
        self._sync_busy = value
        self._sync_busy_message = normalized_message
        self.syncStateChanged.emit()

    @Slot()
    def cancelModalOperation(self) -> None:  # noqa: N802 - QML API
        if self._modal_operation_id:
            self._media_tree_runtime.operations.cancel(self._modal_operation_id)

    def _submit_modal_operation(
        self,
        *,
        operation_type: str,
        conflict_key: str,
        stage: str,
        runner,
        commit,
        failed,
    ) -> bool:
        if self._modal_operation_id:
            return False
        operation_id = f"{operation_type}:{uuid.uuid4().hex}"
        self._modal_operation_id = operation_id
        self._set_sync_busy(True, stage)

        def finish() -> None:
            if self._modal_operation_id != operation_id:
                return
            self._modal_operation_id = ""
            self._set_sync_busy(False)

        def guarded_commit(value: object) -> None:
            if self._modal_operation_id != operation_id:
                return
            commit(value)
            finish()

        def guarded_failure(message: str, _retryable: bool) -> None:
            if self._modal_operation_id != operation_id:
                return
            finish()
            failed(message)

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=f"meeting:{self._tree_key}",
                operation_type=operation_type,
                conflict_key=conflict_key,
                presentation=MediaOperationPresentation.WINDOW_MODAL,
                runner=runner,
                commit=guarded_commit,
                priority=100,
                initial_stage=stage,
                failed=guarded_failure,
                cancelled=finish,
            )
        )
        if not submitted:
            finish()
        return submitted

    def _durable_detached_dir(self) -> Path:
        return self._profile_paths.embedded_dir / "meeting_sync"

    def _warn_sync_failed(
        self,
        message: str,
        *,
        folder_path: str | None = None,
    ) -> bool:
        dialog = QMessageBox(self.parent())
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(_tr("MeetingSync", "Meeting sync"))
        dialog.setText(_tr("MeetingSync", "Could not update meeting sync."))
        dialog.setInformativeText(
            _tr(
                "MeetingSync",
                "Your changes are saved on this computer. Try again or open the linked folder.",
            )
        )
        if message:
            dialog.setDetailedText(message[:2_000])
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Retry
            | QMessageBox.StandardButton.Open
            | QMessageBox.StandardButton.Close
        )
        result = dialog.exec()
        if result == QMessageBox.StandardButton.Retry:
            if folder_path is None:
                self._schedule_sync_manifest_save(delay_ms=0)
            return True
        target_folder = folder_path or self._sync_folder
        if result == QMessageBox.StandardButton.Open and target_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(target_folder))
        return False

    @Slot()
    def newSectionClicked(self):
        dlg = NameDialog(
            parent=self.parent(),
            lang=True,
            label=_tr(_PLAYLIST_EDIT_CONTEXT, "Section name:"),
            placeholder=_tr(_PLAYLIST_EDIT_CONTEXT, "E.g.: Introduction"),
        )
        dlg.setWindowTitle(_tr(_PLAYLIST_EDIT_CONTEXT, "New Section"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        hues = [int(n.get("color_hue", 0)) for n in iter_nodes(self._nodes)]
        node = {
            "id": new_node_id(),
            "type": "section",
            "title": name,
            "color_hue": generate_section_hue(hues),
            "collapsed": False,
            "children": [],
            "meeting_generated": False,
        }
        self._insert_nodes("root", _BIG_INDEX, [node], signal_name="nodes")

    @Slot(str)
    def newSubsectionClicked(self, parent_section_id: str):
        parent_node = self._find_node(parent_section_id)
        if not parent_node or parent_node.get("type") != "section":
            return
        dlg = NameDialog(
            parent=self.parent(),
            lang=True,
            label=_tr(_PLAYLIST_EDIT_CONTEXT, "Subsection name:"),
            placeholder=_tr(_PLAYLIST_EDIT_CONTEXT, "E.g.: Part 1"),
        )
        dlg.setWindowTitle(_tr(_PLAYLIST_EDIT_CONTEXT, "New Subsection"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        node = {
            "id": new_node_id(),
            "type": "subsection",
            "title": name,
            "color_hue": parent_node.get("color_hue", 215),
            "collapsed": False,
            "children": [],
            "meeting_generated": False,
        }
        self._insert_nodes(f"section:{parent_section_id}", _BIG_INDEX, [node], signal_name="nodes")

    @Slot(str)
    def newMarkerClicked(self, subsection_id: str):
        parent_node = self._find_node(subsection_id)
        if not parent_node or parent_node.get("type") != "subsection":
            return
        node = {
            "id": new_node_id(),
            "type": "marker",
            "text": "",
            "children": [],
            "meeting_generated": False,
        }
        if parent_node.get("collapsed"):
            parent_node["collapsed"] = False
            self._insert_nodes(f"subsection:{subsection_id}", 0, [node], signal_name="state")
        else:
            self._insert_nodes(f"subsection:{subsection_id}", 0, [node], signal_name="nodes")
        self.markerEditRequested.emit(node["id"])

    @Slot(str)
    def projectItem(self, item_id: str):
        self._flush_image_framing_save()
        node = self._find_node(item_id)
        if not node or node.get("type") != "media":
            return
        ref = copy.deepcopy(node.get("media_ref") or {})
        title = node.get("title", "")
        if title:
            ref["label"] = title
        resolved = self._resolved_urls.get(item_id) or node.get("resolved_url", "")
        if resolved:
            ref["file_path"] = resolved
        framing = image_transform_to_record(image_transform_from_record(node.get("image_framing")))
        self.projectRequested.emit(
            meeting_media_from_ref(
                ref,
                image_framing=framing,
                start_trim_ticks=self._trim_ticks(node, "start_trim_ticks"),
                end_trim_ticks=self._trim_ticks(node, "end_trim_ticks"),
                base_duration_ticks=self._trim_ticks(node, "base_duration_ticks"),
                origin_kind="meeting",
                origin_container_id=self._tree_key,
                origin_item_id=item_id,
            )
        )

    @Slot(str)
    def addToDestination(self, item_id: str) -> None:  # noqa: N802 - QML API
        if not self._flush_image_framing_save():
            return
        node = self._find_node(item_id)
        if not node or node.get("type") != "media":
            return
        state = self._media_tree_runtime.registry.state(
            self._tree_session.owner_id,
            item_id,
        )
        if state.availability != MediaAvailability.AVAILABLE:
            return
        url = self._url_for_node(node)
        if not url:
            return
        item = playlist_item_from_meeting_node(node, url=url)
        request = create_media_destination_request(item)
        if request is None:
            return
        self.addToDestinationRequested.emit(request)

    @Slot(str)
    def setAsIdle(self, item_id: str) -> None:  # noqa: N802 - QML API
        if not self._flush_image_framing_save():
            return
        node = self._find_node(item_id)
        if not node or node.get("type") != "media":
            return
        state = self._media_tree_runtime.registry.state(
            self._tree_session.owner_id,
            item_id,
        )
        if state.availability != MediaAvailability.AVAILABLE:
            return
        ref = node.get("media_ref") or {}
        media_type = str(
            node.get("media_type") or self._media_type_from_ref(ref)
        ).strip().lower()
        source = existing_idle_media_path(media_type, self._url_for_node(node))
        if not source:
            return
        thumbnail_path = ""
        if media_type == "video":
            thumbnail_path = self._thumbnail_local_path(node, item_id, source)
        request = create_idle_media_request(
            title=str(node.get("title") or media_ref_title(ref)),
            media_type=media_type,
            source=source,
            thumbnail_path=thumbnail_path,
        )
        if request is None:
            return
        self.setAsIdleRequested.emit(request)

    @Slot(result=float)
    def imageFramingAspectRatio(self) -> float:  # noqa: N802 - QML API
        try:
            ratio = self._projection_aspect_ratio_provider()
        except Exception:  # noqa: BLE001 - defensive UI provider boundary
            ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        if not isinstance(ratio, ProjectionAspectRatio):
            ratio = DEFAULT_PROJECTION_ASPECT_RATIO
        return ratio.value

    @Slot(str, result=float)
    def imageFramingSourceAspectRatio(self, item_id: str) -> float:  # noqa: N802
        return float(
            self._media_tree_runtime.registry.state(
                self._tree_session.owner_id,
                item_id,
            ).image_aspect_ratio
        )

    @Slot(str, float, float, float, float, float, bool)
    def setImageFraming(  # noqa: N802 - QML API
        self,
        item_id: str,
        zoom: float,
        norm_x: float,
        norm_y: float,
        source_width: float,
        source_height: float,
        snap_zoom_to_cover: bool,
    ) -> None:
        node = self._find_node(item_id)
        if not node or node.get("type") != "media":
            return
        ref = node.get("media_ref") or {}
        media_type = node.get("media_type") or self._media_type_from_ref(ref)
        if media_type != "image":
            return
        source_aspect_ratio = self.imageFramingSourceAspectRatio(item_id)
        if source_aspect_ratio > 0.0:
            source_width = source_aspect_ratio
            source_height = 1.0
        current_transform = image_transform_from_record(node.get("image_framing"))
        transform = prepare_image_transform_for_aspect(
            source_width,
            source_height,
            self.imageFramingAspectRatio(),
            current_transform,
            ImageTransform(zoom, norm_x, norm_y),
            snap_zoom_to_cover=snap_zoom_to_cover,
        )
        record = image_transform_to_record(transform)
        current = image_transform_to_record(current_transform)
        if record == current:
            return
        if record is None:
            node.pop("image_framing", None)
        else:
            node["image_framing"] = record
        _emit_controller_state_changed(self)
        self._schedule_image_framing_save()

    @Slot(str)
    def resetImageFraming(self, item_id: str) -> None:  # noqa: N802 - QML API
        node = self._find_node(item_id)
        if not node or "image_framing" not in node:
            return
        node.pop("image_framing", None)
        _emit_controller_state_changed(self)
        self._schedule_image_framing_save()

    @Slot(str, float, float, float)
    def setMediaTrim(  # noqa: N802 - QML API
        self,
        item_id: str,
        start_ms: float,
        end_ms: float,
        duration_ms: float,
    ) -> None:
        node = self._find_node(item_id)
        if not node or node.get("type") != "media":
            return
        ref = node.get("media_ref") or {}
        media_type = node.get("media_type") or self._media_type_from_ref(ref)
        if media_type not in {"audio", "video"}:
            return
        try:
            trim = MediaTrim.from_millisecond_bounds(start_ms, end_ms, duration_ms)
        except (TypeError, ValueError):
            return
        values = {
            "start_trim_ticks": trim.start_trim_ticks,
            "end_trim_ticks": trim.end_trim_ticks,
            "base_duration_ticks": trim.base_duration_ticks,
        }
        for field, value in values.items():
            ref.pop(field, None)
            if field != "base_duration_ticks" and value == 0:
                node.pop(field, None)
            else:
                node[field] = value
        self.stateChanged.emit()
        self._save()

    def _schedule_image_framing_save(self) -> None:
        self._image_framing_save_pending = True
        self._image_framing_save_timer.start()

    def _flush_image_framing_save(self) -> bool:
        if not getattr(self, "_image_framing_save_pending", False):
            return True
        timer = getattr(self, "_image_framing_save_timer", None)
        if timer is not None:
            timer.stop()
        return bool(self._save())

    @Slot(str)
    def removeItem(self, item_id: str):
        if self._tree_session.remove_pending_node(item_id):
            return
        node = self._find_node(item_id)
        if not node:
            return
        self._cancel_media_info_requests_for_item(item_id)
        MeetingTreeController._remember_hidden_canonical_media(self, node)
        # Official meeting media is recoverable canonical content. Its linked
        # copy must remain available. A manual item only owns a source file when
        # that file was imported directly by the meeting-folder workflow.
        linked_source = node.get("linked_folder_source", "")
        if linked_source and not node.get("meeting_generated"):
            file_path = self._url_for_node(node)
            if MeetingTreeController._is_direct_meeting_folder_media(
                self,
                item_id,
                file_path,
            ):
                self._queue_linked_media_removal(node, file_path, linked_source)
                return
            self._linked_folder_files.pop(file_path, None)
            self._cleanup_meeting_folder_import_for_removed_node(
                item_id,
                file_path,
                source_removed=False,
            )
        self._remember_deleted_sources(node, include_media=True)
        if self._replace_node(item_id, []):
            self._save_and_emit_replace(item_id, [])

    def _queue_linked_media_removal(
        self,
        node: Node,
        file_path: str,
        linked_source: str,
    ) -> None:
        item_id = str(node.get("id") or "")
        owner_id = self._tree_session.owner_id
        if not item_id or not owner_id:
            return
        operation_id = f"meeting-remove:{uuid.uuid4().hex}"
        stage = _tr("MediaDestinationDialog", "Preparing media")

        def run(report, _cancellation):
            report(MediaOperationProgress(MediaOperationState.FINALIZING, stage=stage))
            return self._watched_folder_file_store.remove_file_inside(
                file_path,
                linked_source,
            )

        def commit(value: object) -> None:
            if not isinstance(value, bool):
                raise TypeError("Meeting media removal returned an invalid result")
            current = self._find_node(item_id)
            if current is None:
                return
            self._linked_folder_files.pop(file_path, None)
            self._cleanup_meeting_folder_import_for_removed_node(
                item_id,
                file_path,
                source_removed=value,
            )
            self._remember_deleted_sources(current, include_media=True)
            if self._replace_node(item_id, []):
                self._save_and_emit_replace(item_id, [])

        self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=owner_id,
                subject_id=item_id,
                operation_type="meeting_linked_media_remove",
                conflict_key=child_folder_resource_claim(linked_source),
                presentation=MediaOperationPresentation.TREE_LOCAL,
                runner=run,
                commit=commit,
                priority=100,
                initial_stage=stage,
                retryable=True,
            )
        )

    def _is_direct_meeting_folder_media(self, node_id: str, file_path: str) -> bool:
        if node_id in MeetingTreeController._meeting_folder_imported_node_ids(self):
            return True
        for record in getattr(self, "_meeting_folder_imports", {}).values():
            if not isinstance(record, dict):
                continue
            source_path = str(record.get("path") or "")
            if (
                record.get("kind") == "media"
                and file_path
                and source_path
                and same_local_source(source_path, file_path)
            ):
                return True
        return False

    def _remember_hidden_canonical_media(self, node: Node) -> None:
        source_key = str(node.get("meeting_source_key") or "")
        if node.get("type") == "media" and node.get("meeting_generated") and source_key:
            hidden_media = getattr(self, "_hidden_canonical_media", None)
            if hidden_media is None:
                hidden_media = {}
                self._hidden_canonical_media = hidden_media
            hidden_media[source_key] = copy.deepcopy(node)

    def _meeting_folder_imported_node_ids(self) -> set[str]:
        result: set[str] = set()
        for record in getattr(self, "_meeting_folder_imports", {}).values():
            if not isinstance(record, dict):
                continue
            node_ids = record.get("node_ids", [])
            if not isinstance(node_ids, list):
                continue
            result.update(str(node_id) for node_id in node_ids if node_id)
        return result

    def _cleanup_meeting_folder_import_for_removed_node(
        self,
        node_id: str,
        file_path: str,
        *,
        source_removed: bool,
    ) -> None:
        if not node_id and not file_path:
            return
        for key, record in list(self._meeting_folder_imports.items()):
            if not isinstance(record, dict):
                continue
            record_path = str(record.get("path") or "")
            node_ids = [str(value) for value in record.get("node_ids", []) if value]
            contains_node = bool(node_id and node_id in node_ids)
            same_source_file = bool(
                file_path and record_path and same_local_source(record_path, file_path)
            )
            source_is_gone = bool(record_path and same_source_file and source_removed)
            if source_is_gone:
                self._meeting_folder_imports.pop(key, None)
                continue
            if contains_node:
                remaining = [value for value in node_ids if value != node_id]
                if remaining != node_ids:
                    record["node_ids"] = remaining

    @Slot(str)
    def renameItem(self, item_id: str):
        node = self._find_node(item_id)
        if not node or node.get("type") != "media":
            return
        dlg = NameDialog(node.get("title", ""), lang=True, parent=self.parent())
        dlg.setWindowTitle(_tr(_PLAYLIST_EDIT_CONTEXT, "Rename media"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        node["title"] = name
        node["user_title_override"] = True
        ref = node.setdefault("media_ref", {})
        ref["label"] = name
        self._save()
        _emit_controller_state_changed(self)

    @Slot(str)
    def downloadItem(self, item_id: str):
        url = self._url_for_node_id(item_id)
        if not url:
            return
        mgr = self._media_cache_manager
        state = self._media_tree_runtime.registry.state(
            self._tree_session.owner_id,
            item_id,
        )
        if not state.cached and not mgr.is_prefetching(url):
            mgr.prefetch(url, priority=True)
            self._emit_cloud_for_node(item_id)

    @Slot(str, str)
    def renameMarker(self, marker_id: str, text: str):
        node = self._find_node(marker_id)
        if not node or node.get("type") != "marker":
            return
        node["text"] = text
        node["user_title_override"] = True
        self._save()
        _emit_controller_state_changed(self)

    @Slot(str)
    def deleteMarker(self, marker_id: str):
        node = self._find_node(marker_id)
        if node:
            self._remember_deleted_sources(node, include_media=False)
        if self._replace_node(marker_id, []):
            self._save_and_emit_replace(marker_id, [])

    @Slot(str)
    def renameSection(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        canonical = MeetingTreeController._canonical_counterpart(self, node)
        initial_title = display_meeting_section_title(node)
        label = (
            _tr(_PLAYLIST_EDIT_CONTEXT, "Subsection name:")
            if node.get("type") == "subsection"
            else _tr(_PLAYLIST_EDIT_CONTEXT, "Section name:")
        )
        dlg = NameDialog(initial_title, lang=True, parent=self.parent(), label=label)
        dlg.setWindowTitle(_tr(_PLAYLIST_EDIT_CONTEXT, "Rename section"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        canonical_title = display_meeting_section_title(canonical) if canonical is not None else ""
        if canonical is not None and name == canonical_title:
            canonical_value = str(canonical.get("title") or "")
            if str(node.get("title") or "") == canonical_value and not node.get(
                "user_title_override"
            ):
                return
            node["title"] = canonical_value
            node.pop("user_title_override", None)
        else:
            if name == initial_title:
                return
            node["title"] = name
            node["user_title_override"] = True
        self._save()
        self._emit_section_changed(node)

    @Slot(str)
    def deleteSection(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        has_manual_structure = any(
            not child.get("meeting_generated") and child.get("type") != "media"
            for child in iter_nodes(node.get("children", []))
        )
        if node.get("meeting_generated") and has_manual_structure:
            QMessageBox.warning(
                self.parent(),
                _tr(
                    _PLAYLIST_EDIT_CONTEXT,
                    _CANNOT_DELETE_OFFICIAL_SECTION_SOURCE,
                ),
                _tr(
                    _PLAYLIST_EDIT_CONTEXT,
                    _MOVE_MANUAL_STRUCTURE_SOURCE,
                ),
            )
            return
        reply = QMessageBox.question(
            self.parent(),
            _tr(_PLAYLIST_EDIT_CONTEXT, "Delete section"),
            _tr(
                _PLAYLIST_EDIT_CONTEXT, 'Delete section "{name}"?\nItems inside will be kept.'
            ).replace("{name}", display_meeting_section_title(node)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._remember_deleted_sources(node, include_media=False)
        replacement = self._media_descendants(node)
        if self._replace_node(section_id, replacement):
            self._save_and_emit_replace(section_id, replacement)

    @Slot(str)
    def recolorSection(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        dlg = HuePickerDialog(int(node.get("color_hue", 215)), parent=self.parent())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        node["color_hue"] = dlg.selected_hue()
        self._save()
        self._emit_section_changed(node)

    @Slot(str)
    def toggleCollapse(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        node["collapsed"] = not bool(node.get("collapsed", False))
        self._save()
        _emit_controller_state_changed(self)

    @Slot()
    def pointerEnter(self):  # noqa: N802
        self.pointerEntered.emit()

    @Slot(str, int)
    def pointerCursorEnter(  # noqa: N802
        self,
        cursor_source: str,
        cursor_shape: int,
    ):
        self.pointerCursorEntered.emit(cursor_source, cursor_shape)

    @Slot(str, int)
    def pointerCursorChange(  # noqa: N802
        self,
        cursor_source: str,
        cursor_shape: int,
    ):
        self.pointerCursorChanged.emit(cursor_source, cursor_shape)

    @Slot(str)
    def pointerCursorExit(self, cursor_source: str):  # noqa: N802
        self.pointerCursorExited.emit(cursor_source)

    @Slot()
    def pointerExit(self):  # noqa: N802
        self.pointerExited.emit()

    @Slot(str, str, str, result=bool)
    def canDrop(self, node_id: str, node_type: str, target_list_id: str) -> bool:
        return can_drop_tree_node(node_type, target_list_id)

    @Slot(result=int)
    def treeStructureRevision(self) -> int:  # noqa: N802
        return self._tree_session.structure_revision

    @Slot(result=str)
    def treeIdentity(self) -> str:  # noqa: N802
        return self._tree_session.tree_id

    @Slot(str, str, int, str, int, result=bool)
    def moveNode(
        self,
        node_id: str,
        target_list_id: str,
        insert_index: int,
        expected_tree_id: str,
        expected_structure_revision: int,
    ) -> bool:
        if (
            expected_tree_id != self._tree_session.tree_id
            or expected_structure_revision != self._tree_session.structure_revision
        ):
            return False
        if not move_tree_node(self._nodes, node_id, target_list_id, insert_index):
            return False
        self._save()
        self.stateChanged.emit()
        self._emit_section_counts()
        return True

    def _connect_services(self) -> None:
        self._svc.media_resolved.connect(self._on_media_resolved)
        mgr = self._media_cache_manager
        mgr.cache_changed.connect(self._on_cache_changed)
        mgr.cache_removed.connect(self._on_cache_removed)
        mgr.prefetch_progress.connect(self._on_prefetch_progress)
        mgr.prefetch_error.connect(self._on_prefetch_error)

    def _save(self) -> bool:
        self._image_framing_save_timer.stop()
        self._image_framing_save_pending = False
        MeetingTreeController._prune_deleted_source_keys(self)
        MeetingTreeController._refresh_canonical_restore_state(self)
        if not self._tree_key:
            return True
        if self._sync_enabled:
            self._schedule_sync_manifest_save()
        return self._save_local_cache()

    def _save_local_cache(self) -> bool:
        MeetingTreeController._cancel_local_snapshot(self)
        if not self._tree_key:
            return True
        tree_key = self._tree_key
        nodes = clone_nodes(self._nodes)
        canonical_hash = self._canonical_hash
        deleted = set(self._deleted_source_keys)
        linked = dict(self._linked_folder_files) or None
        imports = copy.deepcopy(self._meeting_folder_imports) or None
        overview = self._current_overview()
        canonical_nodes = clone_nodes(getattr(self, "_canonical_nodes", []))
        canonical_generation = getattr(self, "_canonical_reset_generation", 0)
        hidden_media = copy.deepcopy(getattr(self, "_hidden_canonical_media", {}))
        storage_key = self._snapshot_storage_key(tree_key)

        def persist_snapshot() -> None:
            self._store.save(
                tree_key,
                nodes,
                canonical_hash,
                deleted,
                linked,
                imports,
                overview,
                canonical_nodes=canonical_nodes,
                canonical_reset_generation=canonical_generation,
                hidden_canonical_media=hidden_media,
            )

        generation = self._media_tree_runtime.snapshots.request(
            storage_key,
            persist_snapshot,
        )
        if generation <= 0:
            return False
        derived_timer = getattr(self, "_derived_media_save_timer", None)
        if derived_timer is not None:
            derived_timer.stop()
        derived_patches = getattr(self, "_derived_media_patches", None)
        if derived_patches is not None:
            derived_patches.clear()
        return True

    def _snapshot_storage_key(self, tree_key: str | None = None) -> str:
        return f"meeting:{self._store.path}:{tree_key or self._tree_key}"

    @Slot(str, int)
    def _on_snapshot_write_completed(self, key: str, _generation: int) -> None:
        if key == self._snapshot_storage_key():
            self.storageSaved.emit(self._tree_key)

    @Slot(str, int, str)
    def _on_snapshot_write_failed(
        self,
        key: str,
        _generation: int,
        message: str,
    ) -> None:
        if key == self._snapshot_storage_key():
            self.storageSaveFailed.emit(self._tree_key, message)

    def _current_overview(self) -> MeetingTreeOverview | None:
        if self._overview is None:
            return None
        return MeetingTreeOverview(
            title=self._overview.title,
            media_count=count_media(self._nodes),
            cover_bytes=self._overview.cover_bytes,
        )

    def _schedule_sync_manifest_save(self, *, delay_ms: int = _SYNC_SAVE_DEBOUNCE_MS) -> None:
        if not self._sync_identity or not self._sync_folder:
            return
        folder = Path(self._sync_folder)
        key = os.path.normcase(os.path.abspath(folder))
        now = time.monotonic()
        previous = self._pending_sync_saves.get(key)
        self._pending_sync_saves[key] = _PendingSyncSave(
            folder=folder,
            identity=self._sync_identity,
            nodes=clone_nodes(self._nodes),
            deleted_source_keys=set(self._deleted_source_keys),
            linked_folder_files=dict(self._linked_folder_files),
            meeting_folder_imports=copy.deepcopy(self._meeting_folder_imports),
            canonical_reset_generation=getattr(
                self,
                "_canonical_reset_generation",
                0,
            ),
            hidden_canonical_media=copy.deepcopy(getattr(self, "_hidden_canonical_media", {})),
            expected_revision=self._sync_revision,
            next_attempt_at=now + max(0, delay_ms) / 1000,
            generation=(previous.generation + 1) if previous else 1,
            first_attempt_at=previous.first_attempt_at if previous else None,
            retry_index=previous.retry_index if previous else 0,
        )
        self._arm_sync_save_timer()
        self.syncStateChanged.emit()

    def _arm_sync_save_timer(self) -> None:
        if self._sync_save_inflight is not None:
            self._sync_save_timer.stop()
            return
        if not self._pending_sync_saves:
            self._sync_save_timer.stop()
            return
        next_attempt = min(request.next_attempt_at for request in self._pending_sync_saves.values())
        delay_ms = max(0, int((next_attempt - time.monotonic()) * 1000))
        self._sync_save_timer.start(delay_ms)

    def _save_sync_manifest(self) -> None:
        """Immediately enqueue the latest state and run one due transaction."""

        self._schedule_sync_manifest_save(delay_ms=0)
        self._drain_sync_manifest_saves()

    def _drain_sync_manifest_saves(self) -> None:
        if self._sync_save_inflight is not None:
            return
        if not self._pending_sync_saves:
            self.syncStateChanged.emit()
            return
        now = time.monotonic()
        key, request = min(
            self._pending_sync_saves.items(),
            key=lambda item: item[1].next_attempt_at,
        )
        if request.next_attempt_at > now:
            self._arm_sync_save_timer()
            return
        if request.first_attempt_at is None:
            request.first_attempt_at = now
        generation = request.generation
        self._sync_save_inflight = (key, generation)
        self._sync_save_timer.stop()
        future = self._sync_save_executor.submit(
            self._media_tree_runtime.resource_lanes.run,
            child_folder_resource_claim(request.folder),
            lambda: self._sync_service.save_tree(
                request.folder,
                request.identity,
                nodes=request.nodes,
                deleted_source_keys=request.deleted_source_keys,
                linked_folder_files=request.linked_folder_files,
                meeting_folder_imports=request.meeting_folder_imports,
                expected_revision=request.expected_revision,
                canonical_reset_generation=request.canonical_reset_generation,
                hidden_canonical_media=request.hidden_canonical_media,
            ),
        )
        self._sync_save_future = future
        future.add_done_callback(
            lambda completed, save_key=key, save_generation=generation: (
                self._emit_sync_save_completed(
                    save_key,
                    save_generation,
                    completed,
                )
            )
        )

    def _emit_sync_save_completed(
        self,
        key: str,
        generation: int,
        future: Future[MeetingSyncRecord],
    ) -> None:
        try:
            record = future.result()
        except BaseException as exc:  # noqa: BLE001 - worker-to-Qt exception boundary
            self._syncSaveCompleted.emit(key, generation, None, exc)
        else:
            self._syncSaveCompleted.emit(key, generation, record, None)

    @Slot(str, int, object, object)
    def _on_sync_save_completed(
        self,
        key: str,
        generation: int,
        saved_record: MeetingSyncRecord | None,
        error: BaseException | None,
    ) -> None:
        self._sync_save_inflight = None
        self._sync_save_future = None
        request = self._pending_sync_saves.get(key)
        if request is None:
            self._arm_sync_save_timer()
            return

        now = time.monotonic()
        is_latest = request.generation == generation
        if error is None and saved_record is not None:
            if is_latest:
                self._pending_sync_saves.pop(key, None)
            else:
                request.expected_revision = saved_record.revision
                request.next_attempt_at = now
            current_key = os.path.normcase(os.path.abspath(self._sync_folder))
            if is_latest and key == current_key and self._sync_identity == request.identity:
                pending_framing = MeetingTreeController._pending_image_framing_state(self)
                self._apply_sync_record(saved_record)
                MeetingTreeController._restore_pending_image_framing(
                    self,
                    pending_framing,
                )
                self._save_local_cache()
            elif key == current_key:
                self._sync_revision = saved_record.revision
        elif not is_latest:
            request.next_attempt_at = now
        elif isinstance(error, ManifestWriteError):
            elapsed = now - (request.first_attempt_at or now)
            if error.retryable and elapsed < _SYNC_SAVE_RETRY_BUDGET_SECONDS:
                delay = _SYNC_SAVE_RETRY_DELAYS_MS[
                    min(request.retry_index, len(_SYNC_SAVE_RETRY_DELAYS_MS) - 1)
                ]
                request.retry_index += 1
                request.next_attempt_at = now + (delay * random.uniform(0.8, 1.2)) / 1000
                log.warning(
                    "Meeting manifest publish is busy; retry %d in %d ms: %s",
                    request.retry_index,
                    delay,
                    error.path,
                )
            else:
                if self._notify_sync_save_failure(error, request):
                    request.first_attempt_at = None
                    request.retry_index = 0
                    request.next_attempt_at = now
                else:
                    self._pending_sync_saves.pop(key, None)
        else:
            assert error is not None
            if self._notify_sync_save_failure(error, request):
                request.first_attempt_at = None
                request.retry_index = 0
                request.next_attempt_at = now
            else:
                self._pending_sync_saves.pop(key, None)

        self._arm_sync_save_timer()
        self.syncStateChanged.emit()

    def _pending_image_framing_state(
        self,
    ) -> dict[str, tuple[tuple, dict[str, Any]]]:
        if not getattr(self, "_image_framing_save_pending", False):
            return {}
        state: dict[str, tuple[tuple, dict[str, Any]]] = {}
        for node in iter_nodes(self._nodes):
            node_id = str(node.get("id") or "")
            framing = node.get("image_framing")
            if node_id and isinstance(framing, dict):
                state[node_id] = (
                    media_identity_signature(node),
                    copy.deepcopy(framing),
                )
        return state

    def _restore_pending_image_framing(
        self,
        state: dict[str, tuple[tuple, dict[str, Any]]],
    ) -> None:
        if not state:
            return
        for node in iter_nodes(self._nodes):
            entry = state.get(str(node.get("id") or ""))
            if entry is None or media_identity_signature(node) != entry[0]:
                continue
            node["image_framing"] = copy.deepcopy(entry[1])

    def _notify_sync_save_failure(
        self,
        error: BaseException,
        request: _PendingSyncSave,
    ) -> bool:
        log.error(
            "Could not save meeting sync manifest",
            exc_info=(type(error), error, error.__traceback__),
        )
        return bool(
            self._warn_sync_failed(
                str(error),
                folder_path=os.fspath(request.folder),
            )
        )

    def _pause_sync_after_save_failure(self, message: str) -> None:
        self._sync_enabled = False
        self._sync_revision = 0
        self.syncStateChanged.emit()
        self._warn_sync_failed(message)

    def _jwpub_image_dir(self) -> str:
        if self._sync_enabled and self._sync_folder:
            return str(cache_dir(Path(self._sync_folder)))
        return os.fspath(self._profile_paths.images_dir)

    def _embedded_media_dir(self) -> str:
        if self._sync_enabled and self._sync_folder:
            return str(cache_dir(Path(self._sync_folder)))
        return os.fspath(self._profile_paths.embedded_dir)

    def _generated_asset_roots(self) -> tuple[str, ...]:
        return tuple(
            root
            for root in (
                os.fspath(self._runtime_paths.cache_dir),
                os.fspath(self._profile_paths.images_dir),
                os.fspath(self._profile_paths.embedded_dir),
                os.fspath(self._meeting_thumbnail_store.root),
            )
            if root
        )

    def _manual_media_node(self, path: str, title: str = "") -> Node:
        return create_manual_media_node(path, title)

    def _insert_nodes(
        self,
        list_id: str,
        insert_index: int,
        nodes: list[Node],
        *,
        signal_name: str = "media",
    ) -> bool:
        kind, target_id = self._parse_list_id(list_id)
        target_children = self._children_for_target(kind, target_id)
        if target_children is None:
            return False
        index = max(0, min(insert_index, len(target_children)))
        if self._sync_enabled and self._sync_folder and nodes:
            return self._queue_nodes_for_sync(
                list_id,
                index,
                nodes,
                signal_name=signal_name,
            )
        return self._commit_inserted_nodes(
            list_id,
            index,
            nodes,
            signal_name=signal_name,
        )

    def _queue_nodes_for_sync(
        self,
        list_id: str,
        insert_index: int,
        nodes: list[Node],
        *,
        signal_name: str,
    ) -> bool:
        owner_id = self._tree_session.owner_id
        sync_folder = self._sync_folder
        tree_key = self._tree_key
        session_generation = self._tree_session.generation
        if not owner_id or not sync_folder or not tree_key:
            return False
        operation_id = f"meeting-copy:{uuid.uuid4().hex}"
        pending_nodes = clone_nodes(nodes)
        stage = _tr("MediaDestinationDialog", "Preparing media")
        self._tree_session.add_pending(
            operation_id,
            pending_nodes,
            target_list_id=list_id,
            insert_index=insert_index,
        )

        def run(report, cancellation):
            report(
                MediaOperationProgress(
                    state=MediaOperationState.COPYING,
                    stage=stage,
                )
            )
            created_paths: list[Path] = []
            prepared, linked_files = self._sync_service.materialize_tree_files(
                pending_nodes,
                Path(sync_folder),
                generated_roots=self._generated_asset_roots(),
                cancellation=cancellation,
                created_paths_out=created_paths,
            )
            if cancellation.is_set():
                self._sync_service.rollback_materialized_files(created_paths)
                raise MediaOperationCancelled("Meeting media copy cancelled")
            return _PreparedMeetingNodes(
                prepared,
                linked_files,
                tuple(created_paths),
            )

        def commit(value: object) -> None:
            if not isinstance(value, _PreparedMeetingNodes):
                raise TypeError("Meeting media preparation returned an invalid result")
            if (
                self._tree_key != tree_key
                or self._sync_folder != sync_folder
                or self._tree_session.generation != session_generation
            ):
                discard(value)
                return
            self._linked_folder_files.update(value.linked_files)
            if not self._commit_inserted_nodes(
                list_id,
                insert_index,
                value.nodes,
                signal_name=signal_name,
            ):
                raise RuntimeError("Meeting insertion target is no longer available")
            self._tree_session.remove_pending(operation_id)

        def discard(value: object | None) -> None:
            if not isinstance(value, _PreparedMeetingNodes):
                return
            self._media_tree_runtime.schedule_artifact_cleanup(
                value.created_paths,
                conflict_key=child_folder_resource_claim(sync_folder),
            )

        submitted = self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=owner_id,
                operation_type="meeting_media_copy",
                conflict_key=child_folder_resource_claim(sync_folder),
                presentation=MediaOperationPresentation.TREE_LOCAL,
                runner=run,
                commit=commit,
                priority=100,
                initial_stage=stage,
                retryable=True,
                discarded=discard,
            )
        )
        if not submitted:
            self._tree_session.remove_pending(operation_id)
        return submitted

    def _commit_inserted_nodes(
        self,
        list_id: str,
        insert_index: int,
        nodes: list[Node],
        *,
        signal_name: str,
    ) -> bool:
        kind, target_id = self._parse_list_id(list_id)
        target_children = self._children_for_target(kind, target_id)
        if target_children is None:
            return False
        index = max(0, min(insert_index, len(target_children)))
        for offset, node in enumerate(nodes):
            target_children.insert(index + offset, node)
        saved = self._save()
        self._start_media_requests(nodes)
        self._emit_section_counts()
        return saved

    def _children_for_target(self, kind: str, node_id: str) -> list[Node] | None:
        return children_for_tree_target(self._nodes, kind, node_id)

    def _parse_list_id(self, list_id: str) -> tuple[str, str]:
        return parse_tree_list_id(list_id)

    def _find_node(self, node_id: str, nodes: list[Node] | None = None) -> Node | None:
        return find_tree_node(self._nodes, node_id, nodes)

    def _canonical_counterpart(self, node: Node) -> Node | None:
        source_key = str(node.get("meeting_source_key") or "")
        if not node.get("meeting_generated") or not source_key:
            return None
        return next(
            (
                candidate
                for candidate in iter_nodes(getattr(self, "_canonical_nodes", []))
                if candidate.get("meeting_generated")
                and str(candidate.get("meeting_source_key") or "") == source_key
            ),
            None,
        )

    def _replace_node(self, node_id: str, replacement: list[Node]) -> bool:
        return replace_tree_node(self._nodes, node_id, replacement)

    def _media_descendants(self, node: Node) -> list[Node]:
        return media_descendants(node)

    def _save_and_emit_replace(self, node_id: str, replacement: list[Node]) -> None:
        self._save()
        self._emit_section_counts()

    def _emit_section_changed(self, node: Node | None) -> None:
        if not node or node.get("type") not in ("section", "subsection"):
            return
        _emit_controller_state_changed(self)

    def _remember_deleted_sources(self, node: Node, *, include_media: bool) -> None:
        if node.get("meeting_generated"):
            key = node.get("meeting_source_key")
            if key and (include_media or node.get("type") != "media"):
                self._deleted_source_keys.add(str(key))
        for child in node.get("children", []):
            self._remember_deleted_sources(child, include_media=include_media)

    def _badge_for(self, _ref: dict[str, Any], media_type: str) -> str:
        if media_type == "image":
            return _tr("PlaylistPanel", "Image")
        if media_type == "audio":
            return _tr("PlaylistPanel", "Audio")
        return _tr("PlaylistPanel", "Video")

    def _media_type_from_ref(self, ref: dict[str, Any]) -> str:
        mime = str(ref.get("mime_type", "")).lower()
        if "image" in mime:
            return "image"
        if "audio" in mime:
            return "audio"
        return "video"

    @staticmethod
    def _trim_ticks(node: Node, field: str) -> int:
        try:
            return max(
                0,
                int(
                    node.get(field)
                    if node.get(field) is not None
                    else (node.get("media_ref") or {}).get(field, 0)
                ),
            )
        except (TypeError, ValueError):
            return 0

    def _duration_ticks(self, node: Node | None) -> int:
        if not node:
            return 0
        try:
            return int(
                node.get("base_duration_ticks")
                or (node.get("media_ref") or {}).get("base_duration_ticks")
                or 0
            )
        except (TypeError, ValueError):
            return 0

    def _thumbnail_local_path(
        self,
        node: Node | None,
        item_id: str = "",
        source: str = "",
    ) -> str:
        item_id = item_id or str((node or {}).get("id", ""))
        source_key = self._thumbnail_provenance(node, source)
        storage_id = self._thumbnail_storage_id(node, item_id, source_key)
        stored = str((node or {}).get("thumbnail_local_path") or "")
        stored_source_key = str((node or {}).get("thumbnail_source_key") or "")
        if not storage_id:
            return stored
        if stored and (
            stored_source_key == thumbnail_source_fingerprint(source_key)
            and Path(stored).name == self._meeting_thumbnail_store.path(storage_id).name
        ):
            return stored
        return os.fspath(self._meeting_thumbnail_store.path(storage_id))

    def _thumbnail_storage_id(
        self,
        node: Node | None,
        item_id: str = "",
        source: str = "",
    ) -> str:
        item_id = item_id or str((node or {}).get("id", ""))
        base_id = (
            meeting_thumb_storage_id(self._tree_key, item_id)
            if item_id and (node or {}).get("meeting_generated")
            else item_id
        )
        return thumbnail_storage_id(base_id, source or self._thumbnail_provenance(node))

    def _thumbnail_provenance(self, node: Node | None, source: str = "") -> str:
        return str((node or {}).get("thumbnail_url") or source or self._url_for_node(node))

    def _save_thumbnail_for_node(
        self,
        node: Node,
        pixmap: QPixmap,
        source_signature: str,
    ) -> str:
        item_id = str(node.get("id", ""))
        if not item_id or pixmap is None or pixmap.isNull():
            return ""
        source_key = self._thumbnail_provenance(node)
        storage_id = self._thumbnail_storage_id(node, item_id, source_key)
        path = self._meeting_thumbnail_store.path(storage_id)
        node["thumbnail_cache_key"] = path.name
        node["thumbnail_local_path"] = os.fspath(path)
        node["thumbnail_source_key"] = thumbnail_source_fingerprint(source_key)
        self._media_tree_runtime.thumbnails.save_image(
            owner_id=self._tree_session.owner_id,
            node_id=item_id,
            storage_id=storage_id,
            store=self._meeting_thumbnail_store,
            image=pixmap.toImage(),
            source_signature=source_signature,
        )
        return os.fspath(path)

    @staticmethod
    def _bounded_thumbnail(pixmap: QPixmap) -> QPixmap:
        if pixmap.width() <= 400 and pixmap.height() <= 225:
            return pixmap
        return pixmap.scaled(
            400,
            225,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _url_for_node_id(self, node_id: str) -> str:
        node = self._find_node(node_id)
        return self._url_for_node(node) if node else ""

    def _url_for_node(self, node: Node | None) -> str:
        if not node:
            return ""
        item_id = node.get("id", "")
        if item_id in self._resolved_urls:
            return self._resolved_urls[item_id]
        if node.get("resolved_url"):
            return str(node.get("resolved_url"))
        ref = node.get("media_ref") or {}
        return str(ref.get("file_path") or "")

    def _start_media_requests(
        self,
        nodes: list[Node] | None = None,
        *,
        resolve_jw_metadata: bool = True,
    ) -> None:
        media_nodes = [
            node
            for node in iter_nodes(self._nodes if nodes is None else nodes)
            if node.get("type") == "media"
        ]
        probe_ids: set[str] = set()
        for node in media_nodes:
            source = self._url_for_node(node)
            if source:
                item_id = str(node.get("id") or "")
                if item_id:
                    probe_ids.add(item_id)
            if (
                resolve_jw_metadata
                and node.get("meeting_generated")
                and MeetingTreeController._node_needs_jw_metadata(
                    self,
                    node,
                    source=source,
                )
            ):
                self._request_jw_resolution(
                    node,
                    allow_generated=bool(node.get("meeting_generated")),
                )
        self._tree_session.request_nodes(probe_ids)

    def _node_needs_jw_metadata(self, node: Node, *, source: str = "") -> bool:
        if node.get("type") != "media":
            return False
        ref = node.get("media_ref") or {}
        if not isinstance(ref, dict) or not (ref.get("key_symbol") or ref.get("meps_doc_id")):
            return False
        media_type = str(node.get("media_type") or "").lower()
        if not media_type:
            mime_type = str(ref.get("mime_type") or "").lower()
            if mime_type.startswith("image/"):
                media_type = "image"
            elif mime_type.startswith("audio/"):
                media_type = "audio"
            else:
                media_type = "video"
        if media_type not in {"audio", "video"}:
            return False
        try:
            duration_ticks = int(
                node.get("base_duration_ticks") or ref.get("base_duration_ticks") or 0
            )
        except (TypeError, ValueError, OverflowError):
            duration_ticks = 0
        needs_duration = media_type in {"audio", "video"} and duration_ticks <= 0
        needs_thumbnail = not (node.get("thumbnail_url") or node.get("thumbnail_local_path"))
        return bool(
            not (source or node.get("resolved_url") or ref.get("file_path"))
            or node.get("auto_title")
            or needs_duration
            or needs_thumbnail
        )

    def _resolve_unresolved_canonical_media(self, nodes: list[Node]) -> None:
        resolve_to_node_id = getattr(self, "_resolve_to_node_id", None)
        service = getattr(self, "_svc", None)
        resolve = getattr(service, "resolve_media_async", None)
        if resolve_to_node_id is None or not callable(resolve):
            return
        active_node_ids = set(resolve_to_node_id.values())
        for node in iter_nodes(nodes):
            item_id = str(node.get("id") or "")
            ref = node.get("media_ref") or {}
            has_jw_identity = bool(ref.get("key_symbol") or ref.get("meps_doc_id"))
            url = self._url_for_node(node)
            has_usable_url = bool(url)
            if (
                node.get("type") != "media"
                or not node.get("meeting_generated")
                or not item_id
                or item_id in active_node_ids
                or not has_jw_identity
                or (
                    has_usable_url and not MeetingTreeController._node_needs_jw_metadata(self, node)
                )
            ):
                continue
            active_node_ids.add(item_id)
            self._request_jw_resolution(node, allow_generated=True)

    @Slot(str, str)
    def _on_presentation_state_changed(self, owner_id: str, item_id: str) -> None:
        if owner_id != self._tree_session.owner_id:
            return
        node = self._find_node(item_id)
        if node is None or node.get("type") != "media":
            return
        state = self._media_tree_runtime.registry.state(owner_id, item_id)
        if state.availability in {
            MediaAvailability.UNKNOWN,
            MediaAvailability.CHECKING,
            MediaAvailability.TEMPORARILY_UNAVAILABLE,
        }:
            return
        if state.availability == MediaAvailability.MISSING:
            self._request_canonical_media_recovery(node)
            self._request_jw_resolution(
                node,
                allow_generated=bool(node.get("meeting_generated")),
            )
            return
        recovery_requests = getattr(self, "_canonical_media_recovery_requests", None)
        if isinstance(recovery_requests, set):
            recovery_requests.discard(self._canonical_media_recovery_key(node)[0])
        ref = node.get("media_ref") or {}
        media_type = node.get("media_type") or self._media_type_from_ref(ref)
        if media_type == "image":
            return
        has_duration = self._duration_ticks(node) > 0
        thumbnail_url = str(node.get("thumbnail_url") or "")
        metadata_source = state.local_path or str(
            node.get("resolved_url") or ref.get("file_path") or ""
        )
        needs_media_probe = not has_duration or (not state.thumbnail_source and not thumbnail_url)
        if metadata_source and needs_media_probe:
            self._queue_info(
                item_id,
                metadata_source,
                media_type,
                purpose="metadata",
                source_signature=state.source_signature,
            )
            if not state.thumbnail_source and thumbnail_url:
                self._queue_info(
                    item_id,
                    thumbnail_url,
                    "image",
                    purpose="thumb",
                    source_signature=thumbnail_source_fingerprint(thumbnail_url),
                )
            return
        if not state.thumbnail_source:
            thumbnail_url = str(node.get("thumbnail_url") or "")
            if thumbnail_url:
                self._queue_info(
                    item_id,
                    thumbnail_url,
                    "image",
                    purpose="thumb",
                    source_signature=thumbnail_source_fingerprint(thumbnail_url),
                )

    def _request_canonical_media_recovery(self, node: Node) -> None:
        """Ask the meeting coordinator to rebuild missing official local media."""

        if self._sync_enabled or not self._tree_key:
            return
        canonical = self._canonical_counterpart(node)
        if canonical is None or canonical.get("type") != "media":
            return
        request_key = self._canonical_media_recovery_key(node)
        recovery_scope, source = request_key
        if recovery_scope in self._canonical_media_recovery_requests:
            return
        self._canonical_media_recovery_requests.add(recovery_scope)
        log.info(
            "Canonical meeting media missing; requesting recovery tree=%s source=%s",
            self._tree_key,
            source or recovery_scope,
        )
        self.canonicalMediaRecoveryRequested.emit(
            self._tree_key,
            source,
        )

    def _canonical_media_recovery_key(self, node: Node) -> tuple[str, str]:
        source = self._url_for_node(node)
        source_key = str(node.get("meeting_source_key") or node.get("id") or "")
        if source and not source.startswith(("http://", "https://")):
            for parent in Path(source).parents:
                if parent.name.startswith("x_"):
                    return os.path.normcase(os.path.abspath(parent)), source
        return source or source_key, source

    def _request_jw_resolution(
        self,
        node: Node,
        *,
        allow_generated: bool = False,
    ) -> None:
        item_id = str(node.get("id") or "")
        ref = node.get("media_ref") or {}
        if (
            not item_id
            or (node.get("meeting_generated") and not allow_generated)
            or not (ref.get("key_symbol") or ref.get("meps_doc_id"))
            or item_id in self._resolve_to_node_id.values()
        ):
            return
        request_id = f"meetingtree:{item_id}:{uuid.uuid4().hex}"
        self._resolve_to_node_id[request_id] = item_id
        identity_map = getattr(self, "_resolve_to_identity", None)
        if isinstance(identity_map, dict):
            identity_map[request_id] = media_identity_signature(node)
        self._svc.resolve_media_async(request_id, meeting_media_from_ref(ref))

    def _queue_info(
        self,
        item_id: str,
        url: str,
        media_type: str,
        *,
        purpose: str = "metadata",
        source_signature: str = "",
    ) -> None:
        source_identity = self._media_info_source_identity(url)
        request_key = (item_id, purpose, source_identity, source_signature)
        if request_key in self._active_info_requests:
            return
        for token, active_key in tuple(self._info_request_by_token.items()):
            if active_key[:2] != request_key[:2]:
                continue
            self._info_queue.invalidate(token)
            self._info_request_by_token.pop(token, None)
            self._active_info_requests.discard(active_key)
        token = self._next_token
        self._next_token += 1
        self._info_request_by_token[token] = request_key
        self._active_info_requests.add(request_key)
        self._info_queue.request(
            token,
            url,
            media_type,
            require_duration=(purpose == "metadata" and media_type in {"audio", "video"}),
            restart_on_source_change=False,
        )

    def _cancel_media_info_requests_for_item(self, item_id: str) -> None:
        """Stop queued, active, and retrying media work for a removed node."""

        for token, request_key in tuple(self._info_request_by_token.items()):
            if request_key[0] != item_id:
                continue
            self._info_queue.invalidate(token)
            self._info_request_by_token.pop(token, None)
            self._active_info_requests.discard(request_key)

    @staticmethod
    def _media_info_source_identity(url: str) -> str:
        if MediaCacheManager.is_remote(url):
            return url
        return os.path.normcase(os.path.abspath(url))

    def _media_info_request_is_current(
        self,
        node: Node,
        purpose: str,
        source_identity: str,
        source_signature: str,
    ) -> bool:
        if purpose == "thumb":
            current_source = str(node.get("thumbnail_url") or "")
        else:
            current_source = self._url_for_node(node)
            if MediaCacheManager.is_remote(current_source) and not MediaCacheManager.is_remote(
                source_identity
            ):
                item_id = str(node.get("id") or "")
                current_source = self._media_tree_runtime.registry.state(
                    self._tree_session.owner_id,
                    item_id,
                ).local_path
        if not current_source or (
            self._media_info_source_identity(current_source) != source_identity
        ):
            return False
        if purpose != "metadata" or not source_signature:
            return True
        item_id = str(node.get("id") or "")
        return (
            self._media_tree_runtime.registry.state(
                self._tree_session.owner_id,
                item_id,
            ).source_signature
            == source_signature
        )

    @Slot(str, object)
    def _on_media_resolved(self, request_id: str, raw_result: object):
        item_id = self._resolve_to_node_id.pop(request_id, "")
        identity_map = getattr(self, "_resolve_to_identity", None)
        expected_identity = (
            identity_map.pop(request_id, None) if isinstance(identity_map, dict) else None
        )
        if not item_id:
            return
        node = self._find_node(item_id)
        if not node:
            return
        if expected_identity is not None and media_identity_signature(node) != expected_identity:
            return
        result = dict(raw_result) if isinstance(raw_result, dict) else {}
        url = str(result.get("url") or "")
        title = str(result.get("title") or "")
        thumb_url = str(result.get("thumbnail") or "")
        try:
            duration_ticks = max(0, int(result.get("duration_ticks") or 0))
        except (TypeError, ValueError, OverflowError):
            duration_ticks = 0
        if url:
            node["resolved_url"] = url
            self._resolved_urls[item_id] = url
            if title and self._should_accept_resolved_title(node):
                node["title"] = title
                node["auto_title"] = False
                node.setdefault("media_ref", {})["label"] = title
        if thumb_url:
            if node.get("thumbnail_url") and node.get("thumbnail_url") != thumb_url:
                node.pop("thumbnail_local_path", None)
                node.pop("thumbnail_cache_key", None)
            node["thumbnail_url"] = thumb_url
            node["thumbnail_binding"] = "jw_artwork"
        if duration_ticks > 0:
            node["base_duration_ticks"] = duration_ticks
        self._save()
        self._emit_media_changed(item_id)
        self._emit_cloud_for_node(item_id)
        self._start_media_requests([node], resolve_jw_metadata=False)

    @Slot(int, object, str)
    def _on_info_ready(self, token: int, pixmap: QPixmap, title: str):
        request_key = self._info_request_by_token.pop(token, None)
        self._info_queue.invalidate(token)
        if request_key is None:
            return
        self._active_info_requests.discard(request_key)
        item_id, purpose, source_identity, source_signature = request_key
        if not item_id:
            return
        node = self._find_node(item_id)
        if not node or not self._media_info_request_is_current(
            node,
            purpose,
            source_identity,
            source_signature,
        ):
            return
        patch: dict[str, Any] = {}
        use_pixmap = bool(
            pixmap and not pixmap.isNull() and (purpose == "thumb" or not node.get("thumbnail_url"))
        )
        if use_pixmap:
            thumbnail = self._bounded_thumbnail(pixmap)
            self._thumb_cache[item_id] = thumbnail
            node["thumbnail_binding"] = "jw_artwork" if purpose == "thumb" else "media_frame"
            if path := self._save_thumbnail_for_node(
                node,
                thumbnail,
                source_signature,
            ):
                patch["thumbnail_local_path"] = path
                patch["thumbnail_cache_key"] = str(node.get("thumbnail_cache_key") or "")
                self._thumb_versions[item_id] = self._thumb_versions.get(item_id, 0) + 1
        if (
            title
            and self._should_accept_resolved_title(node)
            and str(node.get("title") or "") != title
        ):
            node["title"] = title
            node["auto_title"] = False
            node.setdefault("media_ref", {})["label"] = title
            patch.update(
                {
                    "title": title,
                    "auto_title": False,
                    "media_ref_label": title,
                }
            )
        if patch:
            self._queue_derived_media_patch(node, patch)
            self._emit_media_changed(item_id)

    @Slot(int, object)
    def _on_info_failed(self, token: int, failure: object) -> None:
        request_key = self._info_request_by_token.pop(token, None)
        if request_key is not None:
            self._active_info_requests.discard(request_key)
            if getattr(failure, "code", "") == "source-changed":
                self._tree_session.request_nodes({request_key[0]})

    def _should_accept_resolved_title(self, node: Node) -> bool:
        return should_accept_resolved_media_title(
            node,
            placeholder_titles=(_tr("_MediaRow", "Media"),),
        )

    @Slot(int, int)
    def _on_duration_ready(self, token: int, duration_ms: int):
        request_key = self._info_request_by_token.get(token)
        item_id = request_key[0] if request_key is not None else ""
        if not item_id or duration_ms <= 0:
            return
        node = self._find_node(item_id)
        if (
            not node
            or request_key is None
            or not self._media_info_request_is_current(
                node,
                request_key[1],
                request_key[2],
                request_key[3],
            )
        ):
            return
        ticks = int(duration_ms) * 10_000
        if self._duration_ticks(node) > 0:
            return
        node["base_duration_ticks"] = ticks
        self._queue_derived_media_patch(node, {"base_duration_ticks": ticks})
        self._emit_media_changed(item_id)

    def _queue_derived_media_patch(
        self,
        node: Node,
        patch: dict[str, Any],
    ) -> None:
        item_id = str(node.get("id") or "")
        if not item_id or not patch:
            return
        identity = media_identity_signature(node)
        current = self._derived_media_patches.get(item_id)
        merged = dict(current[1]) if current and current[0] == identity else {}
        merged.update(patch)
        self._derived_media_patches[item_id] = (identity, merged)
        self._derived_media_save_timer.start()

    def _flush_derived_media_patches(self) -> bool:
        timer = getattr(self, "_derived_media_save_timer", None)
        if timer is not None:
            timer.stop()
        pending = getattr(self, "_derived_media_patches", None)
        if not getattr(self, "_tree_key", "") or not pending:
            return True
        patches = pending
        self._derived_media_patches = {}
        if self._save():
            return True
        for item_id, entry in patches.items():
            current = self._derived_media_patches.get(item_id)
            if current is None or current[0] != entry[0]:
                self._derived_media_patches[item_id] = entry
                continue
            merged = dict(entry[1])
            merged.update(current[1])
            self._derived_media_patches[item_id] = (entry[0], merged)
        if timer is not None:
            timer.start()
        return False

    @Slot(str)
    def _on_cache_changed(self, url: str):
        self._cloud_progress_by_url.pop(url, None)
        self._tree_session.request_source(url)

    @Slot(str)
    def _on_cache_removed(self, path: str):
        from ...core.media.download_storage import cached_path_for

        removed = os.path.normcase(os.path.abspath(path))
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media":
                continue
            url = self._url_for_node(node)
            if not MediaCacheManager.is_remote(url):
                continue
            cached_path = cached_path_for(
                url,
                self._media_cache_manager.media_cache_dir,
            )
            if os.path.normcase(os.path.abspath(cached_path)) == removed:
                self._tree_session.request_source(url)

    @Slot(str, int, int)
    def _on_prefetch_progress(self, url: str, downloaded: int, total: int):
        if total > 0:
            self._cloud_progress_by_url[url] = downloaded / total
        self._tree_session.patch_source_progress(url, downloaded, total)

    @Slot(str, str)
    def _on_prefetch_error(self, url: str, _message: str):
        self._cloud_progress_by_url.pop(url, None)
        self._tree_session.request_source(url)

    def _emit_media_changed(self, item_id: str) -> None:
        node = self._find_node(item_id)
        if not node:
            return
        state_patch: dict[str, object] = {
            "duration_ticks": self._duration_ticks(node),
        }
        pixmap = self._thumb_cache.get(item_id)
        if pixmap is not None and not pixmap.isNull():
            state_patch["thumbnail_source"] = f"image://playlistthumbs/{item_id}"
        if self._tree_session.owner_id:
            self._media_tree_runtime.registry.patch(
                self._tree_session.owner_id,
                item_id,
                **state_patch,
            )
        _emit_controller_state_changed(self)

    def _emit_cloud_for_url(self, url: str) -> None:
        if not url:
            return
        for node in iter_nodes(self._nodes):
            if node.get("type") == "media" and self._url_for_node(node) == url:
                self._emit_cloud_for_node(str(node.get("id", "")))

    def _emit_cloud_for_node(self, item_id: str) -> None:
        node = self._find_node(item_id)
        if not node:
            return
        self._tree_session.request_source(self._url_for_node(node))

    def _emit_section_counts(self) -> None:
        _emit_controller_state_changed(self)
        self.chromeChanged.emit()
