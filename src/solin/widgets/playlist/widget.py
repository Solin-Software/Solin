"""
playlist_widget.py  ─ Solin
Drag-to-reorder completamente reescrito com container manual (sem QListWidget).
Thumbnails persistem através de reordenações; botões com SVG real.
"""
from __future__ import annotations

import copy, logging, os, random, time, uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QDialog, QMessageBox, QStackedWidget,
)
from PySide6.QtCore import (
    QObject, Signal, QTimer,
    QEvent, QUrl, Slot,
)
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtQuickWidgets import QQuickWidget

from solin.styles.theme import PALETTE, QML_THEME
from solin.controllers.playlist_transfer_workflow import PlaylistTransferWorkflow
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.playlist.bridge import PlaylistEditBridge
from solin.ui.qml.media_tree.playlist_session import PlaylistTreeSession
from solin.ui.qml.media_tree.state import MediaAvailability
from solin.ui.qml.playlist.visuals import (
    PlaylistThumbnailProvider,
    PlaylistIconProvider,
)
from solin.ui.qml.jw_media_catalog import JWMediaCatalogBridge
from solin.ui.qml.jw_songs import JWSongsBridge
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.foundation.resource_keys import (
    file_resource_key,
    child_folder_resource_claim,
)
from ...core.ingest.manifest import ManifestWriteError, retry_manifest_write
from ...core.ingest.local_files import LocalFileAvailabilitySignature
from ...core.foundation.qt_threads import stop_owned_qthread
from ...core.foundation.runtime_paths import ProfilePaths
from ...core.i18n.manager import LanguageManager
from ...core.jw.language_context import (
    JWMediaLanguageContext,
    jw_media_language_context,
)
from ...core.jw.identifiers import is_jw_url
from ...core.jw.songs import JWSongsStore
from ...ui.helpers import begin_qml_pointer_cursor, end_qml_pointer_cursor
from ...ui.media_insertion_feedback import connect_media_picker_feedback
from ...core.media.cache import MediaCacheManager
from ...core.media.formats import media_type_from_path
from ...core.media.identity import partition_media_items
from ...core.media.insertion import MediaInsertResult
from ...core.media.operations import MediaOperationPresentation, MediaOperationSpec
from ...core.media.thumbnail_identity import thumbnail_storage_id
from ...core.media.playback_request import MediaTrim
from ...core.playlists.items import looks_like_filename_title
from ...core.playlists.names import (
    PlaylistNameConflictError,
    PlaylistNameError,
    ensure_unique_playlist_name,
)
from ...core.projection.image_framing import (
    ImageTransform,
    image_transform_from_record,
    image_transform_to_record,
    prepare_image_transform_for_aspect,
)
from ...core.playlists.tree_editing import insert_playlist_media
from ...ui.media_info import MediaInfoQueue
from .drag_drop import PlaylistDragDropMixin
from .edit_actions import PlaylistEditActionsMixin
from .import_export import PlaylistEditImportMixin
from .list_view import PlaylistListView
from ...core.meetings.colors import APP_BASE_HUE, generate_section_hue
from .dialogs import HuePickerDialog, NameDialog
from ...core.playlists.storage import (
    PlaylistStoragePaths,
)
_THUMB_W, _THUMB_H = 70, 46
_ITEM_H            = 77   # altura fixa de cada item
_MANIFEST_SAVE_DEBOUNCE_MS = 180
_MANIFEST_SAVE_RETRY_DELAYS_MS = (100, 250, 500, 1_000, 2_000, 5_000, 10_000, 15_000)
_MANIFEST_SAVE_RETRY_BUDGET_SECONDS = 60.0
_WATCHED_FOLDER_REFRESH_RETRY_DELAYS_MS = (
    1_000,
    3_000,
    10_000,
    30_000,
    90_000,
    300_000,
)


@dataclass(slots=True)
class _PendingPlaylistManifestSave:
    folder_path: str
    playlist: dict
    generation: int
    next_attempt_at: float
    first_attempt_at: float | None = None
    retry_index: int = 0


@dataclass(frozen=True, slots=True)
class _ThumbnailRequestIntent:
    thumbnail: bool
    title: bool
    duration: bool
    source_signature: str = ""


@dataclass(frozen=True, slots=True)
class _WatchedFolderSnapshot:
    playlist: dict
    availability: LocalFileAvailabilitySignature
    pending_files: tuple[str, ...]


log = logging.getLogger(__name__)

__all__ = (
    "PlaylistEditView",
    "PlaylistWidget",
)

if TYPE_CHECKING:
    from ...core.ingest.watched_folder import WatchedFolderWatcher
    from ...core.ingest.watched_folder_files import WatchedFolderFileStore
    from ...core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
    from ...core.jw.catalog_service import JWMediaCatalogService
    from ...core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from ...core.jw.thumbnail_fetch import JWCatalogThumbnailSessionFactory
    from ...core.media.profile_store import ProfileMediaStore
    from ...core.media.thumbnail_store import ThumbnailStore
    from ...ui.notifications import NotificationCenter
    from ...core.playlists.storage import PlaylistRepository
    from ...core.playlists.cleanup import PlaylistCleanupQueue
    from ...core.rendering.document_conversion import DocumentConversionService

# ── Tela de edição ─────────────────────────────────────────────────────────────

