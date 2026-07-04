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
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtQuickWidgets import QQuickWidget

from solin.styles.theme import PALETTE, QML_THEME
from solin.ui.qml.host import apply_qml_theme, configure_qml_host
from solin.ui.qml.playlist.bridge import PlaylistEditBridge
from solin.ui.qml.playlist.model import PlaylistEditModel
from solin.ui.qml.playlist.visuals import (
    PlaylistThumbnailProvider,
    PlaylistIconProvider,
)
from solin.ui.qml.jw_media_catalog import JWMediaCatalogBridge
from solin.ui.qml.jw_songs import JWSongsBridge
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.ingest.manifest import (
    ManifestError,
    ManifestWriteError,
    retry_manifest_write,
)
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
from ...core.playlists.items import looks_like_filename_title
from ...core.projection.image_framing import (
    ImageTransform,
    image_transform_from_record,
    image_transform_to_record,
    prepare_image_transform_for_aspect,
)
from ...core.tree_delta import incremental_tree_changes
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
from ...ui.thumbnail_images import (
    image_source_aspect_ratio,
    load_thumbnail,
    save_thumbnail,
)
_THUMB_W, _THUMB_H = 70, 46
_ITEM_H            = 77   # altura fixa de cada item
_PLAYLIST_REORDER_LOCATION_KEYS = frozenset({
    "parent_id",
    "position",
    "section_id",
    "slot_order",
})
_MANIFEST_SAVE_DEBOUNCE_MS = 180
_MANIFEST_SAVE_RETRY_DELAYS_MS = (100, 250, 500, 1_000, 2_000, 5_000, 10_000, 15_000)
_MANIFEST_SAVE_RETRY_BUDGET_SECONDS = 60.0


