"""
playlist_widget.py  ─ Solin
Drag-to-reorder completamente reescrito com container manual (sem QListWidget).
Thumbnails persistem através de reordenações; botões com SVG real.
"""
from __future__ import annotations

import copy, logging, os, uuid
from typing import TYPE_CHECKING, Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QDialog, QMessageBox, QStackedWidget,
)
from PySide6.QtCore import (
    Signal, QTimer,
    QEvent, Slot,
)
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtGui import QSurfaceFormat

from .edit_bridge import PlaylistEditBridge
from .edit_model import PlaylistEditModel
from .edit_visuals import (
    PlaylistThumbnailProvider,
    PlaylistIconProvider,
)
from ..jw_media_catalog_bridge import JWMediaCatalogBridge
from ..jw_songs_bridge import JWSongsBridge

from ...qml_module import load_qml_type
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.i18n.manager import LanguageManager
from ...core.jw.language_context import (
    JWMediaLanguageContext,
    jw_media_language_context,
)
from ...core.ui.helpers import begin_qml_pointer_cursor, end_qml_pointer_cursor
from ...core.media.cache import MediaCacheManager
from ..media_info_extractor import MediaInfoQueue, is_filename_title
from .items import media_type_from_url
from .drag_drop import _PlaylistDragDropMixin
from .edit_actions import _PlaylistEditActionsMixin
from .import_export import _PlaylistEditImportMixin
from .list_view import _PlaylistListView
from ...core.playlists.cleanup import cleanup_item_files
from ...core.meetings.colors import APP_BASE_HUE, generate_section_hue
from .dialogs import _HuePickerDialog, _NameDialog
from ...core.playlists.storage import load_playlists, save_playlists
from ...core.playlists.thumbnails import playlist_thumb_path
from .thumbnails import (
    _load_thumb_from_disk,
    _save_thumb_to_disk,
)
_THUMB_W, _THUMB_H = 70, 46
_ITEM_H            = 77   # altura fixa de cada item

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ...core.ui.notifications import NotificationCenter

_current_playlists_ref: list[list] = [[]]

# ── Tela de edição ─────────────────────────────────────────────────────────────