class PlaylistEditView(
    PlaylistEditActionsMixin,
    PlaylistDragDropMixin,
    PlaylistEditImportMixin,
    QWidget,
):
    back_requested = Signal()
    project_items  = Signal(list, int, str)
    save_temp_as_permanent = Signal(str, dict)  # name, playlist data
    export_requested = Signal(str)
    import_jwl_requested = Signal(object, int, str)
    _manifestSaveCompleted = Signal(str, int, object)
    _watchedFolderRefreshCompleted = Signal(int, str, object, object)

    def __init__(
        self,
        lang: LanguageManager,
        media_ctrl=None,
        *,
        notifications: NotificationCenter,
        playback_protection,
        profile_paths: ProfilePaths,
        document_conversion_service: DocumentConversionService,
        storage_paths: PlaylistStoragePaths,
        playlist_repository: PlaylistRepository,
        profile_media_store: ProfileMediaStore,
        jwpub_import_thread_factory: JwpubImportThreadFactory,
        playlist_thumbnail_store: ThumbnailStore,
        watched_folder_file_store: WatchedFolderFileStore,
        watched_folder_playlist_store: WatchedFolderPlaylistStore,
        media_cache_manager: MediaCacheManager,
        media_tree_runtime,
        jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
        jw_catalog_thumbnail_session_factory: JWCatalogThumbnailSessionFactory,
        jw_songs_store: JWSongsStore,
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        all_playlists: list[dict],
        schedule_cleanup: Callable[[list[dict]], None],
        projection_aspect_ratio_provider: Callable[[], object] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.lang = lang
        self._media_ctrl = media_ctrl
        self._notifications = notifications
        self._playback_protection = playback_protection
        self._profile_paths = profile_paths
        self._document_conversion_service = document_conversion_service
        self._storage_paths = storage_paths
        self._playlist_repository = playlist_repository
        self._profile_media_store = profile_media_store
        self._jwpub_import_thread_factory = jwpub_import_thread_factory
        self._playlist_thumbnail_store = playlist_thumbnail_store
        self._watched_folder_file_store = watched_folder_file_store
        self._watched_folder_playlist_store = watched_folder_playlist_store
        self._media_cache_manager = media_cache_manager
        self._media_tree_runtime = media_tree_runtime
        self._jw_songs_store = jw_songs_store
        self._all_playlists = all_playlists
        self._schedule_cleanup = schedule_cleanup
        self._pl: Optional[dict] = None
        self._is_temp: bool = False
        self._is_watched: bool = False          # linked folder mode
        self._watched_path: str = ""            # physical subfolder path
        self._wf_sync_thread: object = None
        self._id_to_thumb:     dict[str, QPixmap] = {}   # cache de thumbnails por ID
        self._thumb_idx_to_id: dict[int, str] = {}       # request token → item ID
        self._thumb_idx_to_source: dict[int, str] = {}   # request token → source URL
        self._thumb_idx_to_intent: dict[int, _ThumbnailRequestIntent] = {}
        self._thumb_pending_item_ids: set[str] = set()
        self._thumb_request_token: int = 0
        self._thumb_scan_items: list[dict] = []
        self._thumb_scan_index: int = 0
        self._qml_pointer_depth = 0
        self._thumb_queue = media_info_queue_factory(self)
        self._thumb_queue.info_ready.connect(self._on_info)
        self._thumb_queue.duration_ready.connect(self._on_duration_from_extractor)
        self._thumb_queue.request_failed.connect(self._on_thumbnail_failed)
        self._thumb_scan_timer = QTimer(self)
        self._thumb_scan_timer.setSingleShot(True)
        self._thumb_scan_timer.setInterval(0)
        self._thumb_scan_timer.timeout.connect(self._scan_missing_thumbnails_batch)
        self._image_framing_save_pending = False
        self._image_framing_save_timer = QTimer(self)
        self._image_framing_save_timer.setSingleShot(True)
        self._image_framing_save_timer.setInterval(200)
        self._image_framing_save_timer.timeout.connect(
            self._flush_image_framing_save
        )
        self._pending_manifest_saves: dict[str, _PendingPlaylistManifestSave] = {}
        self._manifest_state_generation = 0
        self._manifest_save_inflight: tuple[str, int] | None = None
        self._manifest_save_future: Future[None] | None = None
        self._manifest_save_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="playlist-manifest",
        )
        self._watched_folder_refresh_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="linked-folder-refresh",
        )
        self._manifestSaveCompleted.connect(self._on_manifest_save_completed)
        self._manifest_save_timer = QTimer(self)
        self._manifest_save_timer.setSingleShot(True)
        self._manifest_save_timer.timeout.connect(self._drain_manifest_saves)
        self._watchedFolderRefreshCompleted.connect(
            self._on_watched_folder_refresh_completed
        )
        self._pdf_threads:  list[object] = []
        self._lo_threads:   list[object] = []
        self._wf_refresh_pending: bool = False      # deferred refresh flag
        self._wf_refresh_superseded = False
        self._wf_refresh_generation = 0
        self._wf_refresh_manifest_generation = 0
        self._wf_refresh_inflight: tuple[int, str] | None = None
        self._wf_refresh_future: Future[_WatchedFolderSnapshot] | None = None
        self._wf_refresh_shutdown = False
        self._wf_file_availability: LocalFileAvailabilitySignature = ()
        self._wf_refresh_retry_index = 0
        self._wf_refresh_retry_timer = QTimer(self)
        self._wf_refresh_retry_timer.setSingleShot(True)
        self._wf_refresh_retry_timer.timeout.connect(self.refresh_watched_folder)

        # QML Integration
        self._tree_session = PlaylistTreeSession(
            media_tree_runtime,
            self._playlist_thumbnail_store.path,
            thumbnail_ready=self._accept_probed_thumbnail,
            parent=self,
        )
        media_tree_runtime.registry.stateChanged.connect(
            self._on_presentation_state_changed
        )
        media_tree_runtime.snapshots.writeFailed.connect(
            self._on_playlist_snapshot_write_failed
        )
        media_tree_runtime.snapshots.writeCompleted.connect(
            self._on_playlist_snapshot_write_completed
        )
        self._playlist_save_failure_notified = False
        self.model = self._tree_session.model
        self.bridge = PlaylistEditBridge(
            projection_aspect_ratio_provider,
            self._image_source_aspect_ratio,
            parent=self,
        )
        self.bridge.attach_session(self._tree_session)
        self.catalog_bridge = JWMediaCatalogBridge(
            jw_catalog_service_factory,
            jw_catalog_thumbnail_session_factory,
            insertion_handler=self._on_jw_media_confirmed,
            parent=self,
        )
        self.songs_bridge = JWSongsBridge(
            jw_songs_store,
            insertion_handler=self._on_jw_media_confirmed,
            parent=self,
        )

        self._apply_media_language_context()
        self._connect_media_language_signal()

        self._build_ui()
        self._connect_bridge_signals()
        self._connect_cache_signals()

    def _current_media_context(self) -> JWMediaLanguageContext:
        return jw_media_language_context(self.lang)

    def playlist_export_snapshot(self, _playlist_format: str) -> dict | None:
        """Return a stable UI-thread snapshot ready for a transfer worker."""
        if not self._pl:
            return None
        self._flush_image_framing_save()
        snapshot = copy.deepcopy(self._pl)
        snapshot.pop("_temp", None)
        return snapshot

    @property
    def current_playlist_id(self) -> str:
        return str(self._pl.get("id") or "") if self._pl else ""

    def _apply_media_language_context(self) -> None:
        context = self._current_media_context()
        self.catalog_bridge.set_language_code(context.api_code)
        self.songs_bridge.set_language_context(
            api_code=context.api_code,
            fallback_code=context.fallback_code,
            is_sign_language=context.is_sign_language,
        )

    def _connect_media_language_signal(self) -> None:
        svc = getattr(self.lang, "jw_lang_service", None) if self.lang else None
        if svc is None:
            return
        try:
            svc.media_language_changed.connect(self._on_media_language_changed)
        except TypeError:
            pass

    @Slot(str)
    def _on_media_language_changed(self, _code: str) -> None:
        """Keep JW media integrations in sync with Settings immediately."""
        self._apply_media_language_context()

    def cleanup(self) -> None:
        """Stop background work owned by the edit view before teardown."""
        self._wf_refresh_shutdown = True
        owner_id = self._tree_session.owner_id
        if owner_id:
            self._media_tree_runtime.operations.cancel_scope(owner_id)
        self._tree_session.close()
        self._wf_refresh_retry_timer.stop()
        self._flush_image_framing_save()
        self._manifest_save_timer.stop()
        refresh_future = self._wf_refresh_future
        if refresh_future is not None:
            refresh_future.cancel()
        self._watched_folder_refresh_executor.shutdown(
            wait=False,
            cancel_futures=True,
        )
        for request in self._pending_manifest_saves.values():
            pending = copy.deepcopy(request)
            self._media_tree_runtime.snapshots.request(
                f"playlist-manifest:{os.path.normcase(os.path.abspath(pending.folder_path))}",
                lambda current=pending: retry_manifest_write(
                    lambda: self._watched_folder_playlist_store.save_playlist(
                        current.folder_path,
                        current.playlist,
                    )
                ),
                conflict_key=child_folder_resource_claim(pending.folder_path),
            )
        if self._manifest_save_future is not None:
            self._manifest_save_future.cancel()
        self._manifest_save_executor.shutdown(wait=False, cancel_futures=True)
        self._pending_manifest_saves.clear()
        self._thumb_queue.shutdown()
        self._thumb_scan_timer.stop()
        self._thumb_scan_items.clear()
        self._stop_owned_thread(self._wf_sync_thread, wait_ms=10_000)
        self._wf_sync_thread = None
        for threads in (self._pdf_threads, self._lo_threads):
            for thread in list(threads):
                self._stop_owned_thread(thread, wait_ms=3_000)
            threads.clear()
        try:
            self.catalog_bridge.cleanup()
        except Exception:  # noqa: BLE001 - widget cleanup boundary
            log_ignored_exception(__name__, "Could not cleanup playlist catalog bridge")
        try:
            self.songs_bridge.cleanup()
        except Exception:  # noqa: BLE001 - widget cleanup boundary
            log_ignored_exception(__name__, "Could not cleanup playlist songs bridge")
        svc = getattr(self.lang, "jw_lang_service", None) if self.lang else None
        if svc is not None:
            try:
                svc.media_language_changed.disconnect(self._on_media_language_changed)
            except (RuntimeError, TypeError):
                pass

    @staticmethod
    def _stop_owned_thread(thread, *, wait_ms: int) -> None:
        stop_owned_qthread(thread, wait_ms=wait_ms, logger=log)

    # ── Centralized save dispatch ──────────────────────────────────────────
    def _save(self) -> None:
        """Save playlist state: to playlists.json for normal, to manifest for watched folders."""
        self._image_framing_save_timer.stop()
        self._image_framing_save_pending = False
        if self._is_temp:
            return
        if self._is_watched and self._pl is not None and self._watched_path:
            self._schedule_manifest_save(self._watched_path, self._pl)
        else:
            snapshot = copy.deepcopy(self._all_playlists)
            self._media_tree_runtime.snapshots.request(
                f"playlists:{self._playlist_repository.path}",
                lambda: self._playlist_repository.save_strict(snapshot),
                conflict_key=file_resource_key(self._playlist_repository.path),
            )

    def _playlist_snapshot_key(self) -> str:
        return f"playlists:{self._playlist_repository.path}"

    @Slot(str, int, str)
    def _on_playlist_snapshot_write_failed(
        self,
        key: str,
        _generation: int,
        message: str,
    ) -> None:
        if key != self._playlist_snapshot_key():
            return
        log.error("Could not persist playlist snapshot: %s", message)
        if not self._playlist_save_failure_notified:
            self._playlist_save_failure_notified = True
            self._notifications.error(
                self.tr("Could not save playlist changes. Retrying automatically.")
            )

    @Slot(str, int)
    def _on_playlist_snapshot_write_completed(
        self,
        key: str,
        _generation: int,
    ) -> None:
        if key == self._playlist_snapshot_key():
            self._playlist_save_failure_notified = False

    def _schedule_manifest_save(self, folder_path: str, playlist: dict) -> None:
        self._manifest_state_generation += 1
        if self._wf_refresh_inflight is not None:
            self._wf_refresh_superseded = True
            self._wf_refresh_pending = True
        key = os.path.normcase(os.path.abspath(folder_path))
        previous = self._pending_manifest_saves.get(key)
        self._pending_manifest_saves[key] = _PendingPlaylistManifestSave(
            folder_path=folder_path,
            playlist=copy.deepcopy(playlist),
            generation=(previous.generation + 1) if previous else 1,
            next_attempt_at=time.monotonic() + _MANIFEST_SAVE_DEBOUNCE_MS / 1000,
            first_attempt_at=previous.first_attempt_at if previous else None,
            retry_index=previous.retry_index if previous else 0,
        )
        self._arm_manifest_save_timer()

    def _arm_manifest_save_timer(self) -> None:
        if self._manifest_save_inflight is not None:
            self._manifest_save_timer.stop()
            return
        if not self._pending_manifest_saves:
            self._manifest_save_timer.stop()
            return
        next_attempt = min(
            request.next_attempt_at for request in self._pending_manifest_saves.values()
        )
        delay_ms = max(0, int((next_attempt - time.monotonic()) * 1000))
        self._manifest_save_timer.start(delay_ms)

    def _drain_manifest_saves(self) -> None:
        if self._manifest_save_inflight is not None:
            return
        if not self._pending_manifest_saves:
            return
        now = time.monotonic()
        key, request = min(
            self._pending_manifest_saves.items(),
            key=lambda item: item[1].next_attempt_at,
        )
        if request.next_attempt_at > now:
            self._arm_manifest_save_timer()
            return
        if request.first_attempt_at is None:
            request.first_attempt_at = now
        generation = request.generation
        self._manifest_save_inflight = (key, generation)
        self._manifest_save_timer.stop()
        future = self._manifest_save_executor.submit(
            self._media_tree_runtime.resource_lanes.run,
            child_folder_resource_claim(request.folder_path),
            lambda: self._watched_folder_playlist_store.save_playlist(
                request.folder_path,
                request.playlist,
            ),
        )
        self._manifest_save_future = future
        future.add_done_callback(
            lambda completed, save_key=key, save_generation=generation: (
                self._emit_manifest_save_completed(
                    save_key,
                    save_generation,
                    completed,
                )
            )
        )

    def _emit_manifest_save_completed(
        self,
        key: str,
        generation: int,
        future: Future[None],
    ) -> None:
        try:
            future.result()
        except BaseException as exc:  # noqa: BLE001 - worker-to-Qt exception boundary
            self._manifestSaveCompleted.emit(key, generation, exc)
        else:
            self._manifestSaveCompleted.emit(key, generation, None)

    @Slot(str, int, object)
    def _on_manifest_save_completed(
        self,
        key: str,
        generation: int,
        error: BaseException | None,
    ) -> None:
        self._manifest_save_inflight = None
        self._manifest_save_future = None
        request = self._pending_manifest_saves.get(key)
        if request is None:
            self._arm_manifest_save_timer()
            return
        now = time.monotonic()
        is_latest = request.generation == generation

        if error is None:
            if is_latest:
                self._pending_manifest_saves.pop(key, None)
            else:
                request.next_attempt_at = now
            current_key = (
                os.path.normcase(os.path.abspath(self._watched_path))
                if self._watched_path
                else ""
            )
            if is_latest and key == current_key and self._wf_refresh_pending:
                self._wf_refresh_pending = False
                QTimer.singleShot(0, self.refresh_watched_folder)
        elif not is_latest:
            request.next_attempt_at = now
        elif isinstance(error, ManifestWriteError):
            elapsed = now - (request.first_attempt_at or now)
            if error.retryable and elapsed < _MANIFEST_SAVE_RETRY_BUDGET_SECONDS:
                delay = _MANIFEST_SAVE_RETRY_DELAYS_MS[
                    min(
                        request.retry_index,
                        len(_MANIFEST_SAVE_RETRY_DELAYS_MS) - 1,
                    )
                ]
                request.retry_index += 1
                retry_delay = int(delay * random.uniform(0.8, 1.2))
                request.next_attempt_at = now + retry_delay / 1000
                log.warning(
                    "Playlist manifest publish is busy; retry %d in %d ms: %s",
                    request.retry_index,
                    retry_delay,
                    error.path,
                )
            elif self._warn_manifest_save_failed(request.folder_path, str(error)):
                request.first_attempt_at = None
                request.retry_index = 0
                request.next_attempt_at = now
            else:
                self._pending_manifest_saves.pop(key, None)
        else:
            if self._warn_manifest_save_failed(request.folder_path, str(error)):
                request.first_attempt_at = None
                request.retry_index = 0
                request.next_attempt_at = now
            else:
                self._pending_manifest_saves.pop(key, None)

        self._arm_manifest_save_timer()

    def _warn_manifest_save_failed(self, folder_path: str, message: str) -> bool:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(self.tr("Linked folder"))
        dialog.setText(self.tr("Could not update the linked folder."))
        dialog.setInformativeText(
            self.tr("Try again or open the linked folder to check its sync status.")
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
            return True
        if result == QMessageBox.StandardButton.Open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder_path))
        return False

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.qml_widget = QQuickWidget(self)
        self.qml_widget.installEventFilter(self)
        configure_qml_host(
            self.qml_widget,
            type_name="PlaylistEditView",
            clear_color=QML_THEME["mediaPlaceholder"],
            image_providers={
                "playlistthumbs": PlaylistThumbnailProvider(self._id_to_thumb),
                "playlisticons": PlaylistIconProvider(),
            },
            context_properties={
                "playlistModel": self.model,
                "controller": self.bridge,
                "catalogBridge": self.catalog_bridge,
                "songsBridge": self.songs_bridge,
                "playbackProtection": self._playback_protection,
            },
            mouse_tracking=True,
        )

        root.addWidget(self.qml_widget, stretch=1)

        # Enable Drag and Drop
        self.setAcceptDrops(True)
        self.qml_widget.setAcceptDrops(False)

    def _accept_probed_thumbnail(
        self,
        item_id: str,
        image: QImage | None,
    ) -> None:
        if image is None or image.isNull():
            self._id_to_thumb.pop(item_id, None)
            return
        self._id_to_thumb[item_id] = QPixmap.fromImage(image)

    def _connect_bridge_signals(self):
        self.bridge.backRequested.connect(self._on_back)
        self.bridge.addRequested.connect(self._add_dialog)
        self.bridge.exportRequested.connect(self._export)
        self.bridge.saveTempRequested.connect(self._save_temp_playlist)
        self.bridge.playAllRequested.connect(self._do_play_all)
        self.bridge.shuffleRequested.connect(self._do_shuffle)
        self.bridge.newSectionRequested.connect(self._create_section)
        self.bridge.newSubsectionRequested.connect(self._create_subsection)
        self.bridge.newMarkerRequested.connect(self._create_marker)

        self.bridge.projectItemSignal.connect(self._project_by_id)
        self.bridge.removeItemSignal.connect(self._remove_item)
        self.bridge.renameItemSignal.connect(self._rename_item)
        self.bridge.downloadItemSignal.connect(self._download_item)
        self.bridge.operationCancelRequested.connect(
            self._media_tree_runtime.operations.cancel
        )
        self.bridge.operationRetryRequested.connect(
            self._media_tree_runtime.operations.retry
        )
        self.bridge.imageFramingSetRequested.connect(self._set_image_framing)
        self.bridge.imageFramingResetRequested.connect(self._reset_image_framing)
        self.bridge.mediaTrimSetRequested.connect(self._set_media_trim)
        self.bridge.renameMarkerSignal.connect(self._rename_marker)
        self.bridge.deleteMarkerSignal.connect(self._delete_marker)

        self.bridge.renameSectionSignal.connect(self._rename_section)
        self.bridge.deleteSectionSignal.connect(self._delete_section)
        self.bridge.recolorSectionSignal.connect(self._recolor_section)

        self.bridge.collapseSectionSignal.connect(self._toggle_section_collapse)
        self.bridge.dragFinished.connect(self._save)
        self.bridge.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.bridge.pointerExited.connect(self.end_qml_pointer_cursor)

        # Catalog bridge connections
        connect_media_picker_feedback(self.catalog_bridge, self._notifications)
        connect_media_picker_feedback(self.songs_bridge, self._notifications)

    @Slot(str, float, float, float, float, float, bool)
    def _set_image_framing(
        self,
        item_id: str,
        zoom: float,
        norm_x: float,
        norm_y: float,
        source_width: float,
        source_height: float,
        snap_zoom_to_cover: bool,
    ) -> None:
        if not self._pl:
            return
        item = next(
            (candidate for candidate in self._pl.get("items", [])
             if candidate.get("id") == item_id),
            None,
        )
        if not item or item.get("type") != "image":
            return
        source_aspect_ratio = self._image_source_aspect_ratio(item_id)
        if source_aspect_ratio > 0.0:
            source_width = source_aspect_ratio
            source_height = 1.0
        current_transform = image_transform_from_record(item.get("image_framing"))
        transform = prepare_image_transform_for_aspect(
            source_width,
            source_height,
            self.bridge.imageFramingAspectRatio(),
            current_transform,
            ImageTransform(zoom, norm_x, norm_y),
            snap_zoom_to_cover=snap_zoom_to_cover,
        )
        record = image_transform_to_record(transform)
        current = image_transform_to_record(current_transform)
        if record == current:
            return
        if record is None:
            item.pop("image_framing", None)
        else:
            item["image_framing"] = record
        self._tree_session.refresh(probe_changed_sources=False)
        self._schedule_image_framing_save()

    def _image_source_aspect_ratio(self, item_id: str) -> float:
        return float(
            self._media_tree_runtime.registry.state(
                self._tree_session.owner_id,
                item_id,
            ).image_aspect_ratio
        )

    @Slot(str)
    def _reset_image_framing(self, item_id: str) -> None:
        if not self._pl:
            return
        item = next(
            (candidate for candidate in self._pl.get("items", [])
             if candidate.get("id") == item_id),
            None,
        )
        if not item or "image_framing" not in item:
            return
        item.pop("image_framing", None)
        self._tree_session.refresh(probe_changed_sources=False)
        self._schedule_image_framing_save()

    @Slot(str, float, float, float)
    def _set_media_trim(
        self,
        item_id: str,
        start_ms: float,
        end_ms: float,
        duration_ms: float,
    ) -> None:
        if not self._pl:
            return
        item = next(
            (candidate for candidate in self._pl.get("items", [])
             if candidate.get("id") == item_id),
            None,
        )
        if not item or item.get("type") not in {"audio", "video"}:
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
            if field != "base_duration_ticks" and value == 0:
                item.pop(field, None)
            else:
                item[field] = value
        self._tree_session.refresh(probe_changed_sources=False)
        self.bridge.stateChanged.emit()
        self._save()

    def _schedule_image_framing_save(self) -> None:
        self._image_framing_save_pending = True
        self._image_framing_save_timer.start()

    def _flush_image_framing_save(self) -> None:
        if not self._image_framing_save_pending:
            return
        self._image_framing_save_timer.stop()
        self._save()

    def _toggle_section_collapse(self, section_id: str) -> None:
        section = self._section_by_id(section_id)
        if section is None:
            return
        section["collapsed"] = not bool(section.get("collapsed", False))
        self._tree_session.refresh(probe_changed_sources=False)
        self._save()

    def _connect_cache_signals(self):
        self._media_cache_manager.cache_changed.connect(self._on_cache_changed)
        self._media_cache_manager.cache_removed.connect(self._on_cache_removed)
        self._media_cache_manager.prefetch_progress.connect(self._on_prefetch_progress)
        self._media_cache_manager.prefetch_error.connect(self._on_prefetch_error)

    def _disconnect_cache_signals(self):
        try:
            self._media_cache_manager.cache_changed.disconnect(self._on_cache_changed)
            self._media_cache_manager.cache_removed.disconnect(self._on_cache_removed)
            self._media_cache_manager.prefetch_progress.disconnect(self._on_prefetch_progress)
            self._media_cache_manager.prefetch_error.disconnect(self._on_prefetch_error)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect playlist cache signals")

    def _on_cache_changed(self, url: str):
        self._tree_session.request_source(url)

    def _on_cache_removed(self, url: str):
        self._tree_session.request_source(url)

    def _on_prefetch_progress(self, url: str, downloaded: int, total: int):
        if total > 0:
            self._tree_session.patch_source_progress(url, downloaded, total)

    def _on_prefetch_error(self, url: str, _message: str):
        self._tree_session.request_source(url)

    def _download_item(self, item_id: str):
        if not self._pl:
            return
        for item in self._pl.get("items", []):
            if item.get("id") == item_id:
                url = item.get("url", "")
                state = self._media_tree_runtime.registry.state(
                    self._tree_session.owner_id,
                    item_id,
                )
                if url and not state.cached:
                    self._media_cache_manager.prefetch(url, priority=True)
                    self._tree_session.request_source(url)
                break

    # ── Carga ──────────────────────────────────────────────────────────────

    def load_playlist(self, pl: dict):
        self._flush_image_framing_save()
        self._reset_watched_folder_refresh_retry()
        self._wf_refresh_pending = False
        self._is_watched = False
        self._watched_path = ""
        self._pl = pl
        self._is_temp = bool(pl.get("_temp"))
        self._thumb_queue.clear()
        self._thumb_scan_timer.stop()
        self._thumb_scan_items.clear()
        self._id_to_thumb.clear()
        self._thumb_idx_to_id.clear()
        self._thumb_idx_to_source.clear()
        self._thumb_idx_to_intent.clear()
        self._thumb_pending_item_ids.clear()
        self._reconcile_playlist()

    def load_watched_folder(self, folder_path: str):
        """Open a linked folder without reading cloud-backed files on the Qt thread."""
        self._flush_image_framing_save()
        self._reset_watched_folder_refresh_retry()
        self._wf_refresh_pending = False
        self._is_watched = True
        self._watched_path = folder_path
        self._is_temp = False
        key = os.path.normcase(os.path.abspath(folder_path))
        pending = self._pending_manifest_saves.get(key)
        self._thumb_queue.clear()
        self._thumb_scan_timer.stop()
        self._thumb_scan_items.clear()
        self._id_to_thumb.clear()
        self._thumb_idx_to_id.clear()
        self._thumb_idx_to_source.clear()
        self._thumb_idx_to_intent.clear()
        self._thumb_pending_item_ids.clear()
        self._wf_file_availability = ()

        if pending is not None:
            self._pl = copy.deepcopy(pending.playlist)
            self._wf_refresh_pending = True
            self._reconcile_playlist()
            return

        self._pl = None
        loading_playlist = {
            "id": f"linked:{key}",
            "name": os.path.basename(os.path.normpath(folder_path)) or folder_path,
            "items": [],
            "sections": [],
            "markers": [],
        }
        self.catalog_bridge.set_playlist_ref(loading_playlist)
        self._tree_session.activate(loading_playlist)
        self.bridge.set_state(
            name=loading_playlist["name"],
            is_watched=True,
            is_loading=True,
            item_count=0,
            item_word=self.tr("items"),
        )
        self.refresh_watched_folder()

    def _start_wf_sync(self, pending_files: tuple[str, ...] | None = None):
        """Start background sync for pending processable files in linked folder."""
        if not self._watched_path:
            return
        if pending_files is None:
            raise ValueError("Linked-folder sync requires a background snapshot")
        pending = list(pending_files)
        if not pending:
            return
        context = self._current_media_context()
        thread = self._watched_folder_playlist_store.create_sync_thread(
            self._watched_path,
            media_lang=context.api_code,
            fallback_lang_code=context.fallback_code,
            parent=self,
        )
        self._wf_sync_thread = thread
        @thread.sync_complete.connect
        def _on_done():
            self._wf_sync_thread = None
            if self._wf_refresh_pending:
                self._wf_refresh_pending = False
                self.refresh_watched_folder()
            else:
                self.refresh_watched_folder()
        @thread.sync_failed.connect
        def _on_err(err):
            self._wf_sync_thread = None
            self._notifications.error(str(err)[:160])
        thread.finished.connect(lambda: setattr(self, '_wf_sync_thread', None))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _watched_playlist_equivalent(self, other: dict) -> bool:
        """Return True when a watched-folder refresh is only our own persisted state."""
        if not self._pl:
            return False

        def normalize_url(url: str) -> str:
            if not isinstance(url, str) or not url:
                return ""
            if MediaCacheManager.is_remote(url):
                return url
            return os.path.normcase(os.path.normpath(os.path.abspath(url)))

        def normalize_value(value, key: str = ""):
            if key == "url":
                return normalize_url(value)
            if isinstance(value, dict):
                return {
                    k: normalize_value(v, k)
                    for k, v in sorted(value.items())
                    if not k.startswith("_")
                }
            if isinstance(value, list):
                return [normalize_value(v) for v in value]
            return value

        def payload(pl: dict) -> dict:
            return {
                "items": normalize_value(pl.get("items", [])),
                "sections": normalize_value(pl.get("sections", [])),
                "markers": normalize_value(pl.get("markers", [])),
            }

        return payload(self._pl) == payload(other)

    def _availability_changed_item_ids(
        self,
        playlist: dict,
        availability: LocalFileAvailabilitySignature,
    ) -> tuple[str, ...]:
        """Return existing media whose machine-local availability changed."""

        previous = dict(self._wf_file_availability)
        current = dict(availability)
        changed: list[str] = []
        for item in playlist.get("items", []):
            item_id = str(item.get("id") or "")
            url = str(item.get("url") or "")
            if not item_id or not url or MediaCacheManager.is_remote(url):
                continue
            path_key = os.path.normcase(
                os.path.normpath(os.path.abspath(url))
            )
            if previous.get(path_key) != current.get(path_key):
                changed.append(item_id)
        return tuple(changed)

    def refresh_watched_folder(self):
        """Schedule a linked-folder snapshot without blocking the Qt thread."""
        if self._wf_refresh_shutdown:
            return
        self._flush_image_framing_save()
        if not self._is_watched or not self._watched_path:
            return
        key = os.path.normcase(os.path.abspath(self._watched_path))
        if (
            key in self._pending_manifest_saves
            or self._wf_sync_thread is not None
            or self._wf_refresh_inflight is not None
        ):
            self._wf_refresh_pending = True
            return
        self._wf_refresh_pending = False
        self._wf_refresh_generation += 1
        generation = self._wf_refresh_generation
        folder_path = self._watched_path
        self._wf_refresh_superseded = False
        self._wf_refresh_manifest_generation = self._manifest_state_generation
        self._wf_refresh_inflight = (generation, key)
        future = self._watched_folder_refresh_executor.submit(
            self._read_watched_folder_snapshot,
            folder_path,
        )
        self._wf_refresh_future = future
        future.add_done_callback(
            lambda completed, current_generation=generation, current_key=key: (
                self._emit_watched_folder_refresh_completed(
                    current_generation,
                    current_key,
                    completed,
                )
            )
        )

    def _reset_watched_folder_refresh_retry(self) -> None:
        self._wf_refresh_retry_timer.stop()
        self._wf_refresh_retry_index = 0

    def _schedule_watched_folder_refresh_retry(self) -> None:
        if self._wf_refresh_shutdown or not self._is_watched:
            return
        delay = _WATCHED_FOLDER_REFRESH_RETRY_DELAYS_MS[
            min(
                self._wf_refresh_retry_index,
                len(_WATCHED_FOLDER_REFRESH_RETRY_DELAYS_MS) - 1,
            )
        ]
        self._wf_refresh_retry_index += 1
        self._wf_refresh_retry_timer.start(delay)

    def supersede_watched_folder_refresh(self) -> None:
        """Reject an in-flight snapshot as soon as a newer disk event arrives."""
        if self._wf_refresh_inflight is not None:
            self._wf_refresh_superseded = True

    def _read_watched_folder_snapshot(
        self,
        folder_path: str,
    ) -> _WatchedFolderSnapshot:
        def read() -> _WatchedFolderSnapshot:
            playlist = self._watched_folder_playlist_store.load_playlist(folder_path)
            availability = (
                self._watched_folder_playlist_store.file_availability_signature(
                    item.get("url", "")
                    for item in playlist.get("items", [])
                )
            )
            pending_files = tuple(
                self._watched_folder_playlist_store.pending_files(folder_path)
            )
            return _WatchedFolderSnapshot(
                playlist=playlist,
                availability=availability,
                pending_files=pending_files,
            )

        return self._media_tree_runtime.resource_lanes.run(
            child_folder_resource_claim(folder_path),
            read,
        )

    def _emit_watched_folder_refresh_completed(
        self,
        generation: int,
        key: str,
        future: Future[_WatchedFolderSnapshot],
    ) -> None:
        if self._wf_refresh_shutdown:
            return
        try:
            snapshot = future.result()
        except BaseException as exc:  # noqa: BLE001 - worker-to-Qt boundary
            self._watchedFolderRefreshCompleted.emit(
                generation,
                key,
                None,
                exc,
            )
        else:
            self._watchedFolderRefreshCompleted.emit(
                generation,
                key,
                snapshot,
                None,
            )

    @Slot(int, str, object, object)
    def _on_watched_folder_refresh_completed(
        self,
        generation: int,
        key: str,
        snapshot: _WatchedFolderSnapshot | None,
        error: BaseException | None,
    ) -> None:
        if self._wf_refresh_inflight != (generation, key):
            return
        self._wf_refresh_inflight = None
        self._wf_refresh_future = None

        save_generation_changed = (
            self._wf_refresh_manifest_generation
            != self._manifest_state_generation
        )
        if (
            self._wf_refresh_superseded
            or self._wf_refresh_pending
            or save_generation_changed
        ):
            should_refresh_now = (
                self._wf_refresh_pending or save_generation_changed
            )
            self._wf_refresh_superseded = False
            self._wf_refresh_pending = False
            if should_refresh_now:
                QTimer.singleShot(0, self.refresh_watched_folder)
            return

        current_key = (
            os.path.normcase(os.path.abspath(self._watched_path))
            if self._watched_path
            else ""
        )
        can_apply = (
            self._is_watched
            and key == current_key
            and key not in self._pending_manifest_saves
            and self._wf_sync_thread is None
        )
        if error is not None:
            log.warning(
                "Could not refresh linked-folder snapshot",
                exc_info=(type(error), error, error.__traceback__),
            )
            if can_apply:
                self._schedule_watched_folder_refresh_retry()
        elif snapshot is not None and can_apply:
            self._reset_watched_folder_refresh_retry()
            self._apply_watched_folder_snapshot(snapshot)
        elif snapshot is not None and key == current_key:
            self._wf_refresh_pending = True

        if self._wf_refresh_pending and self._wf_refresh_inflight is None:
            self._wf_refresh_pending = False
            QTimer.singleShot(0, self.refresh_watched_folder)

    def _apply_watched_folder_snapshot(
        self,
        snapshot: _WatchedFolderSnapshot,
    ) -> None:
        pl = snapshot.playlist
        availability = snapshot.availability
        availability_changed_ids = self._availability_changed_item_ids(
            pl,
            availability,
        )
        for item_id in availability_changed_ids:
            self._cancel_thumbnail_requests_for_item(item_id)
        if self._watched_playlist_equivalent(pl):
            if availability_changed_ids:
                self._pl = pl
                self._wf_file_availability = availability
                self._publish_tree_snapshot()
                self._tree_session.request_nodes(set(availability_changed_ids))
                self._sync_playlist_chrome(emit_data_changed=False)
                QTimer.singleShot(0, self._request_missing_thumbnails)
            self._start_wf_sync(snapshot.pending_files)
            return
        self._reconcile_thumbnail_requests(pl.get("items", []))
        self._pl = pl
        self._wf_file_availability = availability
        self._publish_tree_snapshot()
        if availability_changed_ids:
            self._tree_session.request_nodes(set(availability_changed_ids))
        self._sync_playlist_chrome(emit_data_changed=False)
        QTimer.singleShot(0, self._request_missing_thumbnails)
        self._start_wf_sync(snapshot.pending_files)

    def _reconcile_playlist(self) -> None:
        if not self._pl:
            return

        self.catalog_bridge.set_playlist_ref(self._pl)
        self._apply_media_language_context()

        # Fix types
        items = self._pl.get("items", [])
        sections = self._pl.get("sections", [])
        subsection_ids = {
            section.get("id") for section in sections
            if section.get("parent_id")
        }
        needs_save = False
        for item in items:
            detected = media_type_from_path(
                item.get("url", ""),
                default="video",
            )
            if item.get("type") == "video" and detected != "video":
                item["type"] = detected
                needs_save = True
            url = item.get("url", "")
            if (not item.get("auto_title")
                    and url.startswith(("http://", "https://"))
                    and is_jw_url(url)
                    and " | " in item.get("title", "")):
                item["auto_title"] = True
                needs_save = True
        markers = self._pl.get("markers")
        if markers:
            valid_markers = [
                marker for marker in markers
                if marker.get("subsection_id") in subsection_ids
            ]
            if len(valid_markers) != len(markers):
                self._pl["markers"] = valid_markers
                needs_save = True
        if needs_save:
            self._save()

        self._publish_tree_snapshot()

        self._sync_playlist_chrome()

        # Defer thumbnail work until QML has reconciled the snapshot. Local
        # images and embedded audio covers can resolve synchronously, so doing
        # this before reconciliation can emit a media patch before the QML node
        # exists.
        QTimer.singleShot(0, self._request_missing_thumbnails)

    def _request_missing_thumbnails(self) -> None:
        if not self._pl:
            return
        self._thumb_scan_items = list(self._pl.get("items", []))
        self._thumb_scan_index = 0
        if self._thumb_scan_items:
            self._thumb_scan_timer.start()

    def _publish_tree_snapshot(self) -> None:
        """Publish current playlist data to the backing model."""

        if not self._pl:
            return
        if self._tree_session.playlist is self._pl:
            self._tree_session.refresh()
        elif self._tree_session.owner_id == f"playlist:{self._pl.get('id', '')}":
            self._tree_session.update_playlist(self._pl)
        else:
            self._tree_session.activate(self._pl)

    def _scan_missing_thumbnails_batch(self) -> None:
        if not self._pl:
            self._thumb_scan_timer.stop()
            self._thumb_scan_items.clear()
            return

        batch_size = 32
        end = min(self._thumb_scan_index + batch_size, len(self._thumb_scan_items))
        for item in self._thumb_scan_items[self._thumb_scan_index:end]:
            self._request_missing_thumbnail_for_item(item)
        self._thumb_scan_index = end

        if self._thumb_scan_index >= len(self._thumb_scan_items):
            self._thumb_scan_timer.stop()
            self._thumb_scan_items.clear()
        else:
            self._thumb_scan_timer.start()

    def _request_missing_thumbnail_for_item(self, item: dict) -> None:
        intent = self._thumbnail_request_intent(item)
        if not any((intent.thumbnail, intent.title, intent.duration)):
            return
        self._request_thumbnail(
            item["id"],
            str(item.get("url") or ""),
            str(item.get("type") or "video"),
            require_thumbnail=intent.thumbnail,
            require_title=intent.title,
            require_duration=intent.duration,
        )

    def _thumbnail_request_intent(self, item: dict) -> _ThumbnailRequestIntent:
        item_id = item["id"]
        media_type = item.get("type", "video")
        state = self._media_tree_runtime.registry.state(
            self._tree_session.owner_id,
            item_id,
        )
        probe_ready = state.availability == MediaAvailability.AVAILABLE
        needs_title = (
            probe_ready
            and media_type in {"audio", "video"}
            and bool(item.get("auto_title", False))
        )
        duration_ticks = item.get("base_duration_ticks")
        has_duration = (
            isinstance(duration_ticks, int)
            and not isinstance(duration_ticks, bool)
            and duration_ticks > 0
        )
        needs_duration = (
            probe_ready
            and media_type in {"audio", "video"}
            and not has_duration
        )

        cached = self._id_to_thumb.get(item_id)
        has_thumbnail = bool(
            (cached is not None and not cached.isNull())
            or state.thumbnail_source
            or media_type == "image" and state.local_path
        )
        return _ThumbnailRequestIntent(
            thumbnail=probe_ready and not has_thumbnail,
            title=needs_title,
            duration=needs_duration,
            source_signature=state.source_signature,
        )

    @Slot(str, str)
    def _on_presentation_state_changed(self, owner_id: str, item_id: str) -> None:
        if owner_id != self._tree_session.owner_id or self._pl is None:
            return
        item = next(
            (
                candidate
                for candidate in self._pl.get("items", [])
                if str(candidate.get("id") or "") == item_id
            ),
            None,
        )
        if item is not None:
            self._request_missing_thumbnail_for_item(item)

    def _reconcile_thumbnail_requests(self, items: list[dict]) -> None:
        """Keep in-flight work when its media identity is unchanged."""

        current_items = {
            str(item.get("id") or ""): item
            for item in items
            if item.get("id")
        }
        for token, item_id in tuple(self._thumb_idx_to_id.items()):
            requested_source = self._thumb_idx_to_source.get(token, "")
            current_item = current_items.get(item_id)
            active_intent = self._thumb_idx_to_intent.get(token)
            desired_intent = (
                self._thumbnail_request_intent(current_item)
                if current_item is not None
                else None
            )
            if (
                current_item is not None
                and active_intent is not None
                and desired_intent is not None
                and any(
                    (
                        desired_intent.thumbnail,
                        desired_intent.title,
                        desired_intent.duration,
                    )
                )
                and PlaylistEditView._thumbnail_intent_covers(
                    active_intent,
                    desired_intent,
                )
                and self._same_media_source(
                    requested_source,
                    str(current_item.get("url") or ""),
                )
            ):
                continue
            self._thumb_queue.invalidate(token)
            self._retire_thumbnail_request(token)
            if current_item is not None and desired_intent is not None and any(
                (
                    desired_intent.thumbnail,
                    desired_intent.title,
                    desired_intent.duration,
                )
            ):
                self._request_thumbnail(
                    item_id,
                    str(current_item.get("url") or ""),
                    str(current_item.get("type") or "video"),
                    require_thumbnail=desired_intent.thumbnail,
                    require_title=desired_intent.title,
                    require_duration=desired_intent.duration,
                )

    @staticmethod
    def _thumbnail_intent_covers(
        active: _ThumbnailRequestIntent,
        desired: _ThumbnailRequestIntent,
    ) -> bool:
        return (
            (not desired.thumbnail or active.thumbnail)
            and (not desired.title or active.title)
            and (not desired.duration or active.duration)
            and active.source_signature == desired.source_signature
        )

    def _retire_thumbnail_request(
        self,
        token: int,
    ) -> tuple[str | None, str, _ThumbnailRequestIntent | None]:
        item_id = self._thumb_idx_to_id.pop(token, None)
        source_url = self._thumb_idx_to_source.pop(token, "")
        intent = self._thumb_idx_to_intent.pop(token, None)
        if item_id is not None and item_id not in self._thumb_idx_to_id.values():
            self._thumb_pending_item_ids.discard(item_id)
        return item_id, source_url, intent

    def _cancel_thumbnail_requests_for_item(self, item_id: str) -> None:
        """Stop queued, active, and retrying work owned by a removed item."""

        for token, requested_item_id in tuple(self._thumb_idx_to_id.items()):
            if requested_item_id != item_id:
                continue
            self._thumb_queue.invalidate(token)
            self._retire_thumbnail_request(token)

    def _thumbnail_result_is_current(
        self,
        item_id: str,
        source_url: str,
        source_signature: str,
    ) -> bool:
        if not self._pl:
            return False
        item = next(
            (
                candidate
                for candidate in self._pl.get("items", [])
                if candidate.get("id") == item_id
                and self._same_media_source(
                    str(candidate.get("url") or ""),
                    source_url,
                )
            ),
            None,
        )
        if item is None:
            return False
        if MediaCacheManager.is_remote(source_url):
            return True
        state = self._media_tree_runtime.registry.state(
            self._tree_session.owner_id,
            item_id,
        )
        return bool(
            state.availability == MediaAvailability.AVAILABLE
            and state.source_signature == source_signature
        )

    @staticmethod
    def _same_media_source(first: str, second: str) -> bool:
        first_remote = MediaCacheManager.is_remote(first)
        second_remote = MediaCacheManager.is_remote(second)
        if first_remote or second_remote:
            return first_remote and second_remote and first == second
        return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
            os.path.abspath(second)
        )

    def _request_thumbnail(
        self,
        item_id: str,
        url: str,
        media_type: str,
        *,
        require_thumbnail: bool = False,
        require_title: bool = False,
        require_duration: bool = False,
    ) -> None:
        if not (require_thumbnail or require_title or require_duration):
            return
        requested_intent = _ThumbnailRequestIntent(
            thumbnail=require_thumbnail,
            title=require_title,
            duration=require_duration,
            source_signature=self._media_tree_runtime.registry.state(
                self._tree_session.owner_id,
                item_id,
            ).source_signature,
        )
        active_tokens = [
            token
            for token, requested_item_id in self._thumb_idx_to_id.items()
            if requested_item_id == item_id
        ]
        if active_tokens:
            matching_source = all(
                self._same_media_source(
                    self._thumb_idx_to_source.get(token, ""),
                    url,
                )
                for token in active_tokens
            )
            active_intents = [
                self._thumb_idx_to_intent.get(token)
                for token in active_tokens
            ]
            if (
                matching_source
                and all(intent is not None for intent in active_intents)
                and all(
                    PlaylistEditView._thumbnail_intent_covers(
                        intent,
                        requested_intent,
                    )
                    for intent in active_intents
                    if intent is not None
                )
            ):
                return
            if matching_source:
                for active_intent in active_intents:
                    if active_intent is None:
                        continue
                    requested_intent = _ThumbnailRequestIntent(
                        thumbnail=(
                            requested_intent.thumbnail or active_intent.thumbnail
                        ),
                        title=requested_intent.title or active_intent.title,
                        duration=requested_intent.duration or active_intent.duration,
                        source_signature=requested_intent.source_signature,
                    )
            for token in active_tokens:
                self._thumb_queue.invalidate(token)
                self._retire_thumbnail_request(token)
        self._thumb_request_token += 1
        token = self._thumb_request_token
        self._thumb_idx_to_id[token] = item_id
        self._thumb_idx_to_source[token] = url
        self._thumb_idx_to_intent[token] = requested_intent
        self._thumb_pending_item_ids.add(item_id)
        self._thumb_queue.request(
            token,
            url,
            media_type,
            require_thumbnail=requested_intent.thumbnail,
            require_title=requested_intent.title,
            require_duration=requested_intent.duration,
            restart_on_source_change=False,
        )

    def _on_info(self, idx: int, pixmap: QPixmap, title: str):
        item_id, source_url, intent = self._retire_thumbnail_request(idx)
        self._thumb_queue.invalidate(idx)
        if item_id is None or intent is None:
            return
        if not self._thumbnail_result_is_current(
            item_id,
            source_url,
            intent.source_signature,
        ):
            return

        current_item = next(
            (
                item
                for item in (self._pl or {}).get("items", [])
                if item.get("id") == item_id
            ),
            None,
        )
        if current_item is None:
            return
        desired_intent = self._thumbnail_request_intent(current_item)
        intent = _ThumbnailRequestIntent(
            thumbnail=intent.thumbnail and desired_intent.thumbnail,
            title=intent.title and desired_intent.title,
            duration=intent.duration and desired_intent.duration,
            source_signature=intent.source_signature,
        )

        media_changed = False
        if intent.thumbnail and pixmap and not pixmap.isNull():
            self._id_to_thumb[item_id] = pixmap
            self._media_tree_runtime.thumbnails.save_image(
                owner_id=self._tree_session.owner_id,
                node_id=item_id,
                storage_id=thumbnail_storage_id(
                    item_id,
                    str(current_item.get("url") or ""),
                ),
                store=self._playlist_thumbnail_store,
                image=pixmap.toImage(),
                source_signature=intent.source_signature,
            )
            self._media_tree_runtime.registry.patch(
                self._tree_session.owner_id,
                item_id,
                thumbnail_source=f"image://playlistthumbs/{item_id}",
            )
            media_changed = True

        if intent.title and self._pl:
            for item in self._pl.get("items", []):
                if item.get("id") != item_id:
                    continue
                current = item.get("title", "")
                title_changed = bool(title) and (
                    looks_like_filename_title(current)
                    or item.get("auto_title")
                ) and title != current
                auto_title_resolved = bool(title) and bool(item.get("auto_title"))
                if title_changed:
                    item["title"] = title
                if auto_title_resolved:
                    item["auto_title"] = False
                if title_changed or auto_title_resolved:
                    self._save()
                if title_changed:
                    media_changed = True
                break

        if media_changed:
            self._tree_session.refresh(probe_changed_sources=False)

    def _on_thumbnail_failed(self, idx: int, failure: object) -> None:
        item_id = self._thumb_idx_to_id.get(idx, "")
        self._retire_thumbnail_request(idx)
        if item_id and getattr(failure, "code", "") == "source-changed":
            self._tree_session.request_nodes({item_id})

    def _on_duration_from_extractor(self, idx: int, dur_ms: int) -> None:
        item_id = self._thumb_idx_to_id.get(idx)
        source_url = self._thumb_idx_to_source.get(idx, "")
        intent = self._thumb_idx_to_intent.get(idx)
        if (
            item_id
            and intent is not None
            and self._thumbnail_result_is_current(
                item_id,
                source_url,
                intent.source_signature,
            )
        ):
            self.notify_duration(item_id, dur_ms)

    def notify_duration(self, item_id: str, duration_ms: int) -> None:
        if not self._pl or duration_ms <= 0:
            return
        ticks = duration_ms * 10_000
        for item in self._pl.get("items", []):
            if item.get("id") == item_id:
                if not item.get("base_duration_ticks"):
                    item["base_duration_ticks"] = ticks
                    self._save()
                    self._media_tree_runtime.registry.patch(
                        self._tree_session.owner_id,
                        item_id,
                        duration_ticks=ticks,
                    )
                    self._tree_session.refresh(probe_changed_sources=False)
                break

    def _sync_order(self):
        self._save()

    def _media_ids_in_tree_node(self, node: dict) -> list[str]:
        """Return media ids in the visual order of a tree node."""
        if not node:
            return []
        if node.get("type") == "media":
            return [node.get("id", "")]
        media_ids: list[str] = []
        for child in node.get("children", []):
            media_ids.extend(self._media_ids_in_tree_node(child))
        return [item_id for item_id in media_ids if item_id]

    def _section_by_id(self, section_id: str) -> dict | None:
        if not self._pl:
            return None
        return next(
            (
                section for section in self._pl.get("sections", [])
                if section.get("id") == section_id
            ),
            None,
        )

    def _create_section(self):
        if not self._pl: return
        dlg = NameDialog(lang=self.lang, parent=self, label=self.tr("Section name:"), placeholder=self.tr("E.g.: Introduction"))
        dlg.setWindowTitle(self.tr("New Section"))
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        name = dlg.get_name()
        if not name: return
        sections = self._pl.setdefault("sections", [])
        existing_hues = [s.get("color_hue", 0) for s in sections]
        hue = generate_section_hue(existing_hues)
        sec = {
            "id": str(uuid.uuid4()),
            "name": name,
            "color_hue": hue,
            "collapsed": False,
        }
        sections.append(sec)
        self._save()
        self._publish_tree_snapshot()
        self._sync_playlist_chrome(emit_data_changed=False)

    def _create_subsection(self, parent_section_id: str):
        if not self._pl: return
        dlg = NameDialog(lang=self.lang, parent=self, label=self.tr("Subsection name:"), placeholder=self.tr("E.g.: Part 1"))
        dlg.setWindowTitle(self.tr("New Subsection"))
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        name = dlg.get_name()
        if not name: return
        sections = self._pl.setdefault("sections", [])
        sub = {
            "id": str(uuid.uuid4()),
            "name": name,
            "parent_id": parent_section_id,
            "collapsed": False,
        }
        sections.append(sub)
        self._save()
        self._publish_tree_snapshot()
        self._sync_playlist_chrome(emit_data_changed=False)

    def _create_marker(self, subsection_id: str) -> None:
        if not self._pl:
            return
        subsection = next(
            (s for s in self._pl.get("sections", [])
             if s.get("id") == subsection_id and s.get("parent_id")),
            None,
        )
        if not subsection:
            return
        was_collapsed = bool(subsection.get("collapsed", False))
        if was_collapsed:
            subsection["collapsed"] = False
        markers = self._pl.setdefault("markers", [])
        marker = {
            "id": str(uuid.uuid4()),
            "text": "",
            "subsection_id": subsection_id,
            "position": 0,
            "slot_order": 0,
        }
        markers.append(marker)
        self._publish_tree_snapshot()
        self._save()
        self._sync_playlist_chrome(emit_data_changed=False)
        self.bridge.markerEditRequested.emit(marker["id"])

    def _rename_marker(self, marker_id: str, text: str) -> None:
        if not self._pl:
            return
        marker = next(
            (m for m in self._pl.get("markers", [])
             if m.get("id") == marker_id),
            None,
        )
        if not marker:
            return
        marker["text"] = text
        self._save()
        self._tree_session.refresh(probe_changed_sources=False)

    def _delete_marker(self, marker_id: str) -> None:
        if not self._pl:
            return
        markers = self._pl.get("markers", [])
        if not any(m.get("id") == marker_id for m in markers):
            return
        self._pl["markers"] = [
            marker for marker in markers
            if marker.get("id") != marker_id
        ]
        self._save()
        self._tree_session.refresh(probe_changed_sources=False)
        self._sync_playlist_chrome(emit_data_changed=False)

    def _rename_section(self, sec_id: str):
        if not self._pl: return
        sections = self._pl.get("sections", [])
        sec = next((s for s in sections if s["id"] == sec_id), None)
        if not sec: return
        dlg = NameDialog(sec.get("name", ""), lang=self.lang, parent=self, label=self.tr("Section name:"), placeholder=self.tr("E.g.: Introduction"))
        dlg.setWindowTitle(self.tr("Rename section"))
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        name = dlg.get_name()
        if not name: return
        sec["name"] = name
        self._save()
        self._tree_session.refresh(probe_changed_sources=False)

    def _delete_section(self, sec_id: str):
        """Delete a section/subsection but keep its items."""
        if not self._pl: return
        sections = self._pl.get("sections", [])
        sec = next((s for s in sections if s["id"] == sec_id), None)
        if not sec: return
        reply = QMessageBox.question(
            self, self.tr("Delete section"),
            self.tr('Delete section "{name}"?\nItems inside will be kept.').replace(
                "{name}", sec.get("name", "")
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes: return
        parent_id = sec.get("parent_id") or None
        is_subsection = bool(parent_id)
        replacement_section_id = parent_id if is_subsection else None

        sections.remove(sec)
        for item in self._pl.get("items", []):
            if item.get("section_id") == sec_id:
                item["section_id"] = replacement_section_id

        child_sections = [s for s in sections if s.get("parent_id") == sec_id]
        if is_subsection:
            for child in child_sections:
                child["parent_id"] = parent_id
            self._pl["markers"] = [
                marker for marker in self._pl.get("markers", [])
                if marker.get("subsection_id") != sec_id
            ]
        else:
            child_ids = {s["id"] for s in child_sections}
            for item in self._pl.get("items", []):
                if item.get("section_id") in child_ids:
                    item["section_id"] = None
            for child in child_sections:
                sections.remove(child)
            removed_section_ids = child_ids | {sec_id}
            self._pl["markers"] = [
                marker for marker in self._pl.get("markers", [])
                if marker.get("subsection_id") not in removed_section_ids
            ]
        self._save()
        self._publish_tree_snapshot()
        self._sync_playlist_chrome(emit_data_changed=False)

    def _recolor_section(self, sec_id: str):
        """Open hue picker to choose section color."""
        if not self._pl: return
        sections = self._pl.get("sections", [])
        sec = next((s for s in sections if s["id"] == sec_id), None)
        if not sec: return
        current_hue = sec.get("color_hue", APP_BASE_HUE)
        dlg = HuePickerDialog(current_hue, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        sec["color_hue"] = dlg.selected_hue()
        self._save()
        self._tree_session.refresh(probe_changed_sources=False)

    def _remove_item(self, item_id: str):
        if not self._pl: return
        pending = self._tree_session.pending_item(item_id)
        if pending is not None:
            operation = self._tree_session.operation_for(item_id)
            if operation is not None:
                self._media_tree_runtime.operations.cancel(operation.operation_id)
                self._media_tree_runtime.operations.discard(operation.operation_id)
            self._tree_session.remove_pending(item_id)
            self._sync_playlist_chrome(emit_data_changed=False)
            return
        item = next((it for it in self._pl.get("items", []) if it["id"] == item_id), None)
        if item and self._media_ctrl is not None:
            item_type = item.get("type", "video")
            if item_type in ("video", "audio"):
                item_url   = item.get("url", "")
                cur_url    = self._media_ctrl.current_url or ""
                cur_local  = self._media_ctrl.local_path or ""
                playing    = item_url and item_url in (cur_url, cur_local)
                if not playing and cur_local and item_url:
                    playing = os.path.normpath(item_url) == os.path.normpath(cur_local)
                if playing:
                    QMessageBox.warning(
                        self,
                        self.tr("Media is playing"),
                        self.tr('Cannot remove "{title}" while it is currently playing.\nStop the projection and try again.')
                                    .replace("{title}", str(item.get("title", item_url))),
                    )
                    return
        self._pl["items"] = [it for it in self._pl["items"] if it["id"] != item_id]
        self._cancel_thumbnail_requests_for_item(item_id)
        self._save()
        if self._is_watched and self._watched_path and item:
            self._remove_watched_item(self._watched_path, item)
        elif item:
            self._schedule_cleanup([item])
        self._publish_tree_snapshot()
        self._sync_playlist_chrome(emit_data_changed=False)

    def _remove_watched_item(self, folder_path: str, item: dict) -> None:
        operation_id = f"linked-remove:{uuid.uuid4().hex}"
        item_snapshot = copy.deepcopy(item)

        def run(_progress, _cancellation):
            return self._watched_folder_playlist_store.remove_item(
                folder_path,
                item_snapshot,
            )

        def failed(message: str, _retryable: bool) -> None:
            if self._warn_manifest_save_failed(folder_path, message):
                QTimer.singleShot(
                    0,
                    lambda: self._remove_watched_item(folder_path, item_snapshot),
                )

        self._media_tree_runtime.operations.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=self._tree_session.owner_id or "playlist:inactive",
                operation_type="linked_folder_remove",
                conflict_key=child_folder_resource_claim(folder_path),
                presentation=MediaOperationPresentation.BACKGROUND,
                runner=run,
                commit=lambda _value: None,
                priority=100,
                failed=failed,
            )
        )

    def _rename_item(self, item_id: str):
        if not self._pl: return
        item = next((it for it in self._pl.get("items", []) if it["id"] == item_id), None)
        if not item: return
        dlg = NameDialog(item.get("title", ""), lang=self.lang, parent=self)
        dlg.setWindowTitle(self.tr("Rename media"))
        if dlg.exec() and dlg.get_name():
            title = dlg.get_name()
            item["title"] = title
            item["auto_title"] = False
            self._save()
            self._tree_session.refresh(probe_changed_sources=False)

    # ── i18n ──────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._apply_media_language_context()
        if hasattr(self, "qml_widget"):
            engine = self.qml_widget.engine()
            if hasattr(engine, "retranslate"):
                engine.retranslate()
        if self._pl:
            self._reconcile_playlist()

    def apply_theme(self) -> None:
        apply_qml_theme(self.qml_widget, clear_color=PALETTE.media_placeholder)
        if self._pl:
            self._publish_tree_snapshot()
            self._sync_playlist_chrome()
        else:
            self.bridge.stateChanged.emit()

    def begin_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth += 1
        begin_qml_pointer_cursor(self.qml_widget)

    def end_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth = max(0, self._qml_pointer_depth - 1)
        if self._qml_pointer_depth == 0:
            end_qml_pointer_cursor(self.qml_widget)

    def _reset_qml_pointer_cursor(self) -> None:
        self._qml_pointer_depth = 0
        end_qml_pointer_cursor(self.qml_widget)

# ── Widget principal ───────────────────────────────────────────────────────────

class PlaylistWidget(QWidget):
    project_video_signal = Signal(str, str, object, str)
    project_image_signal = Signal(bytes)

    def __init__(
        self,
        lang: LanguageManager,
        media_ctrl=None,
        watched_folder: str = "",
        *,
        notifications: NotificationCenter,
        playback_protection,
        profile_paths: ProfilePaths,
        document_conversion_service: DocumentConversionService,
        storage_paths: PlaylistStoragePaths,
        playlist_repository: PlaylistRepository,
        profile_media_store: ProfileMediaStore,
        jwpub_import_thread_factory: JwpubImportThreadFactory,
        playlist_thumbnail_store: ThumbnailStore,
        watched_folder_file_store: WatchedFolderFileStore,
        watched_folder_playlist_store: WatchedFolderPlaylistStore,
        watched_folder_watcher_factory: Callable[[QObject], WatchedFolderWatcher],
        playlist_cleanup_queue_factory: Callable[..., PlaylistCleanupQueue],
        media_cache_manager: MediaCacheManager,
        media_tree_runtime,
        jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
        jw_catalog_thumbnail_session_factory: JWCatalogThumbnailSessionFactory,
        jw_songs_store: JWSongsStore,
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        projection_aspect_ratio_provider: Callable[[], object] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.lang        = lang
        self._media_ctrl = media_ctrl
        self._notifications = notifications
        self._playback_protection = playback_protection
        self._profile_paths = profile_paths
        self._document_conversion_service = document_conversion_service
        self._storage_paths = storage_paths
        self._playlist_repository = playlist_repository
        self._profile_media_store = profile_media_store
        self._jwpub_import_thread_factory = jwpub_import_thread_factory
        self._playlist_thumbnail_store = playlist_thumbnail_store
        self._watched_folder_file_store = watched_folder_file_store
        self._watched_folder_playlist_store = watched_folder_playlist_store
        self._watched_folder_watcher_factory = watched_folder_watcher_factory
        self._media_cache_manager = media_cache_manager
        self._media_tree_runtime = media_tree_runtime
        self._jw_catalog_service_factory = jw_catalog_service_factory
        self._jw_catalog_thumbnail_session_factory = (
            jw_catalog_thumbnail_session_factory
        )
        self._jw_songs_store = jw_songs_store
        self._media_info_queue_factory = media_info_queue_factory
        self._projection_aspect_ratio_provider = projection_aspect_ratio_provider
        self._cleanup_queue = playlist_cleanup_queue_factory(
            storage_paths,
            self._playlist_thumbnail_store,
        )
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.setSingleShot(True)
        self._cleanup_timer.setInterval(750)
        self._cleanup_timer.timeout.connect(self._cleanup_queue.flush)
        self._playlists = self._playlist_repository.load()
        self._watched_folder = watched_folder
        self._build_ui()
        self._setup_watcher()

    # ── Build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._stack = QStackedWidget(self)

        # Index 0: list view
        self._list_view = PlaylistListView(
            self._playlists, self.lang,
            media_ctrl=self._media_ctrl,
            watched_folder=self._watched_folder,
            persist_playlists=self._persist_playlists,
            watched_folder_file_store=self._watched_folder_file_store,
            watched_folder_playlist_store=self._watched_folder_playlist_store,
            media_tree_runtime=self._media_tree_runtime,
            schedule_cleanup=self._schedule_cleanup,
            parent=self,
        )
        # Index 1: playlist edit (also used for watched folders)
        self._edit_view = PlaylistEditView(
            self.lang,
            media_ctrl=self._media_ctrl,
            notifications=self._notifications,
            playback_protection=self._playback_protection,
            profile_paths=self._profile_paths,
            document_conversion_service=self._document_conversion_service,
            storage_paths=self._storage_paths,
            playlist_repository=self._playlist_repository,
            profile_media_store=self._profile_media_store,
            jwpub_import_thread_factory=self._jwpub_import_thread_factory,
            playlist_thumbnail_store=self._playlist_thumbnail_store,
            watched_folder_file_store=self._watched_folder_file_store,
            watched_folder_playlist_store=self._watched_folder_playlist_store,
            media_cache_manager=self._media_cache_manager,
            media_tree_runtime=self._media_tree_runtime,
            jw_catalog_service_factory=self._jw_catalog_service_factory,
            jw_catalog_thumbnail_session_factory=(
                self._jw_catalog_thumbnail_session_factory
            ),
            jw_songs_store=self._jw_songs_store,
            media_info_queue_factory=self._media_info_queue_factory,
            projection_aspect_ratio_provider=self._projection_aspect_ratio_provider,
            all_playlists=self._playlists,
            schedule_cleanup=self._schedule_cleanup,
            parent=self,
        )

        self._stack.addWidget(self._list_view)   # 0
        self._stack.addWidget(self._edit_view)   # 1
        root.addWidget(self._stack)

        self._list_view.open_playlist.connect(self._open_playlist)
        self._list_view.open_watched_folder.connect(self._open_watched_folder)
        self._list_view.import_requested.connect(self._on_playlist_import_requested)
        self._list_view.export_playlist_requested.connect(
            self._on_playlist_export_requested
        )
        self._list_view.export_watched_folder_requested.connect(
            self._on_watched_folder_export_requested
        )
        self._edit_view.back_requested.connect(self._go_back)
        self._edit_view.project_items.connect(self._on_project_items)
        self._edit_view.save_temp_as_permanent.connect(self._on_save_temp_playlist)
        self._edit_view.export_requested.connect(self._on_edit_export_requested)
        self._edit_view.import_jwl_requested.connect(
            self._on_edit_jwl_import_requested
        )

        self._playlist_transfers = PlaylistTransferWorkflow(
            parent=self,
            notifications=self._notifications,
            language_manager=self.lang,
            playlists=self._playlists,
            playlist_repository=self._playlist_repository,
            profile_paths=self._profile_paths,
            profile_media_store=self._profile_media_store,
            media_cache_manager=self._media_cache_manager,
            refresh_playlists=self._list_view.refresh,
            open_playlist=self._open_playlist,
        )

    def _setup_watcher(self):
        self._folder_watcher = self._watched_folder_watcher_factory(self)
        if self._watched_folder:
            self._folder_watcher.set_root(self._watched_folder)
        self._folder_watcher.changed.connect(self._on_folder_changed)
        self._folder_watcher.subfolder_changed.connect(self._on_subfolder_changed)

        # Root and subfolder notifications commonly describe the same disk
        # mutation. One debounce boundary schedules one background snapshot.
        self._wf_refresh_debounce = QTimer(self)
        self._wf_refresh_debounce.setSingleShot(True)
        self._wf_refresh_debounce.setInterval(600)
        self._wf_refresh_debounce.timeout.connect(
            self._flush_watched_folder_refresh
        )
        self._wf_root_refresh_pending = False
        self._wf_pending_sub_path: str = ""

    def _schedule_cleanup(self, items: list[dict]) -> None:
        self._cleanup_queue.enqueue_items(items)
        self._cleanup_timer.start()

    # ── Navigation ─────────────────────────────────────────────────────────

    def _open_playlist(self, pl_id: str):
        pl = next((p for p in self._playlists if p["id"] == pl_id), None)
        if not pl: return
        self._edit_view.load_playlist(pl)
        self._stack.setCurrentIndex(1)

    def _open_watched_folder(self, folder_path: str):
        self._edit_view.load_watched_folder(folder_path)
        self._stack.setCurrentIndex(1)

    def _go_back(self):
        # If coming back from a watched folder, clear the watched state
        if self._edit_view._is_watched:
            self._edit_view._reset_watched_folder_refresh_retry()
            self._edit_view._is_watched = False
            self._edit_view._watched_path = ""
        self._list_view.refresh()
        self._list_view.refresh_watched()
        self._stack.setCurrentIndex(0)

    @Slot(object, str)
    def _on_playlist_import_requested(
        self,
        paths: list[str],
        playlist_format: str,
    ) -> None:
        self._playlist_transfers.import_playlists(paths, playlist_format)

    def import_native_playlists(
        self,
        paths: list[str],
        open_after: bool = False,
    ) -> None:
        """Import native packages opened by argv, IPC, or file association."""
        self._playlist_transfers.import_playlists(
            paths,
            "solin",
            open_after=open_after,
        )

    @Slot(str, str)
    def _on_playlist_export_requested(
        self,
        playlist_id: str,
        playlist_format: str,
    ) -> None:
        playlist = next(
            (entry for entry in self._playlists if entry.get("id") == playlist_id),
            None,
        )
        if playlist is None:
            return
        self._playlist_transfers.export_playlist(
            copy.deepcopy(playlist), playlist_format
        )

    @Slot(str, str)
    def _on_watched_folder_export_requested(
        self,
        folder_path: str,
        playlist_format: str,
    ) -> None:
        display_name = os.path.basename(os.path.normpath(folder_path)) or folder_path
        self._playlist_transfers.load_and_export_playlist(
            display_name=display_name,
            playlist_format=playlist_format,
            loader=lambda: self._watched_folder_playlist_store.load_playlist(
                folder_path
            ),
            empty_message=self.tr('No media files found in "{name}".').replace(
                "{name}", display_name
            ),
        )

    @Slot(str)
    def _on_edit_export_requested(self, playlist_format: str) -> None:
        snapshot = self._edit_view.playlist_export_snapshot(playlist_format)
        if snapshot is not None:
            self._playlist_transfers.export_playlist(snapshot, playlist_format)

    @Slot(object, int, str)
    def _on_edit_jwl_import_requested(
        self,
        paths: list[str],
        insert_at: int,
        section_id: str,
    ) -> None:
        expected_playlist_id = self._edit_view.current_playlist_id
        if not expected_playlist_id:
            return
        self._playlist_transfers.import_jwl_items(
            paths,
            section_id=section_id,
            completed=lambda items: self._edit_view.commit_imported_jwl_items(
                items,
                insert_at=insert_at,
                expected_playlist_id=expected_playlist_id,
            ),
        )

    # ── Watched folder ─────────────────────────────────────────────────────

    def set_watched_folder(self, path: str) -> None:
        """Chamado pelo main_window quando a configuração muda."""
        self._watched_folder = path
        self._list_view.set_watched_folder(path)
        self._folder_watcher.set_root(path)

    def _on_folder_changed(self) -> None:
        """O watcher detectou mudança na pasta raiz ou subpastas (debounced)."""
        if self._stack.currentIndex() == 1 and self._edit_view._is_watched:
            self._edit_view.supersede_watched_folder_refresh()
        self._wf_root_refresh_pending = True
        self._wf_refresh_debounce.start()

    def _on_subfolder_changed(self, path: str) -> None:
        """Mudança em subpasta específica — debounced."""
        if (self._stack.currentIndex() == 1
                and self._edit_view._is_watched
                and self._edit_view._watched_path == path):
            self._edit_view.supersede_watched_folder_refresh()
            self._wf_pending_sub_path = path
            self._wf_refresh_debounce.start()

    def _flush_watched_folder_refresh(self) -> None:
        """Apply one snapshot for all watcher events in the debounce window."""
        refresh_root = self._wf_root_refresh_pending
        subfolder_path = self._wf_pending_sub_path
        self._wf_root_refresh_pending = False
        self._wf_pending_sub_path = ""

        edit_is_open = (
            self._stack.currentIndex() == 1
            and self._edit_view._is_watched
        )
        if refresh_root and not edit_is_open:
            self._list_view.refresh_watched()
        subfolder_matches = (
            bool(subfolder_path)
            and self._edit_view._watched_path == subfolder_path
        )
        if edit_is_open and (refresh_root or subfolder_matches):
            self._edit_view.refresh_watched_folder()

        if refresh_root or subfolder_path:
            self._watched_folder_playlist_store.notify_external_change()

    # ── Temp playlist ──────────────────────────────────────────────────────

    def _on_save_temp_playlist(self, name: str, playlist_data: dict):
        try:
            name = ensure_unique_playlist_name(name, self._playlists)
        except PlaylistNameConflictError as exc:
            self._notifications.warning(
                self.tr('A playlist named "{name}" already exists.').replace(
                    "{name}", exc.name
                )
            )
            return
        except PlaylistNameError:
            return
        if isinstance(playlist_data, dict):
            pl = copy.deepcopy(playlist_data)
        else:
            pl = {"items": copy.deepcopy(list(playlist_data or []))}
        pl["id"] = str(uuid.uuid4())
        pl["name"] = name
        pl.pop("_temp", None)
        pl.setdefault("items", [])
        self._playlists.append(pl)
        self._persist_playlists()
        self._list_view.refresh()
        self._edit_view.load_playlist(pl)

    def open_pdf_as_temp_playlist(self, items: list, pdf_stem: str) -> str:
        playlist_id = self._edit_view.load_temp_playlist(
            items,
            self.lang,
            name=f"📄  {pdf_stem}",
        )
        self._stack.setCurrentIndex(1)
        return playlist_id

    def _on_project_items(self, items: list, start_idx: int, order: str):
        if not items: return
        item  = items[start_idx]
        url   = item.get("url", "")
        title = item.get("title", "")
        pl = [dict(playlist_item) for playlist_item in items]
        source = getattr(self._edit_view, "_pl", None) or {}
        container_id = str(source.get("id") or "")
        origin_kind = (
            "temporary"
            if getattr(self._edit_view, "_is_temp", False)
            else "playlist"
        )
        if getattr(self._edit_view, "_is_watched", False):
            origin_kind = "linked_folder" if container_id else "temporary"
        for playlist_item in pl:
            playlist_item["origin_kind"] = origin_kind
            playlist_item["origin_container_id"] = container_id
            playlist_item["origin_item_id"] = str(playlist_item.get("id") or "")
        self.project_video_signal.emit(url, title, pl, order)

    # ── API pública ────────────────────────────────────────────────────────

    def open_temp_playlist(
        self,
        items: list,
        lang=None,
        *,
        name: str | None = None,
    ) -> str:
        playlist_id = self._edit_view.load_temp_playlist(items, lang, name=name)
        self._stack.setCurrentIndex(1)
        return playlist_id

    @Slot(str, int)
    def record_source_duration(self, item_id: str, duration_ms: int) -> None:
        """Persist an original media duration through the active playlist owner."""
        if not item_id or duration_ms <= 0:
            return
        self._edit_view.notify_duration(item_id, duration_ms)

    def append_temp_playlist_items(
        self,
        playlist_id: str,
        items: list[dict],
    ) -> bool:
        playlist = self._edit_view._pl
        if (
            not self._edit_view._is_temp
            or playlist is None
            or playlist.get("id") != playlist_id
        ):
            return False
        playlist.setdefault("items", []).extend(copy.deepcopy(items))
        self._edit_view._reconcile_playlist()
        return True

    def get_playlist_names(self) -> list[tuple[str, str]]:
        return [(p["id"], p["name"]) for p in self._playlists]

    def playlist_placement_ref(self, pl_id: str) -> dict | None:
        playlist = next((p for p in self._playlists if p["id"] == pl_id), None)
        return copy.deepcopy(playlist) if playlist is not None else None

    def add_items_to_playlist(
        self,
        pl_id: str,
        items: list[dict],
        *,
        list_id: str,
        insert_index: int,
    ) -> MediaInsertResult:
        pl = next((p for p in self._playlists if p["id"] == pl_id), None)
        if pl is None:
            return MediaInsertResult(target_valid=False)

        partition = partition_media_items(pl.get("items", []), items)
        added_items = [dict(copy.deepcopy(item)) for item in partition.unique_items]

        if not added_items:
            return MediaInsertResult(duplicate_items=partition.duplicate_items)
        if not insert_playlist_media(
            pl,
            list_id,
            insert_index,
            added_items,
        ):
            return MediaInsertResult(
                duplicate_items=partition.duplicate_items,
                target_valid=False,
            )

        self._persist_playlists()
        if (self._stack.currentIndex() == 1
                and self._edit_view._pl
                and self._edit_view._pl["id"] == pl_id):
            self._edit_view._reconcile_playlist()
        return MediaInsertResult(
            added_items=tuple(added_items),
            duplicate_items=partition.duplicate_items,
        )

    def create_playlist_with_items(self, name: str, items: list[dict]) -> str:
        name = ensure_unique_playlist_name(name, self._playlists)
        partition = partition_media_items([], items)
        pl = {
            "id": str(uuid.uuid4()),
            "name": name,
            "items": [copy.deepcopy(item) for item in partition.unique_items],
        }
        self._playlists.append(pl)
        self._persist_playlists()
        self._list_view.refresh()
        return pl["id"]

    def _persist_playlists(self) -> None:
        snapshot = copy.deepcopy(self._playlists)
        self._media_tree_runtime.snapshots.request(
            f"playlists:{self._playlist_repository.path}",
            lambda: self._playlist_repository.save_strict(snapshot),
        )

    # ── i18n ──────────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._list_view.retranslateUi()
        self._edit_view.retranslateUi()

    def apply_theme(self) -> None:
        self._list_view.apply_theme()
        self._edit_view.apply_theme()

    def cleanup(self) -> None:
        """Stop background work owned by child views."""
        self._cleanup_timer.stop()
        self._wf_refresh_debounce.stop()
        for signal, slot in (
            (self._folder_watcher.changed, self._on_folder_changed),
            (self._folder_watcher.subfolder_changed, self._on_subfolder_changed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        try:
            self._playlist_transfers.shutdown()
        except Exception:  # noqa: BLE001 - widget cleanup boundary
            log_ignored_exception(__name__, "Could not stop playlist transfers")
        try:
            self._list_view.cleanup()
        except Exception:  # noqa: BLE001 - widget cleanup boundary
            log_ignored_exception(__name__, "Could not cleanup playlist list view")
        try:
            self._edit_view.cleanup()
        except Exception:  # noqa: BLE001 - widget cleanup boundary
            log_ignored_exception(__name__, "Could not cleanup playlist edit view")
        self._cleanup_queue.flush()