@dataclass(slots=True)
class _PendingPlaylistManifestSave:
    folder_path: str
    playlist: dict
    generation: int
    next_attempt_at: float
    first_attempt_at: float | None = None
    retry_index: int = 0

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
    _manifestSaveCompleted = Signal(str, int, object)

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
        self._thumb_pending_item_ids: set[str] = set()
        self._thumb_request_token: int = 0
        self._thumb_scan_items: list[dict] = []
        self._thumb_scan_index: int = 0
        self._qml_pointer_depth = 0
        self._thumb_queue = media_info_queue_factory(self)
        self._thumb_queue.info_ready.connect(self._on_info)
        self._thumb_queue.duration_ready.connect(self._on_duration_from_extractor)
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
        self._manifest_save_inflight: tuple[str, int] | None = None
        self._manifest_save_future: Future[None] | None = None
        self._manifest_save_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="playlist-manifest",
        )
        self._manifestSaveCompleted.connect(self._on_manifest_save_completed)
        self._manifest_save_timer = QTimer(self)
        self._manifest_save_timer.setSingleShot(True)
        self._manifest_save_timer.timeout.connect(self._drain_manifest_saves)
        self._pdf_threads:  list[object] = []
        self._lo_threads:   list[object] = []
        self._wf_refresh_pending: bool = False      # deferred refresh flag
        self._wf_file_availability: tuple[tuple[str, bool], ...] = ()

        # QML Integration
        self.model = PlaylistEditModel(
            media_cache_manager,
            self._playlist_thumbnail_store,
            self,
        )
        self.bridge = PlaylistEditBridge(
            projection_aspect_ratio_provider,
            self._image_source_aspect_ratio,
            parent=self,
        )
        self.bridge.attach_model(self.model)
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
        self._flush_image_framing_save()
        self._manifest_save_timer.stop()
        final_saves: list[Future[None]] = []
        for request in self._pending_manifest_saves.values():
            final_saves.append(
                self._manifest_save_executor.submit(
                    retry_manifest_write,
                    lambda pending=request: (
                        self._watched_folder_playlist_store.save_playlist(
                            pending.folder_path,
                            pending.playlist,
                        )
                    ),
                )
            )
        self._manifest_save_executor.shutdown(wait=True, cancel_futures=False)
        for future in final_saves:
            try:
                future.result()
            except ManifestError:
                log.warning("Could not flush playlist manifest during cleanup", exc_info=True)
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
            self._playlist_repository.save(self._all_playlists)

    def _schedule_manifest_save(self, folder_path: str, playlist: dict) -> None:
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
            self._watched_folder_playlist_store.save_playlist,
            request.folder_path,
            request.playlist,
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
                "playlistthumbs": PlaylistThumbnailProvider(
                    self._id_to_thumb,
                    disk_loader_cb=lambda item_id: load_thumbnail(
                        self._playlist_thumbnail_store,
                        item_id,
                    ),
                ),
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
        self.bridge.imageFramingSetRequested.connect(self._set_image_framing)
        self.bridge.imageFramingResetRequested.connect(self._reset_image_framing)
        self.bridge.renameMarkerSignal.connect(self._rename_marker)
        self.bridge.deleteMarkerSignal.connect(self._delete_marker)

        self.bridge.renameSectionSignal.connect(self._rename_section)
        self.bridge.deleteSectionSignal.connect(self._delete_section)
        self.bridge.recolorSectionSignal.connect(self._recolor_section)

        self.bridge.collapseSectionSignal.connect(self._toggle_section_collapse)
        self.bridge.moveEntrySignal.connect(self.model.move_entry)
        self.bridge.dragFinished.connect(self.model.finalize_drag)
        self.bridge.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.bridge.pointerExited.connect(self.end_qml_pointer_cursor)

        # When the model order is finalized, save the changes
        self.model.orderSynced.connect(self._save)

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
        self.model.invalidate_tree_data_cache()
        self.bridge.emit_image_framing_changed(item_id, record)
        self._schedule_image_framing_save()

    def _image_source_aspect_ratio(self, item_id: str) -> float:
        item = next(
            (candidate for candidate in (self._pl or {}).get("items", [])
             if candidate.get("id") == item_id),
            None,
        )
        ratio = image_source_aspect_ratio(
            path=str((item or {}).get("url") or "")
        )
        if ratio > 0.0:
            return ratio
        pixmap = self._id_to_thumb.get(item_id)
        if pixmap is None or pixmap.isNull():
            pixmap = load_thumbnail(self._playlist_thumbnail_store, item_id)
        return image_source_aspect_ratio(pixmap=pixmap)

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
        self.model.invalidate_tree_data_cache()
        self.bridge.emit_image_framing_changed(item_id, None)
        self._schedule_image_framing_save()

    def _schedule_image_framing_save(self) -> None:
        self._image_framing_save_pending = True
        self._image_framing_save_timer.start()

    def _flush_image_framing_save(self) -> None:
        if not self._image_framing_save_pending:
            return
        self._image_framing_save_timer.stop()
        self._save()

    def _toggle_section_collapse(self, section_id: str) -> None:
        self.model.toggle_collapse(section_id)
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
        self.model.update_cloud_state(url)
        self.bridge.emit_cloud_changed_for_url(url)

    def _on_cache_removed(self, url: str):
        self.model.update_cloud_state(url)
        self.bridge.emit_cloud_changed_for_url(url)

    def _on_prefetch_progress(self, url: str, downloaded: int, total: int):
        if total > 0:
            pct = int(downloaded * 100 / total)
            self.model.update_cloud_progress(url, pct)
            self.bridge.emit_cloud_changed_for_url(url)

    def _on_prefetch_error(self, url: str, _message: str):
        self.model.update_cloud_state(url)
        self.bridge.emit_cloud_changed_for_url(url)

    def _download_item(self, item_id: str):
        if not self._pl:
            return
        for item in self._pl.get("items", []):
            if item.get("id") == item_id:
                url = item.get("url", "")
                if url and not self._media_cache_manager.is_cached(url):
                    self._media_cache_manager.prefetch(url, priority=True)
                    self.model.update_cloud_state(url)
                    self.bridge.emit_cloud_changed_for_url(url)
                break

    # ── Carga ──────────────────────────────────────────────────────────────

    def load_playlist(self, pl: dict):
        self._flush_image_framing_save()
        self._wf_refresh_pending = False
        self._is_watched = False
        self._watched_path = ""
        self._pl = pl
        self._is_temp = bool(pl.get("_temp"))
        self._thumb_queue.clear()
        self._thumb_scan_timer.stop()
        self._thumb_scan_items.clear()
        self._id_to_thumb.clear()
        self._thumb_pending_item_ids.clear()
        self._rebuild_list()

    def load_watched_folder(self, folder_path: str):
        """Load a linked folder as a full playlist with drag-reorder + sections."""
        self._flush_image_framing_save()
        self._wf_refresh_pending = False
        self._is_watched = True
        self._watched_path = folder_path
        self._is_temp = False
        key = os.path.normcase(os.path.abspath(folder_path))
        pending = self._pending_manifest_saves.get(key)
        pl = (
            copy.deepcopy(pending.playlist)
            if pending is not None
            else self._watched_folder_playlist_store.load_playlist(folder_path)
        )
        self._pl = pl
        self._wf_file_availability = self._watched_file_availability(pl)
        self._thumb_queue.clear()
        self._thumb_scan_timer.stop()
        self._thumb_scan_items.clear()
        self._id_to_thumb.clear()
        self._thumb_pending_item_ids.clear()
        self._rebuild_list()
        self._start_wf_sync()

    def _start_wf_sync(self):
        """Start background sync for pending processable files in linked folder."""
        if not self._watched_path:
            return
        pending = self._watched_folder_playlist_store.pending_files(self._watched_path)
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

    def _watched_file_availability(self, pl: dict | None = None) -> tuple[tuple[str, bool], ...]:
        """Snapshot machine-local file availability for linked-folder items."""
        source = pl if pl is not None else self._pl
        urls = [
            item.get("url", "")
            for item in (source or {}).get("items", [])
        ]
        return self._watched_folder_playlist_store.file_availability_signature(urls)

    def refresh_watched_folder(self):
        """Re-scan and reconcile linked folder (called by watcher)."""
        self._flush_image_framing_save()
        if not self._is_watched or not self._watched_path:
            return
        key = os.path.normcase(os.path.abspath(self._watched_path))
        if key in self._pending_manifest_saves:
            self._wf_refresh_pending = True
            return
        if self._wf_sync_thread is not None:
            self._wf_refresh_pending = True
            return
        pl = self._watched_folder_playlist_store.load_playlist(self._watched_path)
        if self._watched_playlist_equivalent(pl):
            availability = self._watched_file_availability(pl)
            if availability != self._wf_file_availability:
                self._pl = pl
                self._wf_file_availability = availability
                self.model.rebuild(self._pl)
                self._sync_playlist_chrome()
                QTimer.singleShot(0, self._request_missing_thumbnails)
            self._start_wf_sync()
            return
        availability = self._watched_file_availability(pl)
        if availability == self._wf_file_availability:
            changes = incremental_tree_changes(
                self.model.storage_tree(),
                self.model.storage_tree(pl),
                ignored_payload_keys=_PLAYLIST_REORDER_LOCATION_KEYS,
                section_patch_keys={"name", "color_hue", "collapsed"},
            )
            if changes is not None:
                self._pl = pl
                self._wf_file_availability = availability
                self.model.rebuild(self._pl)
                self._sync_playlist_chrome(emit_data_changed=False)
                for removal in changes.removals:
                    self.bridge.emit_node_replaced(removal.node_id, [])
                for insertion in changes.inserts:
                    self.bridge.emit_nodes_inserted(
                        insertion.target_list_id,
                        insertion.insert_index,
                        [
                            str(node.get("id", ""))
                            for node in insertion.nodes
                            if node.get("id")
                        ],
                    )
                for move in changes.moves:
                    self.bridge.emit_node_moved(
                        move.node_id,
                        move.target_list_id,
                        move.insert_index,
                    )
                for update in changes.section_updates:
                    if update.metadata_changed:
                        self.bridge.emit_section_changed(update.node_id)
                    if update.collapsed_changed:
                        section = self._section_by_id(update.node_id)
                        if section is not None:
                            self.bridge.emit_section_collapse_changed(
                                update.node_id,
                                bool(section.get("collapsed", False)),
                            )
                if changes.has_changes:
                    self.bridge.emit_section_counts_changed()
                    QTimer.singleShot(0, self._request_missing_thumbnails)
                self._start_wf_sync()
                return
        self._pl = pl
        self._rebuild_list()
        self._wf_file_availability = availability
        self._start_wf_sync()

    def _rebuild_list(self):
        if not self._pl:
            return

        self.catalog_bridge.set_playlist_ref(self._pl)
        self._apply_media_language_context()

        self._thumb_idx_to_id.clear()

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

        # Rebuild model
        self.model.rebuild(self._pl)

        self._sync_playlist_chrome()

        # Defer thumbnail work until QML has processed the model rebuild. Local
        # images and embedded audio covers can resolve synchronously, so doing
        # this inside the rebuild can emit a media patch before the QML node
        # exists.
        QTimer.singleShot(0, self._request_missing_thumbnails)

    def _request_missing_thumbnails(self) -> None:
        if not self._pl:
            return
        self._thumb_scan_items = list(self._pl.get("items", []))
        self._thumb_scan_index = 0
        if self._thumb_scan_items:
            self._thumb_scan_timer.start()

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
        item_id = item["id"]
        url = item.get("url", "")
        media_type = item.get("type", "video")
        needs_title = item.get("auto_title", False) and url.startswith(("http://", "https://"))

        cached = self._id_to_thumb.get(item_id)
        if cached is not None and not cached.isNull():
            if needs_title:
                self._request_thumbnail(item_id, url, media_type)
            return

        has_disk = self._playlist_thumbnail_store.exists(item_id)
        if has_disk:
            if needs_title:
                self._request_thumbnail(item_id, url, media_type)
            return

        self._request_thumbnail(item_id, url, media_type)

    def _request_thumbnail(self, item_id: str, url: str, media_type: str) -> None:
        if item_id in self._thumb_pending_item_ids:
            return
        self._thumb_request_token += 1
        token = self._thumb_request_token
        self._thumb_idx_to_id[token] = item_id
        self._thumb_pending_item_ids.add(item_id)
        self._thumb_queue.request(token, url, media_type)

    def _on_info(self, idx: int, pixmap: QPixmap, title: str):
        item_id = self._thumb_idx_to_id.get(idx)
        if item_id is None:
            return
        self._thumb_pending_item_ids.discard(item_id)

        media_changed = False
        if pixmap and not pixmap.isNull():
            self._id_to_thumb[item_id] = pixmap
            try:
                save_thumbnail(self._playlist_thumbnail_store, item_id, pixmap)
            except OSError:
                log_ignored_exception(__name__, "Could not save playlist thumbnail")
            self.model.update_thumb(item_id)
            media_changed = True

        if title and self._pl:
            for item in self._pl.get("items", []):
                if item.get("id") != item_id:
                    continue
                current = item.get("title", "")
                if (
                    looks_like_filename_title(current)
                    or item.get("auto_title")
                ) and title != current:
                    item["title"] = title
                    item["auto_title"] = False
                    self._save()
                    self.model.update_title(item_id, title)
                    media_changed = True
                break

        if media_changed:
            self.bridge.emit_media_changed(item_id)

    def _on_duration_from_extractor(self, idx: int, dur_ms: int) -> None:
        item_id = self._thumb_idx_to_id.get(idx)
        if item_id:
            self.notify_duration(item_id, dur_ms)

    def notify_duration(self, item_id: str, duration_ms: int) -> None:
        if not self._pl or duration_ms <= 0:
            return
        ticks = duration_ms * 10_000
        for item in self._pl.get("items", []):
            if item.get("id") == item_id:
                if item.get("base_duration_ticks") != ticks:
                    item["base_duration_ticks"] = ticks
                    self._save()
                    self.model.update_title(item_id, item.get("title", ""))
                    self.bridge.emit_media_changed(item_id)
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
        self.model.rebuild(self._pl)
        self._sync_playlist_chrome(emit_data_changed=False)
        self.bridge.emit_nodes_inserted("root", 2**31 - 1, [sec["id"]])
        self.bridge.emit_section_counts_changed()

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
        self.model.rebuild(self._pl)
        self._sync_playlist_chrome(emit_data_changed=False)
        self.bridge.emit_nodes_inserted(
            f"section:{parent_section_id}", 2**31 - 1, [sub["id"]])
        self.bridge.emit_section_counts_changed()

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
        self.model.rebuild(self._pl)
        self.model.move_node(marker["id"], f"subsection:{subsection_id}", 0)
        self._save()
        self._sync_playlist_chrome(emit_data_changed=False)
        if was_collapsed:
            self.bridge.emit_node_replaced(subsection_id, [subsection_id])
        else:
            self.bridge.emit_nodes_inserted(
                f"subsection:{subsection_id}", 0, [marker["id"]])
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
        self.model.invalidate_tree_data_cache()

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
        self.model.invalidate_tree_data_cache()
        self._sync_playlist_chrome(emit_data_changed=False)
        self.bridge.emit_node_replaced(marker_id, [])

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
        self.model.update_section(sec_id)
        self.bridge.emit_section_changed(sec_id)

    def _delete_section(self, sec_id: str):
        """Delete a section/subsection but keep its items."""
        if not self._pl: return
        sections = self._pl.get("sections", [])
        sec = next((s for s in sections if s["id"] == sec_id), None)
        if not sec: return
        replacement_ids = self._media_ids_in_tree_node(self.model.node_patch(sec_id))
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
        self.model.rebuild(self._pl)
        self._sync_playlist_chrome(emit_data_changed=False)
        self.bridge.emit_node_replaced(sec_id, replacement_ids)
        self.bridge.emit_section_counts_changed()

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
        self.model.update_section(sec_id)
        self.bridge.emit_section_changed(sec_id)

    def _remove_item(self, item_id: str):
        if not self._pl: return
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
        self._save()
        if self._is_watched and self._watched_path and item:
            self._remove_watched_item(self._watched_path, item)
        elif item:
            self._schedule_cleanup([item])
        self.model.rebuild(self._pl)
        self._sync_playlist_chrome(emit_data_changed=False)
        self.bridge.emit_node_replaced(item_id, [])
        self.bridge.emit_section_counts_changed()

    def _remove_watched_item(self, folder_path: str, item: dict) -> None:
        try:
            self._watched_folder_playlist_store.remove_item(folder_path, item)
        except ManifestError as exc:
            if self._warn_manifest_save_failed(folder_path, str(exc)):
                QTimer.singleShot(
                    0,
                    lambda: self._remove_watched_item(folder_path, item),
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
            self.model.update_title(item_id, title)
            self.bridge.emit_media_changed(item_id)

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
            self._rebuild_list()

    def apply_theme(self) -> None:
        apply_qml_theme(self.qml_widget, clear_color=PALETTE.media_placeholder)
        if self._pl:
            self.model.rebuild(self._pl)
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
            profile_paths=self._profile_paths,
            storage_paths=self._storage_paths,
            playlist_repository=self._playlist_repository,
            profile_media_store=self._profile_media_store,
            playlist_thumbnail_store=self._playlist_thumbnail_store,
            watched_folder_file_store=self._watched_folder_file_store,
            watched_folder_playlist_store=self._watched_folder_playlist_store,
            media_cache_manager=self._media_cache_manager,
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
        self._edit_view.back_requested.connect(self._go_back)
        self._edit_view.project_items.connect(self._on_project_items)
        self._edit_view.save_temp_as_permanent.connect(self._on_save_temp_playlist)

    def _setup_watcher(self):
        self._folder_watcher = self._watched_folder_watcher_factory(self)
        if self._watched_folder:
            self._folder_watcher.set_root(self._watched_folder)
        self._folder_watcher.changed.connect(self._on_folder_changed)
        self._folder_watcher.subfolder_changed.connect(self._on_subfolder_changed)

        # Debounce timers — coalesce rapid-fire filesystem events into a
        # single refresh so the UI thread isn't starved by cascading scans.
        self._wf_debounce_root = QTimer(self)
        self._wf_debounce_root.setSingleShot(True)
        self._wf_debounce_root.setInterval(600)
        self._wf_debounce_root.timeout.connect(self._do_folder_refresh)

        self._wf_debounce_sub = QTimer(self)
        self._wf_debounce_sub.setSingleShot(True)
        self._wf_debounce_sub.setInterval(600)
        self._wf_debounce_sub.timeout.connect(self._do_subfolder_refresh)
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
            self._edit_view._is_watched = False
            self._edit_view._watched_path = ""
        self._list_view.refresh()
        self._list_view.refresh_watched()
        self._stack.setCurrentIndex(0)

    # ── Watched folder ─────────────────────────────────────────────────────

    def set_watched_folder(self, path: str) -> None:
        """Chamado pelo main_window quando a configuração muda."""
        self._watched_folder = path
        self._list_view.set_watched_folder(path)
        self._folder_watcher.set_root(path)

    def _on_folder_changed(self) -> None:
        """O watcher detectou mudança na pasta raiz ou subpastas (debounced)."""
        self._wf_debounce_root.start()   # restart the timer on each event

    def _do_folder_refresh(self) -> None:
        """Actual refresh after debounce window expires."""
        self._list_view.refresh_watched()
        if self._stack.currentIndex() == 1 and self._edit_view._is_watched:
            self._edit_view.refresh_watched_folder()

    def _on_subfolder_changed(self, path: str) -> None:
        """Mudança em subpasta específica — debounced."""
        if (self._stack.currentIndex() == 1
                and self._edit_view._is_watched
                and self._edit_view._watched_path == path):
            self._wf_pending_sub_path = path
            self._wf_debounce_sub.start()   # restart on each event

    def _do_subfolder_refresh(self) -> None:
        """Actual subfolder refresh after debounce window expires."""
        if (self._wf_pending_sub_path
                and self._stack.currentIndex() == 1
                and self._edit_view._is_watched
                and self._edit_view._watched_path == self._wf_pending_sub_path):
            self._edit_view.refresh_watched_folder()
        self._wf_pending_sub_path = ""

    # ── Temp playlist ──────────────────────────────────────────────────────

    def _on_save_temp_playlist(self, name: str, playlist_data: dict):
        if isinstance(playlist_data, dict):
            pl = copy.deepcopy(playlist_data)
        else:
            pl = {"items": copy.deepcopy(list(playlist_data or []))}
        pl["id"] = str(uuid.uuid4())
        pl["name"] = name
        pl.pop("_temp", None)
        pl.setdefault("items", [])
        self._playlists.append(pl)
        self._playlist_repository.save(self._playlists)
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
        self._edit_view._rebuild_list()
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
        added_items = [copy.deepcopy(item) for item in partition.unique_items]

        if not added_items:
            return MediaInsertResult(duplicate_items=partition.duplicate_items)
        if not self._edit_view.model.insert_media_refs_into_playlist(
            pl,
            list_id,
            insert_index,
            added_items,
        ):
            return MediaInsertResult(
                duplicate_items=partition.duplicate_items,
                target_valid=False,
            )

        self._playlist_repository.save(self._playlists)
        if (self._stack.currentIndex() == 1
                and self._edit_view._pl
                and self._edit_view._pl["id"] == pl_id):
            self._edit_view._rebuild_list()
        return MediaInsertResult(
            added_items=tuple(added_items),
            duplicate_items=partition.duplicate_items,
        )

    def create_playlist_with_items(self, name: str, items: list[dict]) -> str:
        partition = partition_media_items([], items)
        pl = {
            "id": str(uuid.uuid4()),
            "name": name,
            "items": [copy.deepcopy(item) for item in partition.unique_items],
        }
        self._playlists.append(pl)
        self._playlist_repository.save(self._playlists)
        self._list_view.refresh()
        return pl["id"]

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
        try:
            self._edit_view.cleanup()
        except Exception:  # noqa: BLE001 - widget cleanup boundary
            log_ignored_exception(__name__, "Could not cleanup playlist edit view")
        self._cleanup_queue.flush()
