"""
meeting_tree_controller.py - Solin
==================================
QML controller for meeting trees.

This intentionally implements the PlaylistTreeView protocol without inheriting
from PlaylistEditModel/PlaylistEditBridge.  Meetings have their own persistence
and merge rules while sharing the same tree surface.
"""
from __future__ import annotations

import copy
import logging
import os
import random
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import (
    QObject,
    Property,
    QCoreApplication,
    QTimer,
    QUrl,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import QFileDialog, QMessageBox, QDialog

from ...core.media.cache import MediaCacheManager
from ...core.media.duration import format_effective_duration_ticks
from ...core.media.profile_store import ProfileMediaStore
from ...core.media.thumbnail_store import ThumbnailStore
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
from ...core.media.insertion import MediaInsertResult
from ...core.media.playback_request import MediaTrim
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.foundation.qt_threads import stop_owned_qthread
from ...core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from ...core.i18n.strings import (
    tr_offline_download,
    tr_offline_downloading,
    tr_offline_downloading_progress,
)
from ...core.ingest.manifest import (
    ManifestError,
    ManifestWriteError,
    cache_dir,
    retry_manifest_write,
)
from ...core.meetings.models import MemorialData
from ...core.meetings.linked_folder_sync import (
    MeetingSyncRecord,
    MeetingSyncError,
    MeetingSyncIdentity,
)
from ...core.meetings.catalog_placement import (
    MeetingCatalogPlaylistRef,
    build_meeting_catalog_playlist_ref,
)
from ...core.meetings.media_nodes import (
    create_manual_media_node,
    create_playlist_media_node,
    int_or_zero,
    meeting_media_from_ref,
    media_ref_title,
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
    media_identity_signature,
    merge_persisted_meeting_trees,
)
from ...core.meetings.tree_store import (
    MeetingTreeOverview,
    MeetingTreeSnapshot,
    MeetingTreeStore,
)
from ...core.meetings.thumbnails import meeting_thumb_storage_id
from ...core.meetings.section_meta import SECTION_META
from ...core.meetings.tree_types import Node, clone_nodes, count_media, iter_nodes, new_node_id
from ...core.projection.aspect_ratio import (
    DEFAULT_PROJECTION_ASPECT_RATIO,
    ProjectionAspectRatio,
)
from ...core.projection.image_framing import (
    ImageTransform,
    image_transform_from_record,
    image_transform_to_record,
    prepare_image_transform_for_aspect,
)
from ...core.playlists.jwl_import import playlist_items_from_jwl_document_items
from ...core.tree_delta import incremental_tree_changes
from ...core.meetings.colors import generate_section_hue, section_colors
from ...styles.theme import current_theme_scheme
from ..playlist.dialogs import HuePickerDialog, NameDialog
from ...ui.media_info import MediaInfoQueue
from ...ui.thumbnail_images import (
    image_source_aspect_ratio,
    load_thumbnail,
    save_thumbnail,
)

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
    expected_revision: int
    next_attempt_at: float
    generation: int = 1
    first_attempt_at: float | None = None
    retry_index: int = 0


def _clear_tree_data_cache(controller: Any) -> None:
    if hasattr(controller, "_tree_data_cache"):
        controller._tree_data_cache = None


def _reset_media_request_queue(controller: Any) -> None:
    timer = getattr(controller, "_media_request_timer", None)
    if timer is not None:
        timer.stop()
    queue = getattr(controller, "_media_request_queue", None)
    if queue is not None:
        queue.clear()
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
    _clear_tree_data_cache(controller)
    controller.stateChanged.emit()


def _local_file_url(path: str) -> str:
    if not path or MediaCacheManager.is_remote(path) or not os.path.exists(path):
        return ""
    return QUrl.fromLocalFile(path).toString()


def _tr(context: str, source: str) -> str:
    return QCoreApplication.translate(context, source)


_PLAYLIST_EDIT_CONTEXT = "PlaylistEditView"


def _translate_section_title(source: str) -> str:
    context = "SermonThemeWidget" if source == "PUBLIC TALK" else "_Section"
    return _tr(context, source)


def _display_section_title(node: Node) -> str:
    title = str(node.get("title") or "")
    if (
        node.get("type") not in ("section", "subsection")
        or not node.get("meeting_generated")
        or node.get("user_title_override")
    ):
        return title
    source_key = str(node.get("meeting_source_key") or "")
    section_code = str(node.get("section_code") or source_key.rsplit(":", 1)[-1])
    source = SECTION_META.get(section_code, ("", 0))[0]
    return _translate_section_title(source) if source else title


def _has_jw_media_identity(ref: dict[str, Any]) -> bool:
    return bool(str(ref.get("key_symbol") or "").strip() or ref.get("meps_doc_id"))


def _usable_ref_file_path(ref: dict[str, Any]) -> str:
    path = str(ref.get("file_path") or "")
    if not path:
        return ""
    if MediaCacheManager.is_remote(path) or os.path.exists(path):
        return path
    if _has_jw_media_identity(ref):
        return ""
    return path


class MeetingTreeController(QObject):
    backRequested = Signal()
    projectRequested = Signal(object)
    pointerEntered = Signal()
    pointerExited = Signal()

    stateChanged = Signal()
    chromeChanged = Signal()
    mediaChanged = Signal(str, str, str, str)
    mediaInserted = Signal(str, int, "QVariant")
    nodesInserted = Signal(str, int, "QVariant")
    nodeReplaced = Signal(str, "QVariant")
    nodeMoved = Signal(str, str, int)
    sectionChanged = Signal(str, str, str, str, str, int)
    sectionCollapseChanged = Signal(str, bool)
    sectionCountsChanged = Signal("QVariant")
    markerEditRequested = Signal(str)
    cloudChanged = Signal(str, bool, bool, float, str)
    imageFramingChanged = Signal(str, "QVariant")
    syncStateChanged = Signal()
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
        self._projection_aspect_ratio_provider = (
            projection_aspect_ratio_provider
            or (lambda: DEFAULT_PROJECTION_ASPECT_RATIO)
        )
        self._builder = MeetingTreeBuilder(
            section_title=_translate_section_title,
            media_fallback_title=lambda: _tr("_MediaRow", "Media"),
        )
        self._sync_service = linked_folder_sync
        self._nodes: list[Node] = []
        self._tree_data_cache: list[Node] | None = None
        self._tree_key = ""
        self._canonical_hash = ""
        self._sync_identity: MeetingSyncIdentity | None = None
        self._sync_root = ""
        self._sync_folder = ""
        self._sync_available = False
        self._sync_enabled = False
        self._sync_busy = False
        self._sync_revision = 0
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
        self._info_request_by_token: dict[int, tuple[str, str]] = {}
        self._active_info_requests: set[tuple[str, str]] = set()
        self._next_token = 1
        self._resolve_to_node_id: dict[str, str] = {}
        self._resolved_urls: dict[str, str] = {}
        self._cloud_progress_by_url: dict[str, float] = {}
        self._media_request_queue: deque[Node] = deque()
        self._media_request_timer = QTimer(self)
        self._media_request_timer.setSingleShot(True)
        self._media_request_timer.setInterval(0)
        self._media_request_timer.timeout.connect(self._drain_media_request_queue)
        self._derived_media_patches: dict[
            str,
            tuple[tuple, dict[str, Any]],
        ] = {}
        self._derived_media_save_timer = QTimer(self)
        self._derived_media_save_timer.setSingleShot(True)
        self._derived_media_save_timer.setInterval(
            _DERIVED_MEDIA_SAVE_DEBOUNCE_MS
        )
        self._derived_media_save_timer.timeout.connect(
            self._flush_derived_media_patches
        )
        self._image_framing_save_pending = False
        self._image_framing_save_timer = QTimer(self)
        self._image_framing_save_timer.setSingleShot(True)
        self._image_framing_save_timer.setInterval(200)
        self._image_framing_save_timer.timeout.connect(
            self._flush_image_framing_save
        )
        self._pdf_threads: list[Any] = []
        self._jwpub_threads: list[Any] = []
        self._lo_threads: list[Any] = []
        self._linked_folder_files: dict[str, str] = {}  # file_path → node_id
        self._linked_folder_availability: tuple[tuple[str, bool], ...] = ()
        self._meeting_folder_imports: dict[str, dict[str, Any]] = {}
        self._meeting_folder_pending_sources: set[str] = set()
        self._overview: MeetingTreeOverview | None = None
        self._connect_services()

    def _invalidate_tree_data_cache(self) -> None:
        _clear_tree_data_cache(self)

    def _emit_state_changed(self) -> None:
        _emit_controller_state_changed(self)

    def refresh_theme(self) -> None:
        _emit_controller_state_changed(self)
        self._emit_section_counts()

    def refresh_language(self) -> None:
        _emit_controller_state_changed(self)

    def _reset_media_request_queue(self) -> None:
        _reset_media_request_queue(self)

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

    @Property(bool, notify=syncStateChanged)
    def syncPending(self):
        if not self._sync_folder:
            return False
        key = os.path.normcase(os.path.abspath(self._sync_folder))
        return key in self._pending_sync_saves

    @Property(str, notify=syncStateChanged)
    def syncFolderPath(self):
        return self._sync_folder

    @Property(bool, notify=chromeChanged)
    def hasItems(self):
        return bool(self._nodes)

    @Property(str, notify=chromeChanged)
    def itemCountText(self):
        count = count_media(self._nodes)
        word = _tr("_PlaylistEditView", "items")
        return f"{count} {word}"

    @Property("QVariant", notify=stateChanged)
    def playlistData(self):
        return self.tree_data()

    def placement_playlist_ref(self) -> MeetingCatalogPlaylistRef:
        return build_meeting_catalog_playlist_ref(
            self._nodes,
            section_title=_display_section_title,
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
            item
            for item in items
            if isinstance(item, dict) and playlist_item_media_url(item)
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
            self._refresh_sync_from_manifest()

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
        store = getattr(self, "_store", None)
        latest_lookup = getattr(store, "snapshot", None)
        latest = latest_lookup(snapshot.tree_key) if callable(latest_lookup) else None
        if latest is not None and latest.revision > snapshot.revision:
            snapshot = latest
        _reset_media_request_queue(self)
        self._meeting_type = snapshot.pub_type
        self._tree_key = snapshot.tree_key
        self._canonical_hash = snapshot.canonical_hash
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
        pending_sync = (
            pending_lookup(self._sync_identity) if callable(pending_lookup) else None
        )
        sync_record = None if pending_sync is not None else self._load_sync_record()
        if pending_sync is not None:
            self._deleted_source_keys |= set(pending_sync.deleted_source_keys)
            self._nodes = merge_persisted_meeting_trees(
                snapshot.nodes,
                pending_sync.nodes,
                self._deleted_source_keys,
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
        elif sync_record is not None:
            self._apply_sync_record(sync_record)
            self._deleted_source_keys |= set(snapshot.deleted_source_keys)
            self._nodes = merge_persisted_meeting_trees(
                snapshot.nodes,
                sync_record.nodes,
                self._deleted_source_keys,
            )
            self._linked_folder_files = {
                **snapshot.linked_folder_files,
                **sync_record.linked_folder_files,
            }
            self._meeting_folder_imports = {
                **copy.deepcopy(snapshot.meeting_folder_imports),
                **copy.deepcopy(sync_record.meeting_folder_imports),
            }
            self._save()
        else:
            self._sync_enabled = False
            self._sync_revision = 0
            self._sync_folder = self._candidate_sync_folder()
            self._nodes = clone_nodes(snapshot.nodes)
        self._meeting_folder_pending_sources.clear()
        self._linked_folder_availability = self._linked_folder_availability_signature()
        if start_media_requests:
            MeetingTreeController.start_media_enrichment(self)
        self.chromeChanged.emit()
        self.syncStateChanged.emit()
        _emit_controller_state_changed(self)
        persisted = latest_lookup(self._tree_key) if callable(latest_lookup) else None
        return persisted or snapshot

    def start_media_enrichment(self) -> None:
        """Start local-only presentation metadata work after the detail is visible."""
        self._start_media_requests()

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
        _reset_media_request_queue(self)
        self._canonical_hash = self._builder.canonical_hash(canonical)
        if self._sync_identity is not None:
            self._sync_identity = MeetingSyncIdentity(
                tree_key=self._sync_identity.tree_key,
                pub_type=self._sync_identity.pub_type,
                monday=self._sync_identity.monday,
                canonical_hash=self._canonical_hash,
            )
        saved, _ = self._store.load(self._tree_key)
        self._deleted_source_keys = self._store.load_deleted_source_keys(self._tree_key)
        self._linked_folder_files = self._store.load_linked_folder_files(self._tree_key)
        self._meeting_folder_imports = self._store.load_meeting_folder_imports(self._tree_key)
        pending_lookup = getattr(self, "_pending_sync_save_for_identity", None)
        pending_sync = (
            pending_lookup(self._sync_identity) if callable(pending_lookup) else None
        )
        sync_record = None if pending_sync is not None else self._load_sync_record()
        if pending_sync is not None:
            saved = clone_nodes(pending_sync.nodes)
            self._deleted_source_keys = set(pending_sync.deleted_source_keys)
            self._linked_folder_files = dict(pending_sync.linked_folder_files)
            self._meeting_folder_imports = copy.deepcopy(
                pending_sync.meeting_folder_imports
            )
            self._sync_folder = os.fspath(pending_sync.folder)
            self._sync_enabled = True
            self._sync_revision = pending_sync.expected_revision
        elif sync_record is not None:
            saved = sync_record.nodes
            self._apply_sync_record(sync_record)
        else:
            self._sync_enabled = False
            self._sync_revision = 0
            self._sync_folder = self._candidate_sync_folder()
        self._meeting_folder_pending_sources.clear()
        self._nodes = MeetingTreeMerger(
            canonical,
            self._deleted_source_keys,
        ).merge(saved)
        self._linked_folder_availability = self._linked_folder_availability_signature()
        self._save()
        self._start_media_requests()
        self.chromeChanged.emit()
        self.syncStateChanged.emit()
        _emit_controller_state_changed(self)

    def _refresh_sync_availability(self) -> None:
        available = bool(
            self._sync_root
            and Path(self._sync_root).is_dir()
            and self._sync_identity is not None
        )
        if available == self._sync_available:
            return
        self._sync_available = available
        if not available:
            self._sync_enabled = False
            self._sync_folder = ""
            self._sync_revision = 0
        self.syncStateChanged.emit()

    def _candidate_sync_folder(self) -> str:
        if not self._sync_root or self._sync_identity is None:
            return ""
        folder = self._sync_service.locate_folder(
            self._sync_root,
            self._sync_identity,
            create=False,
        )
        return str(folder) if folder else ""

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

    def _load_sync_record(self):
        if not self._sync_available or self._sync_identity is None:
            return None
        try:
            return self._sync_service.load_tree(self._sync_root, self._sync_identity)
        except ManifestError as exc:
            log_ignored_exception(__name__, "Meeting linked-folder manifest is invalid")
            QMessageBox.warning(
                self.parent(),
                _tr("MeetingSync", "Meeting sync"),
                str(exc),
            )
            return None
        except MeetingSyncError:
            log_ignored_exception(__name__, "Could not load meeting linked-folder sync")
            return None

    def _refresh_sync_from_manifest(self) -> bool:
        self._flush_image_framing_save()
        sync_folder = getattr(self, "_sync_folder", "")
        pending_saves = getattr(self, "_pending_sync_saves", {})
        if sync_folder:
            key = os.path.normcase(os.path.abspath(sync_folder))
            if key in pending_saves:
                return True
        record = self._load_sync_record()
        if record is None:
            if self._sync_enabled:
                self._sync_enabled = False
                self._sync_revision = 0
                self._sync_folder = self._candidate_sync_folder()
                self.syncStateChanged.emit()
            return False
        if self._sync_enabled and record.revision == self._sync_revision:
            return True

        changes = incremental_tree_changes(
            self._nodes,
            record.nodes,
            section_patch_keys={"title", "color_hue", "collapsed"},
        )
        if changes is not None:
            self._apply_sync_record(record)
            self._meeting_folder_pending_sources.clear()
            availability = self._linked_folder_availability_signature()
            availability_changed = availability != self._linked_folder_availability
            self._linked_folder_availability = availability
            self._save_local_cache()
            if availability_changed:
                self._start_media_requests()
                self.chromeChanged.emit()
                self.syncStateChanged.emit()
                _emit_controller_state_changed(self)
                return True
            for removal in changes.removals:
                self.nodeReplaced.emit(removal.node_id, [])
            for insertion in changes.inserts:
                self.nodesInserted.emit(
                    insertion.target_list_id,
                    insertion.insert_index,
                    [self._qml_node(node) for node in insertion.nodes],
                )
            for move in changes.moves:
                self.nodeMoved.emit(
                    move.node_id,
                    move.target_list_id,
                    move.insert_index,
                )
            for update in changes.section_updates:
                node = self._find_node(update.node_id)
                if node is None:
                    continue
                if update.metadata_changed:
                    self._emit_section_changed(node)
                if update.collapsed_changed:
                    self.sectionCollapseChanged.emit(
                        update.node_id,
                        bool(node.get("collapsed", False)),
                    )
            if changes.has_changes:
                self._emit_section_counts()
                self._start_media_requests()
            self.chromeChanged.emit()
            self.syncStateChanged.emit()
            return True

        self._apply_sync_record(record)
        self._meeting_folder_pending_sources.clear()
        self._linked_folder_availability = self._linked_folder_availability_signature()
        self._save_local_cache()
        self._start_media_requests()
        self.chromeChanged.emit()
        self.syncStateChanged.emit()
        _emit_controller_state_changed(self)
        return True

    def _apply_sync_record(self, record: MeetingSyncRecord) -> None:
        self._nodes = record.nodes
        _clear_tree_data_cache(self)
        self._deleted_source_keys = record.deleted_source_keys
        self._linked_folder_files = record.linked_folder_files
        self._meeting_folder_imports = record.meeting_folder_imports
        self._sync_folder = str(record.folder)
        self._sync_enabled = True
        self._sync_revision = record.revision

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

        folders = self._watched_folder_file_store.scan_meeting_sources(
            watched_folder_path
        )
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

        self._emit_linked_folder_availability_if_changed()
        if touched:
            self.chromeChanged.emit()

    def _linked_folder_availability_signature(self) -> tuple[tuple[str, bool], ...]:
        """Snapshot local availability for linked-folder media nodes."""
        urls: list[str] = []
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media" or not node.get("linked_folder_source"):
                continue
            urls.append(self._url_for_node(node))
        return self._watched_folder_file_store.file_availability_signature(urls)

    def _emit_linked_folder_availability_if_changed(self) -> None:
        availability = self._linked_folder_availability_signature()
        if availability == self._linked_folder_availability:
            return
        self._linked_folder_availability = availability
        available_nodes: list[Node] = []
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media" or not node.get("linked_folder_source"):
                continue
            url = self._url_for_node(node)
            if not url or MediaCacheManager.is_remote(url) or os.path.exists(url):
                available_nodes.append(node)
        self._start_media_requests(available_nodes)
        _emit_controller_state_changed(self)

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

    def _record_meeting_folder_failure(
        self, source: dict[str, Any], name: str, error: str
    ) -> None:
        self._record_meeting_folder_import(
            source,
            [],
            status="failed",
            error=error,
        )
        self._warn_import_failed(name, error)

    def _page_nodes(self, pages: list[str], stem: str) -> list[Node]:
        return [
            self._manual_media_node(page, title=f"{stem} - p. {idx + 1}")
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
            lambda pages, pdf_stem, _source=dict(source), _list=list_id, _index=insert_index:
                self._insert_meeting_folder_nodes(
                    _source, self._page_nodes(pages, pdf_stem), _list, _index
                )
        )
        thread.conversion_failed.connect(
            lambda error, _source=dict(source), _name=stem:
                self._record_meeting_folder_failure(_source, _name, error)
        )
        thread.finished.connect(
            lambda t=thread: self._pdf_threads.remove(t)
            if t in self._pdf_threads else None
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
            lambda pages, doc_stem, _source=dict(source), _list=list_id, _index=insert_index:
                self._insert_meeting_folder_nodes(
                    _source, self._page_nodes(pages, doc_stem), _list, _index
                )
        )
        thread.conversion_failed.connect(
            lambda error, _source=dict(source), _name=stem:
                self._record_meeting_folder_failure(_source, _name, error)
        )
        thread.finished.connect(
            lambda t=thread: self._lo_threads.remove(t)
            if t in self._lo_threads else None
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
            lambda items, file_stem, _source=dict(source), _list=list_id, _index=insert_index:
                self._insert_meeting_folder_nodes(
                    _source,
                    [self._node_from_playlist_item(raw, file_stem) for raw in items],
                    _list,
                    _index,
                )
        )
        thread.failed.connect(
            lambda error, _source=dict(source), _name=stem:
                self._record_meeting_folder_failure(_source, _name, error)
        )
        thread.finished.connect(
            lambda t=thread: self._jwpub_threads.remove(t)
            if t in self._jwpub_threads else None
        )
        thread.start()

    def _import_meeting_folder_jwlplaylist(
        self, source: dict[str, Any], list_id: str, insert_index: int
    ) -> None:
        from ...core.playlists.jwl_files import read_jwlplaylist_document

        path = str(source.get("path") or "")
        try:
            document = read_jwlplaylist_document(
                path,
                fallback_lang_code=self._fallback_language_code,
            )
            result = playlist_items_from_jwl_document_items(
                document.items,
                source_name=Path(path).name,
                save_embedded=self._save_jwl_embedded,
            )
            nodes = [
                self._node_from_playlist_item(raw, Path(path).stem)
                for raw in result.items
            ]
        except (OSError, ValueError) as exc:
            self._record_meeting_folder_failure(source, Path(path).name, str(exc))
            return
        self._insert_meeting_folder_nodes(source, nodes, list_id, insert_index)

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

    def tree_data(self) -> list[Node]:
        if self._tree_data_cache is None:
            self._tree_data_cache = [self._qml_node(node) for node in self._nodes]
        return self._tree_data_cache

    def cleanup(self) -> None:
        self._flush_image_framing_save()
        self._flush_derived_media_patches()
        self._sync_save_timer.stop()
        final_saves: list[Future[MeetingSyncRecord]] = []
        for request in self._pending_sync_saves.values():
            final_saves.append(
                self._sync_save_executor.submit(
                    retry_manifest_write,
                    lambda pending=request: self._sync_service.save_tree(
                        pending.folder,
                        pending.identity,
                        nodes=pending.nodes,
                        deleted_source_keys=pending.deleted_source_keys,
                        linked_folder_files=pending.linked_folder_files,
                        meeting_folder_imports=pending.meeting_folder_imports,
                        expected_revision=pending.expected_revision,
                    ),
                )
            )
        self._sync_save_executor.shutdown(wait=True, cancel_futures=False)
        for future in final_saves:
            try:
                future.result()
            except (ManifestError, MeetingSyncError, OSError):
                log.warning("Could not flush meeting manifest during cleanup", exc_info=True)
        self._pending_sync_saves.clear()
        _reset_media_request_queue(self)
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
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect meeting tree info queue")
        try:
            self._svc.video_resolved.disconnect(self._on_video_resolved)
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

    def disk_thumbnail(self, item_id: str) -> QPixmap | None:
        node = self._find_node(item_id)
        path = self._thumbnail_local_path(node, item_id)
        if os.path.exists(path):
            px = QPixmap(path)
            if not px.isNull():
                return px
        return None

    def add_files(self, paths: list[str], list_id: str = "root",
                  insert_index: int = _BIG_INDEX) -> None:
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
            added = self._import_jwlplaylists(jwl_paths, list_id or "root", cursor)
            if cursor < _BIG_INDEX:
                cursor += added
        if lo_paths:
            self._import_lo_files(lo_paths, list_id or "root", cursor)

    def add_from_jw_catalog(
        self,
        item_data: dict[str, Any],
        list_id: str = "root",
        insert_index: int = _BIG_INDEX,
    ) -> MediaInsertResult:
        node_id = new_node_id()
        track = int_or_zero(item_data.get("track"))
        issue = int_or_zero(item_data.get("issue"))
        doc_id = int_or_zero(item_data.get("docid"))
        ref = {
            "multimedia_id": 0,
            "mime_type": "video/mp4",
            "file_path": item_data.get("download_url", ""),
            "label": item_data.get("title", ""),
            "caption": "",
            "begin_ordinal": 0,
            "key_symbol": item_data.get("pub") or "",
            "track": track,
            "issue_tag": issue,
            "meps_doc_id": doc_id,
            "meps_language": int_or_zero(item_data.get("meps_language")),
            "language": str(item_data.get("language") or "").upper(),
            "jw_media_id": str(item_data.get("jw_media_id") or ""),
            "section": "",
            "is_song": False,
            "cbs_article_title": "",
        }
        partition = partition_media_items(self._media_identity_records(), [ref])
        if partition.duplicate_items:
            return MediaInsertResult(duplicate_items=partition.duplicate_items)
        node = {
            "id": node_id,
            "type": "media",
            "title": ref["label"] or _tr("_MediaRow", "Media"),
            "media_type": "video",
            "media_ref": ref,
            "children": [],
            "meeting_generated": False,
        }
        thumb_path = item_data.get("thumbnail_path", "")
        if thumb_path and os.path.exists(thumb_path):
            try:
                target_path = self._meeting_thumbnail_store.copy_from(
                    node_id,
                    thumb_path,
                )
                target = os.fspath(target_path)
                node["thumbnail_cache_key"] = target_path.name
                node["thumbnail_local_path"] = target
            except OSError:
                log_ignored_exception(__name__, "Could not copy meeting item thumbnail")
        if not self._insert_nodes(list_id or "root", insert_index, [node]):
            return MediaInsertResult(target_valid=False)
        return MediaInsertResult(added_items=(item_data,))

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
                lambda pages, pdf_stem, _list=list_id, _index=insert_index:
                    self._on_pdf_pages_ready(pages, pdf_stem, _list, _index)
            )
            thread.conversion_failed.connect(
                lambda error, _name=stem: self._warn_import_failed(_name, error)
            )
            thread.finished.connect(
                lambda t=thread: self._pdf_threads.remove(t)
                if t in self._pdf_threads else None
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
            self._manual_media_node(page, title=f"{pdf_stem} - p. {idx + 1}")
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
                lambda pages, doc_stem, _list=list_id, _index=insert_index:
                    self._on_lo_pages_ready(pages, doc_stem, _list, _index)
            )
            thread.conversion_failed.connect(
                lambda error, _name=stem: self._warn_import_failed(_name, error)
            )
            thread.finished.connect(
                lambda t=thread: self._lo_threads.remove(t)
                if t in self._lo_threads else None
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
            self._manual_media_node(page, title=f"{stem} - p. {idx + 1}")
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
                lambda items, file_stem, _list=list_id, _index=insert_index:
                    self._on_playlist_items_ready(items, file_stem, _list, _index)
            )
            thread.failed.connect(
                lambda error, _name=stem: self._warn_import_failed(_name, error)
            )
            thread.finished.connect(
                lambda t=thread: self._jwpub_threads.remove(t)
                if t in self._jwpub_threads else None
            )
            thread.start()

    def _import_jwlplaylists(self, paths: list[str], list_id: str, insert_index: int) -> int:
        from ...core.playlists.jwl_files import read_jwlplaylist_document

        total = 0
        for path in paths:
            try:
                document = read_jwlplaylist_document(
                    path,
                    fallback_lang_code=self._fallback_language_code,
                )
                result = playlist_items_from_jwl_document_items(
                    document.items,
                    source_name=Path(path).name,
                    save_embedded=self._save_jwl_embedded,
                )
                nodes = [
                    self._node_from_playlist_item(raw, Path(path).stem)
                    for raw in result.items
                ]
            except (OSError, ValueError) as exc:
                self._warn_import_failed(Path(path).name, str(exc))
                continue
            if nodes:
                self._insert_nodes(list_id or "root", insert_index, nodes)
                total += len(nodes)
                if insert_index < _BIG_INDEX:
                    insert_index += len(nodes)
        return total

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

    def _enable_sync(self) -> None:
        if not self._sync_available or self._sync_identity is None:
            return
        old_state = self._sync_snapshot()
        self._set_sync_busy(True)
        try:
            folder = self._sync_service.locate_folder(
                self._sync_root,
                self._sync_identity,
                create=True,
            )
            if folder is None:
                raise MeetingSyncError("Linked folder is not available.")
            record = self._sync_service.load_tree(self._sync_root, self._sync_identity)
            self._sync_folder = str(folder)
            self._sync_enabled = True
            self._sync_revision = record.revision if record is not None else 0
            if record is not None:
                self._apply_sync_record(record)
                self._save_local_cache()
                self._linked_folder_availability = self._linked_folder_availability_signature()
                self.chromeChanged.emit()
                _emit_controller_state_changed(self)
                return
            self._materialize_current_nodes_for_sync()
            saved_record = self._sync_service.save_tree(
                folder,
                self._sync_identity,
                nodes=self._nodes,
                deleted_source_keys=self._deleted_source_keys,
                linked_folder_files=self._linked_folder_files,
                meeting_folder_imports=self._meeting_folder_imports,
                expected_revision=self._sync_revision,
            )
            self._apply_sync_record(saved_record)
            self._save_local_cache()
            self._linked_folder_availability = self._linked_folder_availability_signature()
            self.chromeChanged.emit()
            _emit_controller_state_changed(self)
        except ManifestWriteError as exc:
            if exc.retryable:
                self._save_local_cache()
                self._schedule_sync_manifest_save(delay_ms=100)
            else:
                self._restore_sync_snapshot(old_state)
                self._save_local_cache()
                self._warn_sync_failed(str(exc))
        except (ManifestError, MeetingSyncError, OSError) as exc:
            self._restore_sync_snapshot(old_state)
            self._save_local_cache()
            self._warn_sync_failed(str(exc))
        finally:
            self._set_sync_busy(False)
            self.syncStateChanged.emit()

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

        old_state = self._sync_snapshot()
        self._set_sync_busy(True)
        folder = Path(self._sync_folder)
        folder_key = os.path.normcase(os.path.abspath(folder))
        self._pending_sync_saves.pop(folder_key, None)
        self._arm_sync_save_timer()
        try:
            durable_dir = self._durable_detached_dir()
            self._nodes = self._sync_service.detach_cache_references(
                self._nodes,
                folder,
                durable_dir,
            )
            self._linked_folder_files = self._linked_files_for_current_nodes()
            self._sync_enabled = False
            self._sync_revision = 0
            self._save_local_cache()
            self._sync_service.delete_sync_metadata(folder)
            self._sync_folder = str(folder)
            self._linked_folder_availability = self._linked_folder_availability_signature()
            if self._sync_root:
                self.inject_linked_folder_media(self._sync_root)
            self.chromeChanged.emit()
            _emit_controller_state_changed(self)
        except (ManifestError, MeetingSyncError, OSError) as exc:
            self._restore_sync_snapshot(old_state)
            self._warn_sync_failed(str(exc))
        finally:
            self._set_sync_busy(False)
            self.syncStateChanged.emit()

    def _sync_snapshot(self) -> dict[str, Any]:
        return {
            "nodes": clone_nodes(self._nodes),
            "deleted": set(self._deleted_source_keys),
            "linked": dict(self._linked_folder_files),
            "imports": copy.deepcopy(self._meeting_folder_imports),
            "folder": self._sync_folder,
            "enabled": self._sync_enabled,
            "revision": self._sync_revision,
        }

    def _restore_sync_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._nodes = snapshot["nodes"]
        self._deleted_source_keys = snapshot["deleted"]
        self._linked_folder_files = snapshot["linked"]
        self._meeting_folder_imports = snapshot["imports"]
        self._sync_folder = snapshot["folder"]
        self._sync_enabled = snapshot["enabled"]
        self._sync_revision = snapshot["revision"]

    def _set_sync_busy(self, value: bool) -> None:
        if self._sync_busy == value:
            return
        self._sync_busy = value
        self.syncStateChanged.emit()

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
        framing = image_transform_to_record(
            image_transform_from_record(node.get("image_framing"))
        )
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
        node = self._find_node(item_id)
        ratio = image_source_aspect_ratio(
            path=self._url_for_node(node) if node else ""
        )
        if ratio > 0.0:
            return ratio
        pixmap = self._thumb_cache.get(item_id)
        if pixmap is None or pixmap.isNull():
            pixmap = load_thumbnail(
                self._meeting_thumbnail_store,
                self._thumbnail_storage_id(node, item_id),
            )
        return image_source_aspect_ratio(pixmap=pixmap)

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
        self._tree_data_cache = None
        self.imageFramingChanged.emit(item_id, record)
        self._schedule_image_framing_save()

    @Slot(str)
    def resetImageFraming(self, item_id: str) -> None:  # noqa: N802 - QML API
        node = self._find_node(item_id)
        if not node or "image_framing" not in node:
            return
        node.pop("image_framing", None)
        self._tree_data_cache = None
        self.imageFramingChanged.emit(item_id, None)
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
        self._tree_data_cache = None
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
        node = self._find_node(item_id)
        if not node:
            return
        # If this item came from a linked folder, physically delete the file
        linked_source = node.get("linked_folder_source", "")
        if linked_source:
            file_path = self._url_for_node(node)
            try:
                removed_physical_file = self._watched_folder_file_store.remove_file_inside(
                    file_path,
                    linked_source,
                )
            except OSError:
                removed_physical_file = False
            # Remove from tracking
            self._linked_folder_files.pop(file_path, None)
            self._cleanup_meeting_folder_import_for_removed_node(
                item_id,
                file_path,
                source_removed=removed_physical_file,
            )
        self._remember_deleted_sources(node, include_media=True)
        if self._replace_node(item_id, []):
            self._save_and_emit_replace(item_id, [])

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
            node_ids = [
                str(value)
                for value in record.get("node_ids", [])
                if value
            ]
            contains_node = bool(node_id and node_id in node_ids)
            same_source_file = bool(
                file_path
                and record_path
                and same_local_source(record_path, file_path)
            )
            source_is_gone = bool(
                record_path
                and same_source_file
                and (source_removed or not os.path.exists(record_path))
            )
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
        _clear_tree_data_cache(self)
        self._save()
        self.mediaChanged.emit(item_id, name, self._duration_for(node), self._thumb_source_for(item_id))

    @Slot(str)
    def downloadItem(self, item_id: str):
        url = self._url_for_node_id(item_id)
        if not url:
            return
        mgr = self._media_cache_manager
        if not mgr.is_cached(url) and not mgr.is_prefetching(url):
            mgr.prefetch(url, priority=True)
            self._emit_cloud_for_node(item_id)

    @Slot(str, str)
    def renameMarker(self, marker_id: str, text: str):
        node = self._find_node(marker_id)
        if not node or node.get("type") != "marker":
            return
        node["text"] = text
        node["user_title_override"] = True
        _clear_tree_data_cache(self)
        self._save()

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
        label = (
            _tr(_PLAYLIST_EDIT_CONTEXT, "Subsection name:")
            if node.get("type") == "subsection"
            else _tr(_PLAYLIST_EDIT_CONTEXT, "Section name:")
        )
        dlg = NameDialog(node.get("title", ""), lang=True, parent=self.parent(), label=label)
        dlg.setWindowTitle(_tr(_PLAYLIST_EDIT_CONTEXT, "Rename section"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name = dlg.get_name()
        if not name:
            return
        node["title"] = name
        node["user_title_override"] = True
        _clear_tree_data_cache(self)
        self._save()
        self._emit_section_changed(node)

    @Slot(str)
    def deleteSection(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        reply = QMessageBox.question(
            self.parent(),
            _tr(_PLAYLIST_EDIT_CONTEXT, "Delete section"),
            _tr(_PLAYLIST_EDIT_CONTEXT, 'Delete section "{name}"?\nItems inside will be kept.').replace(
                "{name}", str(node.get("title", ""))
            ),
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
        _clear_tree_data_cache(self)
        self._save()
        self._emit_section_changed(node)

    @Slot(str)
    def toggleCollapse(self, section_id: str):
        node = self._find_node(section_id)
        if not node or node.get("type") not in ("section", "subsection"):
            return
        node["collapsed"] = not bool(node.get("collapsed", False))
        _clear_tree_data_cache(self)
        self._save()

    @Slot()
    def pointerEnter(self):  # noqa: N802
        self.pointerEntered.emit()

    @Slot()
    def pointerExit(self):  # noqa: N802
        self.pointerExited.emit()

    @Slot(str, str, str, result=bool)
    def canDrop(self, node_id: str, node_type: str, target_list_id: str) -> bool:
        return can_drop_tree_node(node_type, target_list_id)

    @Slot(str, str, int, result=bool)
    def moveNode(self, node_id: str, target_list_id: str, insert_index: int) -> bool:
        if not move_tree_node(self._nodes, node_id, target_list_id, insert_index):
            return False
        _clear_tree_data_cache(self)
        self._save()
        self.chromeChanged.emit()
        self._emit_section_counts()
        return True

    def _connect_services(self) -> None:
        self._svc.video_resolved.connect(self._on_video_resolved)
        mgr = self._media_cache_manager
        mgr.cache_changed.connect(self._on_cache_changed)
        mgr.cache_removed.connect(self._on_cache_removed)
        mgr.prefetch_progress.connect(self._on_prefetch_progress)
        mgr.prefetch_error.connect(self._on_prefetch_error)

    def _save(self) -> bool:
        self._image_framing_save_timer.stop()
        self._image_framing_save_pending = False
        if not self._tree_key:
            return True
        if self._sync_enabled:
            try:
                self._materialize_current_nodes_for_sync()
            except OSError as exc:
                log_ignored_exception(
                    __name__,
                    "Could not materialize meeting sync files",
                )
                self._pause_sync_after_save_failure(str(exc))
            else:
                self._schedule_sync_manifest_save()
        return self._save_local_cache()

    def _save_local_cache(self) -> bool:
        if not self._tree_key:
            return True
        try:
            self._store.save(
                self._tree_key,
                self._nodes,
                self._canonical_hash,
                self._deleted_source_keys,
                self._linked_folder_files or None,
                self._meeting_folder_imports or None,
                self._current_overview(),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            log_ignored_exception(__name__, "Could not save meeting tree local cache")
            self.storageSaveFailed.emit(self._tree_key, str(exc))
            return False
        derived_timer = getattr(self, "_derived_media_save_timer", None)
        if derived_timer is not None:
            derived_timer.stop()
        derived_patches = getattr(self, "_derived_media_patches", None)
        if derived_patches is not None:
            derived_patches.clear()
        self.storageSaved.emit(self._tree_key)
        return True

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
        next_attempt = min(
            request.next_attempt_at for request in self._pending_sync_saves.values()
        )
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
            self._sync_service.save_tree,
            request.folder,
            request.identity,
            nodes=request.nodes,
            deleted_source_keys=request.deleted_source_keys,
            linked_folder_files=request.linked_folder_files,
            meeting_folder_imports=request.meeting_folder_imports,
            expected_revision=request.expected_revision,
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
                pending_framing = MeetingTreeController._pending_image_framing_state(
                    self
                )
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
        _clear_tree_data_cache(self)

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

    def _materialize_current_nodes_for_sync(self) -> None:
        if not self._sync_folder:
            return
        self._nodes, linked_files = self._sync_service.materialize_tree_files(
            self._nodes,
            Path(self._sync_folder),
            generated_roots=self._generated_asset_roots(),
        )
        self._linked_folder_files = linked_files

    def _linked_files_for_current_nodes(self) -> dict[str, str]:
        linked: dict[str, str] = {}
        for node in iter_nodes(self._nodes):
            if node.get("type") != "media" or not node.get("linked_folder_source"):
                continue
            node_id = str(node.get("id") or "")
            url = self._url_for_node(node)
            if node_id and url:
                linked[url] = node_id
        return linked

    def _prepare_nodes_for_sync(self, nodes: list[Node]) -> list[Node]:
        if not self._sync_enabled or not self._sync_folder or not nodes:
            return nodes
        prepared, linked_files = self._sync_service.materialize_tree_files(
            nodes,
            Path(self._sync_folder),
            generated_roots=self._generated_asset_roots(),
        )
        if linked_files:
            self._linked_folder_files.update(linked_files)
        return prepared

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
        nodes = self._prepare_nodes_for_sync(nodes)
        index = max(0, min(insert_index, len(target_children)))
        for offset, node in enumerate(nodes):
            target_children.insert(index + offset, node)
        _clear_tree_data_cache(self)
        saved = self._save()
        self._start_media_requests(nodes)
        self.chromeChanged.emit()
        qml_nodes = [self._qml_node(node) for node in nodes]
        if signal_name == "state":
            _emit_controller_state_changed(self)
        elif signal_name == "nodes":
            self.nodesInserted.emit(list_id, index, qml_nodes)
        else:
            self.mediaInserted.emit(list_id, index, qml_nodes)
        self._emit_section_counts()
        return saved

    def _children_for_target(self, kind: str, node_id: str) -> list[Node] | None:
        return children_for_tree_target(self._nodes, kind, node_id)

    def _parse_list_id(self, list_id: str) -> tuple[str, str]:
        return parse_tree_list_id(list_id)

    def _find_node(self, node_id: str, nodes: list[Node] | None = None) -> Node | None:
        return find_tree_node(self._nodes, node_id, nodes)

    def _replace_node(self, node_id: str, replacement: list[Node]) -> bool:
        return replace_tree_node(self._nodes, node_id, replacement)

    def _media_descendants(self, node: Node) -> list[Node]:
        return media_descendants(node)

    def _save_and_emit_replace(self, node_id: str, replacement: list[Node]) -> None:
        _clear_tree_data_cache(self)
        self._save()
        self.chromeChanged.emit()
        self.nodeReplaced.emit(node_id, [self._qml_node(node) for node in replacement])
        self._emit_section_counts()

    def _section_patch(self, node: Node | None) -> dict[str, Any]:
        if not node or node.get("type") not in ("section", "subsection"):
            return {}
        hue = int(node.get("color_hue", 215))
        colors = section_colors(hue, current_theme_scheme())
        return {
            "id": str(node.get("id", "")),
            "title": _display_section_title(node),
            "color": colors["accent"],
            "textColor": colors["text"],
            "badgeBg": colors["badge"],
            "itemCount": count_media(node.get("children", [])),
        }

    def _emit_section_changed(self, node: Node | None) -> None:
        patch = self._section_patch(node)
        if not patch:
            return
        self.sectionChanged.emit(
            patch["id"],
            patch["title"],
            patch["color"],
            patch["textColor"],
            patch["badgeBg"],
            patch["itemCount"],
        )

    def _remember_deleted_sources(self, node: Node, *, include_media: bool) -> None:
        if node.get("meeting_generated"):
            key = node.get("meeting_source_key")
            if key and (include_media or node.get("type") != "media"):
                self._deleted_source_keys.add(str(key))
        for child in node.get("children", []):
            self._remember_deleted_sources(child, include_media=include_media)

    def _qml_node(self, node: Node) -> Node:
        node_type = node.get("type", "")
        if node_type in ("section", "subsection"):
            hue = int(node.get("color_hue", 215))
            colors = section_colors(hue, current_theme_scheme())
            children = [self._qml_node(child) for child in node.get("children", [])]
            return {
                "id": node.get("id", ""),
                "type": node_type,
                "title": _display_section_title(node),
                "color": colors["accent"],
                "textColor": colors["text"],
                "badgeBg": colors["badge"],
                "collapsed": bool(node.get("collapsed", False)),
                "itemCount": count_media(node.get("children", [])),
                "children": children,
            }
        if node_type == "marker":
            return {
                "id": node.get("id", ""),
                "type": "marker",
                "text": node.get("text", ""),
                "children": [],
            }
        return self._media_qml_node(node)

    def _media_qml_node(self, node: Node) -> Node:
        item_id = node.get("id", "")
        ref = node.get("media_ref") or {}
        media_type = node.get("media_type") or self._media_type_from_ref(ref)
        url = self._url_for_node(node)
        cloud_visible, cloud_active, cloud_progress, cloud_tooltip = self._cloud_state(url)
        is_remote = MediaCacheManager.is_remote(url)
        is_missing = bool(url and not is_remote and not os.path.exists(url))
        cached_path = self._media_cache_manager.cached_path(url) if is_remote else None
        trim_source = (
            QUrl.fromLocalFile(cached_path).toString()
            if cached_path
            else url if is_remote
            else QUrl.fromLocalFile(os.path.abspath(url)).toString() if url else ""
        )
        return {
            "id": item_id,
            "type": "media",
            "title": node.get("title") or media_ref_title(ref) or _tr("_MediaRow", "Media"),
            "mediaType": media_type,
            "badge": self._badge_for(ref, media_type),
            "duration": self._duration_for(node),
            "thumbSource": self._display_thumb_source_for(node, item_id),
            "url": url,
            "trimSource": trim_source,
            "trimAvailable": bool(trim_source and not is_missing),
            "cloudVisible": cloud_visible,
            "cloudActive": cloud_active,
            "cloudProgress": cloud_progress,
            "cloudTooltip": cloud_tooltip,
            "isMissing": is_missing,
            "startTrimTicks": self._trim_ticks(node, "start_trim_ticks"),
            "endTrimTicks": self._trim_ticks(node, "end_trim_ticks"),
            "baseDurationTicks": self._trim_ticks(node, "base_duration_ticks"),
            "hasCustomTrim": bool(
                self._trim_ticks(node, "start_trim_ticks")
                or self._trim_ticks(node, "end_trim_ticks")
            ),
            "imageFraming": image_transform_to_record(
                image_transform_from_record(node.get("image_framing"))
            ),
            "children": [],
        }

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

    def _duration_for(self, node: Node) -> str:
        return format_effective_duration_ticks(
            self._duration_ticks(node),
            self._trim_ticks(node, "start_trim_ticks"),
            self._trim_ticks(node, "end_trim_ticks"),
        )

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
            return int(node.get("base_duration_ticks")
                       or (node.get("media_ref") or {}).get("base_duration_ticks")
                       or 0)
        except (TypeError, ValueError):
            return 0

    def _thumbnail_local_path(self, node: Node | None, item_id: str = "") -> str:
        item_id = item_id or str((node or {}).get("id", ""))
        storage_id = self._thumbnail_storage_id(node, item_id)
        stored = str((node or {}).get("thumbnail_local_path") or "")
        if not storage_id:
            return stored
        if stored and (
            not (node or {}).get("meeting_generated")
            or (node or {}).get("linked_folder_source")
            or Path(stored).name == self._meeting_thumbnail_store.path(storage_id).name
        ):
            return stored
        return os.fspath(self._meeting_thumbnail_store.path(storage_id))

    def _thumbnail_storage_id(self, node: Node | None, item_id: str = "") -> str:
        item_id = item_id or str((node or {}).get("id", ""))
        if not item_id or not (node or {}).get("meeting_generated"):
            return item_id
        return meeting_thumb_storage_id(self._tree_key, item_id)

    def _has_local_thumbnail(self, node: Node | None) -> bool:
        item_id = str((node or {}).get("id", ""))
        path = self._thumbnail_local_path(node, item_id)
        return bool(path and os.path.exists(path))

    def _save_thumbnail_for_node(self, node: Node, pixmap: QPixmap) -> str:
        item_id = str(node.get("id", ""))
        if not item_id or pixmap is None or pixmap.isNull():
            return ""
        storage_id = self._thumbnail_storage_id(node, item_id)
        try:
            if not save_thumbnail(self._meeting_thumbnail_store, storage_id, pixmap):
                return ""
        except Exception:  # noqa: BLE001 - Qt image codec boundary
            log_ignored_exception(__name__, "Could not save meeting thumbnail")
            return ""
        path = self._meeting_thumbnail_store.path(storage_id)
        node["thumbnail_cache_key"] = path.name
        node["thumbnail_local_path"] = os.fspath(path)
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

    def _thumb_source_for(self, item_id: str) -> str:
        node = self._find_node(item_id)
        version = self._thumb_versions.get(item_id, 0)
        thumb_path = self._thumbnail_local_path(node, item_id)
        if version > 0 or (thumb_path and os.path.exists(thumb_path)):
            return f"image://playlistthumbs/{item_id}/{version}"
        return ""

    def _display_thumb_source_for(self, node: Node | None, item_id: str = "") -> str:
        item_id = item_id or str((node or {}).get("id", ""))
        thumb_source = self._thumb_source_for(item_id)
        if thumb_source:
            return thumb_source
        ref = (node or {}).get("media_ref") or {}
        media_type = (node or {}).get("media_type") or self._media_type_from_ref(ref)
        if media_type != "image":
            return ""
        return _local_file_url(self._local_media_path(node))

    def _local_media_path(self, node: Node | None) -> str:
        url = self._url_for_node(node)
        if not url:
            return ""
        if MediaCacheManager.is_remote(url):
            return self._media_cache_manager.cached_path(url) or ""
        return url if os.path.exists(url) else ""

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
        path = _usable_ref_file_path(ref)
        if path:
            return path
        return ""

    def _cloud_state(self, url: str) -> tuple[bool, bool, float, str]:
        if not MediaCacheManager.is_remote(url):
            return False, False, -1.0, ""
        mgr = self._media_cache_manager
        if mgr.is_cached(url):
            return False, False, 1.0, ""
        active = mgr.is_prefetching(url)
        progress = self._cloud_progress_by_url.get(url, -1.0)
        if active and progress >= 0:
            tooltip = tr_offline_downloading_progress(int(round(progress * 100)))
        elif active:
            tooltip = tr_offline_downloading()
        else:
            tooltip = tr_offline_download()
        return True, active, progress, tooltip

    def _start_media_requests(self, nodes: list[Node] | None = None) -> None:
        media_nodes = [
            node for node in iter_nodes(self._nodes if nodes is None else nodes)
            if node.get("type") == "media"
        ]
        if not media_nodes:
            return
        self._media_request_queue.extend(media_nodes)
        if not self._media_request_timer.isActive():
            self._media_request_timer.start()

    def _drain_media_request_queue(self) -> None:
        batch_size = 2
        for _ in range(min(batch_size, len(self._media_request_queue))):
            self._start_media_request(self._media_request_queue.popleft())
        if self._media_request_queue:
            self._media_request_timer.start()
        else:
            self._media_request_timer.stop()

    def _start_media_request(self, node: Node) -> None:
        item_id = node.get("id", "")
        if not item_id:
            return
        ref = node.get("media_ref") or {}
        media_type = node.get("media_type") or self._media_type_from_ref(ref)
        url = self._url_for_node(node)
        local_path = self._local_media_path(node)
        has_thumb = self._has_local_thumbnail(node)
        has_duration = media_type == "image" or self._duration_ticks(node) > 0

        if media_type == "image":
            if local_path:
                self._emit_media_changed(item_id)
            if url:
                self._emit_cloud_for_node(item_id)
            return

        if url:
            self._emit_cloud_for_node(item_id)
            if local_path and (not has_thumb or not has_duration):
                self._queue_info(
                    item_id,
                    local_path,
                    media_type,
                    purpose="metadata",
                )
            elif not has_thumb:
                thumbnail_url = str(node.get("thumbnail_url") or "")
                if thumbnail_url:
                    self._queue_info(
                        item_id,
                        thumbnail_url,
                        "image",
                        purpose="thumb",
                    )
            return

        if (
            not node.get("meeting_generated")
            and (ref.get("key_symbol") or ref.get("meps_doc_id"))
        ):
            request_id = f"meetingtree:{item_id}:{uuid.uuid4().hex}"
            self._resolve_to_node_id[request_id] = item_id
            self._svc.resolve_video_async(request_id, meeting_media_from_ref(ref))

    def _queue_info(
        self,
        item_id: str,
        url: str,
        media_type: str,
        *,
        purpose: str = "metadata",
    ) -> None:
        request_key = (item_id, purpose)
        if request_key in self._active_info_requests:
            return
        token = self._next_token
        self._next_token += 1
        self._info_request_by_token[token] = request_key
        self._active_info_requests.add(request_key)
        self._info_queue.request(
            token,
            url,
            media_type,
            require_duration=(
                purpose == "metadata" and media_type in {"audio", "video"}
            ),
        )

    @Slot(str, str, str, str)
    def _on_video_resolved(self, request_id: str, url: str, title: str, thumb_url: str):
        item_id = self._resolve_to_node_id.pop(request_id, "")
        if not item_id:
            return
        node = self._find_node(item_id)
        if not node:
            return
        if url:
            if node.get("resolved_url") and node.get("resolved_url") != url:
                node.pop("base_duration_ticks", None)
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
        self._save()
        self._emit_media_changed(item_id)
        self._emit_cloud_for_node(item_id)
        self._start_media_requests([node])

    @Slot(int, object, str)
    def _on_info_ready(self, token: int, pixmap: QPixmap, title: str):
        request_key = self._info_request_by_token.pop(token, None)
        if request_key is None:
            return
        self._active_info_requests.discard(request_key)
        item_id, _purpose = request_key
        if not item_id:
            return
        node = self._find_node(item_id)
        if not node:
            return
        patch: dict[str, Any] = {}
        if pixmap and not pixmap.isNull():
            thumbnail = self._bounded_thumbnail(pixmap)
            self._thumb_cache[item_id] = thumbnail
            if path := self._save_thumbnail_for_node(node, thumbnail):
                patch["thumbnail_local_path"] = path
                patch["thumbnail_cache_key"] = str(
                    node.get("thumbnail_cache_key") or ""
                )
                self._thumb_versions[item_id] = (
                    self._thumb_versions.get(item_id, 0) + 1
                )
        if (
            title
            and self._should_accept_resolved_title(node)
            and str(node.get("title") or "") != title
        ):
            node["title"] = title
            node["auto_title"] = False
            node.setdefault("media_ref", {})["label"] = title
            patch.update({
                "title": title,
                "auto_title": False,
                "media_ref_label": title,
            })
        if patch:
            self._queue_derived_media_patch(node, patch)
            self._emit_media_changed(item_id)

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
        if not node:
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
        try:
            self._store.patch_media_batch(self._tree_key, patches)
        except (OSError, UnicodeError, ValueError) as exc:
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
            self.storageSaveFailed.emit(self._tree_key, str(exc))
            return False
        return True

    @Slot(str)
    def _on_cache_changed(self, url: str):
        self._cloud_progress_by_url.pop(url, None)
        self._emit_cloud_for_url(url)
        matching_nodes = [
            node
            for node in iter_nodes(self._nodes)
            if node.get("type") == "media" and self._url_for_node(node) == url
        ]
        self._start_media_requests(matching_nodes)

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
                self._emit_cloud_for_node(str(node.get("id", "")))

    @Slot(str, int, int)
    def _on_prefetch_progress(self, url: str, downloaded: int, total: int):
        if total > 0:
            self._cloud_progress_by_url[url] = downloaded / total
        self._emit_cloud_for_url(url)

    @Slot(str, str)
    def _on_prefetch_error(self, url: str, _message: str):
        self._cloud_progress_by_url.pop(url, None)
        self._emit_cloud_for_url(url)

    def _emit_media_changed(self, item_id: str) -> None:
        node = self._find_node(item_id)
        if not node:
            return
        self.mediaChanged.emit(
            item_id,
            str(node.get("title", "")),
            self._duration_for(node),
            self._display_thumb_source_for(node, item_id),
        )

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
        visible, active, progress, tooltip = self._cloud_state(self._url_for_node(node))
        self.cloudChanged.emit(item_id, visible, active, progress, tooltip)

    def _emit_section_counts(self) -> None:
        counts = {
            str(node.get("id", "")): count_media(node.get("children", []))
            for node in iter_nodes(self._nodes)
            if node.get("type") in ("section", "subsection")
        }
        self.sectionCountsChanged.emit(counts)
        self.chromeChanged.emit()