class _PlaylistEditView(
    _PlaylistEditActionsMixin,
    _PlaylistDragDropMixin,
    _PlaylistEditImportMixin,
    QWidget,
):
    back_requested = Signal()
    project_items  = Signal(list, int, str)
    save_temp_as_permanent = Signal(str, dict)  # name, playlist data

    def __init__(
        self,
        lang: LanguageManager,
        media_ctrl=None,
        *,
        notifications: NotificationCenter,
        parent=None,
    ):
        super().__init__(parent)
        self.lang = lang
        self._media_ctrl = media_ctrl
        self._notifications = notifications
        self._pl: Optional[dict] = None
        self._is_temp: bool = False
        self._is_watched: bool = False          # linked folder mode
        self._watched_path: str = ""            # physical subfolder path
        self._wf_sync_thread: object = None     # WatchedFolderSyncThread
        self._id_to_thumb:     dict[str, QPixmap] = {}   # cache de thumbnails por ID
        self._thumb_idx_to_id: dict[int, str] = {}       # request token → item ID
        self._thumb_pending_item_ids: set[str] = set()
        self._thumb_request_token: int = 0
        self._qml_pointer_depth = 0
        self._thumb_queue = MediaInfoQueue(self)
        self._thumb_queue.info_ready.connect(self._on_info)
        self._thumb_queue.duration_ready.connect(self._on_duration_from_extractor)
        self._pdf_threads:  list[object] = []
        self._lo_threads:   list[object] = []
        self._wf_refresh_pending: bool = False      # deferred refresh flag
        self._wf_file_availability: tuple[tuple[str, bool], ...] = ()

        # QML Integration
        self.model = PlaylistEditModel(self)
        self.bridge = PlaylistEditBridge(self)
        self.bridge.attach_model(self.model)
        self.catalog_bridge = JWMediaCatalogBridge(self)
        self.songs_bridge = JWSongsBridge(self)

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
        if thread is None:
            return
        try:
            if not thread.isRunning():
                return
            thread.requestInterruption()
            thread.quit()
            thread.wait(wait_ms)
            if thread.isRunning():
                log.warning(
                    "Background thread %s did not stop within %d ms",
                    type(thread).__name__,
                    wait_ms,
                )
                thread.setParent(None)
                thread.finished.connect(thread.deleteLater)
        except RuntimeError:
            return

    # ── Centralized save dispatch ──────────────────────────────────────────
    def _save(self) -> None:
        """Save playlist state: to playlists.json for normal, to manifest for watched folders."""
        if self._is_temp:
            return
        if self._is_watched:
            from ...core.ingest.watched_folder import save_manifest_playlist
            save_manifest_playlist(self._watched_path, self._pl)
        else:
            save_playlists(_current_playlists_ref[0])

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Create QQuickWidget
        self.qml_widget = QQuickWidget(self)
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        self.qml_widget.setFormat(fmt)
        self.qml_widget.setClearColor(QColor("#0a0e14"))
        self.qml_widget.setMouseTracking(True)
        self.qml_widget.installEventFilter(self)

        # Set image providers
        self.qml_widget.engine().addImageProvider(
            "playlistthumbs",
            PlaylistThumbnailProvider(self._id_to_thumb, disk_loader_cb=_load_thumb_from_disk)
        )
        self.qml_widget.engine().addImageProvider("playlisticons", PlaylistIconProvider())

        # Set context properties
        ctx = self.qml_widget.rootContext()
        ctx.setContextProperty("playlistModel", self.model)
        ctx.setContextProperty("controller", self.bridge)
        ctx.setContextProperty("catalogBridge", self.catalog_bridge)
        ctx.setContextProperty("songsBridge", self.songs_bridge)

        load_qml_type(self.qml_widget, "PlaylistEditView")
        self.qml_widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)

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
        self.catalog_bridge.itemAddedSuccessfully.connect(self._notifications.success)
        self.catalog_bridge.jwMediaConfirmed.connect(self._on_jw_media_confirmed)
        self.songs_bridge.itemAddedSuccessfully.connect(self._notifications.success)
        self.songs_bridge.jwMediaConfirmed.connect(self._on_jw_media_confirmed)

    def _toggle_section_collapse(self, section_id: str) -> None:
        self.model.toggle_collapse(section_id)
        self._save()

    def _connect_cache_signals(self):
        MediaCacheManager.instance().cache_changed.connect(self._on_cache_changed)
        MediaCacheManager.instance().cache_removed.connect(self._on_cache_removed)
        MediaCacheManager.instance().prefetch_progress.connect(self._on_prefetch_progress)
        MediaCacheManager.instance().prefetch_error.connect(self._on_prefetch_error)

    def _disconnect_cache_signals(self):
        try:
            MediaCacheManager.instance().cache_changed.disconnect(self._on_cache_changed)
            MediaCacheManager.instance().cache_removed.disconnect(self._on_cache_removed)
            MediaCacheManager.instance().prefetch_progress.disconnect(self._on_prefetch_progress)
            MediaCacheManager.instance().prefetch_error.disconnect(self._on_prefetch_error)
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
                if url and not MediaCacheManager.instance().is_cached(url):
                    MediaCacheManager.instance().prefetch(url, priority=True)
                    self.model.update_cloud_state(url)
                    self.bridge.emit_cloud_changed_for_url(url)
                break

    # ── Carga ──────────────────────────────────────────────────────────────

    def load_playlist(self, pl: dict):
        self._is_watched = False
        self._watched_path = ""
        self._pl = pl
        self._is_temp = bool(pl.get("_temp"))
        self._thumb_queue.clear()
        self._id_to_thumb.clear()
        self._thumb_pending_item_ids.clear()
        self._rebuild_list()

    def load_watched_folder(self, folder_path: str):
        """Load a linked folder as a full playlist with drag-reorder + sections."""
        from ...core.ingest.watched_folder import load_manifest_playlist
        self._is_watched = True
        self._watched_path = folder_path
        self._is_temp = False
        pl = load_manifest_playlist(folder_path)
        self._pl = pl
        self._wf_file_availability = self._watched_file_availability(pl)
        self._thumb_queue.clear()
        self._id_to_thumb.clear()
        self._thumb_pending_item_ids.clear()
        self._rebuild_list()
        self._start_wf_sync()

    def _start_wf_sync(self):
        """Start background sync for pending processable files in linked folder."""
        from ...core.ingest.watched_folder import WatchedFolderSyncThread, get_pending_files
        if not self._watched_path:
            return
        pending = get_pending_files(self._watched_path)
        if not pending:
            return
        context = self._current_media_context()
        self._notifications.information(
            self.tr("Processing %n file(s)...", None, len(pending))
        )
        thread = WatchedFolderSyncThread(
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
            self._notifications.success(self.tr("All files processed"))
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
        from ...core.ingest.watched_folder import local_file_availability_signature
        source = pl if pl is not None else self._pl
        urls = [
            item.get("url", "")
            for item in (source or {}).get("items", [])
        ]
        return local_file_availability_signature(urls)

    def refresh_watched_folder(self):
        """Re-scan and reconcile linked folder (called by watcher)."""
        if not self._is_watched or not self._watched_path:
            return
        if self._wf_sync_thread is not None:
            self._wf_refresh_pending = True
            return
        from ...core.ingest.watched_folder import load_manifest_playlist
        pl = load_manifest_playlist(self._watched_path)
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
        self._pl = pl
        self._rebuild_list()
        self._wf_file_availability = self._watched_file_availability(pl)
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
        from ...core.jw.metadata import JW_DOMAINS as _JW_DOMAINS_CHECK
        for item in items:
            detected = media_type_from_url(item.get("url", ""))
            if item.get("type") == "video" and detected != "video":
                item["type"] = detected
                needs_save = True
            url = item.get("url", "")
            if (not item.get("auto_title")
                    and url.startswith(("http://", "https://"))
                    and any(d in url.lower() for d in _JW_DOMAINS_CHECK)
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
        for item in self._pl.get("items", []):
            item_id = item["id"]
            url = item.get("url", "")
            media_type = item.get("type", "video")
            needs_title = item.get("auto_title", False) and url.startswith(("http://", "https://"))

            cached = self._id_to_thumb.get(item_id)
            if cached is not None and not cached.isNull():
                if needs_title:
                    self._request_thumbnail(item_id, url, media_type)
                continue

            has_disk = playlist_thumb_path(item_id).exists()
            if has_disk:
                if needs_title:
                    self._request_thumbnail(item_id, url, media_type)
                continue

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
            _save_thumb_to_disk(item_id, pixmap)
            self.model.update_thumb(item_id)
            media_changed = True

        if title and self._pl:
            for item in self._pl.get("items", []):
                if item.get("id") != item_id:
                    continue
                current = item.get("title", "")
                if (is_filename_title(current) or item.get("auto_title")) and title != current:
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

    def _create_section(self):
        if not self._pl: return
        dlg = _NameDialog(lang=self.lang, parent=self, label=self.tr("Section name:"), placeholder=self.tr("E.g.: Introduction"))
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
        dlg = _NameDialog(lang=self.lang, parent=self, label=self.tr("Subsection name:"), placeholder=self.tr("E.g.: Part 1"))
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
        self.model.rebuild(self._pl)

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
        self.model.rebuild(self._pl)
        self._sync_playlist_chrome(emit_data_changed=False)
        self.bridge.emit_node_replaced(marker_id, [])

    def _rename_section(self, sec_id: str):
        if not self._pl: return
        sections = self._pl.get("sections", [])
        sec = next((s for s in sections if s["id"] == sec_id), None)
        if not sec: return
        dlg = _NameDialog(sec.get("name", ""), lang=self.lang, parent=self, label=self.tr("Section name:"), placeholder=self.tr("E.g.: Introduction"))
        dlg.setWindowTitle(self.tr("Rename section"))
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        name = dlg.get_name()
        if not name: return
        sec["name"] = name
        self._save()
        self._rebuild_list()

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
        dlg = _HuePickerDialog(current_hue, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        sec["color_hue"] = dlg.selected_hue()
        self._save()
        self._rebuild_list()

    def _url_in_playlist(self, url: str) -> bool:
        if not self._pl or not url: return False
        return any(it.get("url","") == url for it in self._pl.get("items",[]))

    def _remove_item(self, item_id: str):
        if not self._pl: return
        item = next((it for it in self._pl.get("items", []) if it["id"] == item_id), None)
        if item and self._media_ctrl is not None:
            item_type = item.get("type", "video")
            if item_type in ("video", "audio"):
                item_url   = item.get("url", "")
                cur_url    = self._media_ctrl._current_url or ""
                cur_local  = self._media_ctrl._local_path or ""
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
            from ...core.ingest.watched_folder import remove_item_from_manifest
            remove_item_from_manifest(self._watched_path, item)
        elif item:
            cleanup_item_files(item, _current_playlists_ref[0])
        self.model.rebuild(self._pl)
        self._sync_playlist_chrome(emit_data_changed=False)
        self.bridge.emit_node_replaced(item_id, [])
        self.bridge.emit_section_counts_changed()

    def _rename_item(self, item_id: str):
        if not self._pl: return
        item = next((it for it in self._pl.get("items", []) if it["id"] == item_id), None)
        if not item: return
        dlg = _NameDialog(item.get("title", ""), lang=self.lang, parent=self)
        dlg.setWindowTitle(self.tr("Rename media"))
        if dlg.exec() and dlg.get_name():
            item["title"] = dlg.get_name()
            item["auto_title"] = False
            self._save()
            self._rebuild_list()

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

    def refresh_language(self) -> None:
        self.retranslateUi()

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
        parent=None,
    ):
        super().__init__(parent)
        self.lang        = lang
        self._media_ctrl = media_ctrl
        self._notifications = notifications
        self._playlists  = load_playlists()
        _current_playlists_ref[0] = self._playlists
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
        self._list_view = _PlaylistListView(
            self._playlists, self.lang,
            media_ctrl=self._media_ctrl,
            watched_folder=self._watched_folder,
            parent=self,
        )
        # Index 1: playlist edit (also used for watched folders)
        self._edit_view = _PlaylistEditView(
            self.lang,
            media_ctrl=self._media_ctrl,
            notifications=self._notifications,
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
        from ...core.ingest.watched_folder import WatchedFolderWatcher
        self._folder_watcher = WatchedFolderWatcher(self)
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
        save_playlists(self._playlists)
        self._list_view.refresh()
        self._edit_view.load_playlist(pl)

    def open_pdf_as_temp_playlist(self, items: list, pdf_stem: str) -> None:
        self._edit_view.load_temp_playlist(items, self.lang, name=f"📄  {pdf_stem}")
        self._stack.setCurrentIndex(1)

    def _on_project_items(self, items: list, start_idx: int, order: str):
        if not items: return
        item  = items[start_idx]
        url   = item.get("url", "")
        title = item.get("title", "")
        pl    = [
            {"url": it.get("url", ""), "title": it.get("title", ""), "type": it.get("type", "video")}
            for it in items
        ]
        self.project_video_signal.emit(url, title, pl, order)

    # ── API pública ────────────────────────────────────────────────────────

    def open_temp_playlist(self, items: list, lang=None):
        self._edit_view.load_temp_playlist(items, lang)
        self._stack.setCurrentIndex(1)

    def get_playlist_names(self) -> list[tuple[str, str]]:
        return [(p["id"], p["name"]) for p in self._playlists]

    def add_item_to_playlist(self, pl_id: str, item: dict) -> bool:
        pl = next((p for p in self._playlists if p["id"] == pl_id), None)
        if not pl: return False
        url = item.get("url", "")
        if url and any(it.get("url", "") == url for it in pl.get("items", [])):
            return False
        pl.setdefault("items", []).append(item)
        save_playlists(self._playlists)
        if (self._stack.currentIndex() == 1
                and self._edit_view._pl
                and self._edit_view._pl["id"] == pl_id):
            self._edit_view._rebuild_list()
        return True

    def item_exists_in_playlist(self, pl_id: str, url: str) -> bool:
        pl = next((p for p in self._playlists if p["id"] == pl_id), None)
        if not pl or not url: return False
        return any(it.get("url", "") == url for it in pl.get("items", []))

    def create_playlist_with_item(self, name: str, item: dict) -> str:
        pl = {"id": str(uuid.uuid4()), "name": name, "items": [item]}
        self._playlists.append(pl)
        save_playlists(self._playlists)
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

    def refresh_language(self) -> None:
        """Alias de compatibilidade → retranslateUi()."""
        self.retranslateUi()

    def cleanup(self) -> None:
        """Stop background work owned by child views."""
        try:
            self._edit_view.cleanup()
        except Exception:  # noqa: BLE001 - widget cleanup boundary
            log_ignored_exception(__name__, "Could not cleanup playlist edit view")
