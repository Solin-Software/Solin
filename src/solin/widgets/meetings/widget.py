"""
meetings_widget.py  ─  Solin
=============================
Weekly meeting media browser.

Changes in this version:
  1. All user-visible strings wrapped with tr()
  2. PubCard redesigned – cover pinned top, text flows naturally (no floating gaps)
  3. _MediaRow uses MediaInfoQueue for async video/audio thumbnails
  4. Section header: single left accent strip only (no double-bar artefact)
  5. CBS shown as a subsection inside "Our Christian Life", not a separate top-level block
  6. CBS multi-story: each MepsDocumentId gets its own article-title sub-group
  7. SVG icons in section headers (diamond=treasures, wheat=ministry, sheep=christian life)
  8. Memorial card shown in the week the Memorial falls (MemorialService integration)
  9. playlist/widget.py drag-drop jwpub now uses media_api_code (bug fix)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QObject, Signal, Slot, QTimer,
    QCoreApplication, QUrl, QEvent
)
from PySide6.QtGui import (
    QColor, QSurfaceFormat
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QStackedWidget,
)
from PySide6.QtQuickWidgets import QQuickWidget

from ...core.meetings.publications import current_monday
from ...core.meetings.models import MemorialData, WeekData
from ...core.i18n.date import week_label, format_single_date
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from ...core.foundation.constants import (
    DOCX_EXTS as _DOCX_EXTS,
    JWPUB_EXTS as _JWPUB_EXTS,
    PDF_EXTS as _PDF_EXTS,
    PLAYLIST_EXTS as _PLAYLIST_EXTS,
    PPTX_EXTS as _PPTX_EXTS,
)
from ...core.media.formats import MEDIA_EXTS as _MEDIA_EXTS
from ...core.jw.language_context import (
    JWMediaLanguageContext,
    jw_media_language_context,
)
from ...core.jw.catalog import JWMediaCatalogCachePaths
from ...core.jw.songs import JWSongsStore
from ...core.ui.helpers import begin_qml_pointer_cursor, end_qml_pointer_cursor
from ...core.rendering.libreoffice import libreoffice_available
from ...core.meetings.schedule_settings import MeetingScheduleSettingsStore
from ...core.meetings.tree_store import MeetingTreeStore
from ...core.media.cache import MediaCacheManager
from ...core.media.settings import MediaSettingsStore
from ...qml_module import load_qml_type
from ..jw_media_catalog_bridge import JWMediaCatalogBridge
from ..jw_songs_bridge import JWSongsBridge
from .tree_controller import MeetingTreeController
from .overview import _Overview
from .visuals import (
    _ACCENT,
    _BG,
    _GOLD,
    _PURPLE,
)
from .week_nav import WeekNavBar, _WeekPicker
from ..playlist.edit_visuals import PlaylistIconProvider, PlaylistThumbnailProvider
from ...ui.media_info import MediaInfoQueue

if TYPE_CHECKING:
    from ...core.ingest.watched_folder_files import WatchedFolderFileStore
    from ...core.ingest.watched_folder import WatchedFolderWatcher
    from ...core.jw.catalog import JWMediaCatalogService
    from ...core.media.profile_store import ProfileMediaStore
    from ...core.media.thumbnail_store import ThumbnailStore
    from ...core.meetings.memorial import MemorialService
    from ...core.meetings.publications import JwpubService


def _meeting_drop_exts() -> frozenset[str]:
    exts = _MEDIA_EXTS | _PDF_EXTS | _JWPUB_EXTS | _PLAYLIST_EXTS
    if libreoffice_available():
        exts = exts | _PPTX_EXTS | _DOCX_EXTS
    return frozenset(exts)


def _tr_ctx(context: str, source: str) -> str:
    return QCoreApplication.translate(context, source)


def _configure_songs_bridge_language(
    bridge: JWSongsBridge,
    context: JWMediaLanguageContext,
) -> None:
    bridge.set_language_context(
        api_code=context.api_code,
        fallback_code=context.fallback_code,
        is_sign_language=context.is_sign_language,
    )


# ── Study detail view (QML-based) ─────────────────────────────────────────────

class StudyDetailView(QWidget):
    back_requested = Signal()
    play_requested = Signal(object)

    def __init__(self, pub_type: str, wd: "WeekData",
                 service: "JwpubService", *,
                 language_context: JWMediaLanguageContext,
                 meeting_tree_store: MeetingTreeStore,
                 profile_media_store: ProfileMediaStore,
                 meeting_thumbnail_store: ThumbnailStore,
                 watched_folder_file_store: WatchedFolderFileStore,
                 profile_paths: ProfilePaths,
                 runtime_paths: RuntimePaths,
                 cache_manager: MediaCacheManager,
                 jw_catalog_cache_paths: JWMediaCatalogCachePaths,
                 jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
                 jw_songs_store: JWSongsStore,
                 meeting_schedule_settings: MeetingScheduleSettingsStore,
                 media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
                 watched_folder: str = "", parent=None):
        super().__init__(parent)
        self._pub   = pub_type
        self._wd    = wd
        self._svc   = service
        self._language_context = language_context
        self._meeting_tree_store = meeting_tree_store
        self._profile_media_store = profile_media_store
        self._meeting_thumbnail_store = meeting_thumbnail_store
        self._watched_folder_file_store = watched_folder_file_store
        self._profile_paths = profile_paths
        self._runtime_paths = runtime_paths
        self._cache_manager = cache_manager
        self._jw_catalog_cache_paths = jw_catalog_cache_paths
        self._jw_catalog_service_factory = jw_catalog_service_factory
        self._jw_songs_store = jw_songs_store
        self._meeting_schedule_settings = meeting_schedule_settings
        self._media_info_queue_factory = media_info_queue_factory
        self._watched_folder = watched_folder
        self._qml_pointer_depth = 0
        self._disposed = False
        self.setAcceptDrops(True)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        is_mwb = self._pub == "mwb"
        self.pill_color = _ACCENT if is_mwb else _PURPLE
        self._refresh_shell_texts(update_context=False)

        self.qml_widget = QQuickWidget()
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        self.qml_widget.setFormat(fmt)
        self.qml_widget.setParent(self)
        self.qml_widget.setClearColor(QColor("#0d1117"))
        self.qml_widget.setMouseTracking(True)
        self.qml_widget.setAcceptDrops(False)
        self.qml_widget.installEventFilter(self)

        lang_code = self._language_context.api_code
        self.controller = MeetingTreeController(
            self._svc,
            meeting_type=self._pub,
            language_code=lang_code,
            store=self._meeting_tree_store,
            profile_media_store=self._profile_media_store,
            meeting_thumbnail_store=self._meeting_thumbnail_store,
            watched_folder_file_store=self._watched_folder_file_store,
            profile_paths=self._profile_paths,
            runtime_paths=self._runtime_paths,
            cache_manager=self._cache_manager,
            schedule_settings=self._meeting_schedule_settings,
            media_info_queue_factory=self._media_info_queue_factory,
            fallback_language_code=self._language_context.fallback_code,
            parent=self,
        )
        self.controller.backRequested.connect(self.back_requested.emit)
        self.controller.projectRequested.connect(self.play_requested.emit)
        self.controller.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.controller.pointerExited.connect(self.end_qml_pointer_cursor)
        self.controller.set_sync_root(self._watched_folder)

        self.catalog_bridge = JWMediaCatalogBridge(
            self._jw_catalog_cache_paths,
            self._jw_catalog_service_factory,
            self,
        )
        self.catalog_bridge.set_language_code(lang_code)
        self.catalog_bridge.jwMediaConfirmed.connect(
            self.controller.add_from_jw_catalog
        )
        self.songs_bridge = JWSongsBridge(self._jw_songs_store, self)
        self._sync_songs_bridge_language()
        self.songs_bridge.jwMediaConfirmed.connect(
            self.controller.add_from_jw_catalog
        )
        self.controller.chromeChanged.connect(self._sync_catalog_placement)
        self.controller.stateChanged.connect(self._sync_catalog_placement)

        self.qml_widget.engine().addImageProvider(
            "playlistthumbs",
            PlaylistThumbnailProvider(
                self.controller.thumb_cache,
                self.controller.disk_thumbnail,
            ),
        )
        self.qml_widget.engine().addImageProvider("playlisticons", PlaylistIconProvider())

        ctx = self.qml_widget.rootContext()
        ctx.setContextProperty("controller", self.controller)
        ctx.setContextProperty("catalogBridge", self.catalog_bridge)
        ctx.setContextProperty("songsBridge", self.songs_bridge)
        ctx.setContextProperty("meetingPill", self.pill_text)
        ctx.setContextProperty("meetingDate", self.date_text)
        ctx.setContextProperty("pillColor", self.pill_color)
        ctx.setContextProperty("noItemsText", self.no_items_text)
        
        load_qml_type(self.qml_widget, "MeetingDetailView")
        self.qml_widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        
        root.addWidget(self.qml_widget, stretch=1)
        self.controller.load_week(self._pub, self._wd)
        if self._watched_folder:
            self.controller.inject_linked_folder_media(self._watched_folder)
        self._sync_catalog_placement()

    def _populate(self):
        if hasattr(self, "controller"):
            self.controller.set_sync_root(self._watched_folder)
            self.controller.refresh_week(self._pub, self._wd)
            if self._watched_folder:
                self.controller.inject_linked_folder_media(self._watched_folder)

    def update_week(self, wd: "WeekData"):
        """
        Re-render this open detail with a freshly-loaded WeekData (e.g. when a
        background revalidation found a server-side update). Edits the user made
        are preserved by MeetingTreeMerger, so the merge happens in place even
        while the detail is being viewed — no jump back to the overview.
        """
        self._wd = wd
        self._refresh_shell_texts()
        self._populate()

    def refresh_cbs(self):
        self._populate()

    def _sync_catalog_placement(self):
        if not hasattr(self, "controller"):
            return
        playlist_ref = self.controller.placement_playlist_ref()
        if hasattr(self, "catalog_bridge"):
            self.catalog_bridge.set_playlist_ref(playlist_ref)
        if hasattr(self, "songs_bridge"):
            self.songs_bridge.set_playlist_ref(playlist_ref)

    def _sync_songs_bridge_language(self):
        if not hasattr(self, "songs_bridge"):
            return
        _configure_songs_bridge_language(self.songs_bridge, self._language_context)

    def _refresh_shell_texts(self, *, update_context: bool = True):
        is_mwb = self._pub == "mwb"
        self.pill_text = (
            _tr_ctx("_PubCard", "LIFE & MINISTRY")
            if is_mwb else _tr_ctx("_PubCard", "WATCHTOWER STUDY")
        )
        date_lbl = week_label(self._wd.monday)
        if self._wd.mwb_date_label:
            date_lbl = self._wd.mwb_date_label
        self.date_text = date_lbl
        self.no_items_text = _tr_ctx(
            "StudyDetailView", "No media items found for this week."
        )
        if update_context and hasattr(self, "qml_widget"):
            ctx = self.qml_widget.rootContext()
            ctx.setContextProperty("meetingPill", self.pill_text)
            ctx.setContextProperty("meetingDate", self.date_text)
            ctx.setContextProperty("noItemsText", self.no_items_text)
            engine = self.qml_widget.engine()
            if hasattr(engine, "retranslate"):
                engine.retranslate()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self._refresh_shell_texts()
        super().changeEvent(event)

    def begin_qml_pointer_cursor(self):
        self._qml_pointer_depth += 1
        begin_qml_pointer_cursor(self.qml_widget)

    def end_qml_pointer_cursor(self):
        self._qml_pointer_depth = max(0, self._qml_pointer_depth - 1)
        if self._qml_pointer_depth == 0:
            end_qml_pointer_cursor(self.qml_widget)

    def _reset_qml_pointer_cursor(self):
        self._qml_pointer_depth = 0
        end_qml_pointer_cursor(self.qml_widget)

    def eventFilter(self, obj, event):
        if obj is getattr(self, "qml_widget", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    def _has_valid_urls(self, mime_data) -> bool:
        accepted = _meeting_drop_exts()
        return bool(
            mime_data
            and mime_data.hasUrls()
            and any(
                Path(url.toLocalFile()).suffix.lower() in accepted
                for url in mime_data.urls()
                if url.isLocalFile()
            )
        )

    def dragEnterEvent(self, event):
        if self._has_valid_urls(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if not self._has_valid_urls(event.mimeData()):
            super().dragMoveEvent(event)
            return
        event.acceptProposedAction()
        root_obj = self.qml_widget.rootObject()
        if root_obj:
            local_pos = self.qml_widget.mapFrom(self, event.position().toPoint())
            delegate_idx = root_obj.getIndexAt(local_pos.y())
            root_obj.setProperty("dropIndicatorIndex", delegate_idx)

    def dragLeaveEvent(self, event):
        root_obj = self.qml_widget.rootObject()
        if root_obj:
            root_obj.clearExternalDropPreview()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if not self._has_valid_urls(event.mimeData()):
            super().dropEvent(event)
            return
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
            and Path(url.toLocalFile()).suffix.lower() in _meeting_drop_exts()
        ]
        list_id = "root"
        insert_idx = 2**31 - 1
        root_obj = self.qml_widget.rootObject()
        if root_obj:
            local_pos = self.qml_widget.mapFrom(self, event.position().toPoint())
            root_obj.getIndexAt(local_pos.y())
            list_id = root_obj.property("externalDropListId") or "root"
            value = root_obj.property("externalDropIndex")
            if isinstance(value, int) and value >= 0:
                insert_idx = value
            root_obj.clearExternalDropPreview()
        self.controller.add_files(paths, list_id, insert_idx)
        event.acceptProposedAction()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        if hasattr(self, "controller"):
            self.controller.cleanup()
        if hasattr(self, "catalog_bridge"):
            self.catalog_bridge.cleanup()
        if hasattr(self, "songs_bridge"):
            self.songs_bridge.cleanup()
        if hasattr(self, "qml_widget"):
            try:
                self.qml_widget.removeEventFilter(self)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                log_ignored_exception(__name__, "Could not remove meeting overview event filter")
            self.qml_widget.setSource(QUrl())

    def deleteLater(self):
        self.dispose()
        super().deleteLater()


# ── Memorial detail view (QML-based) ──────────────────────────────────────────

class _MemorialDetailView(QWidget):
    back_requested = Signal()
    play_requested = Signal(object)

    def __init__(self, md: "MemorialData", service: "JwpubService", *,
                 language_context: JWMediaLanguageContext,
                 meeting_tree_store: MeetingTreeStore,
                 profile_media_store: ProfileMediaStore,
                 meeting_thumbnail_store: ThumbnailStore,
                 watched_folder_file_store: WatchedFolderFileStore,
                 profile_paths: ProfilePaths,
                 runtime_paths: RuntimePaths,
                 cache_manager: MediaCacheManager,
                 jw_catalog_cache_paths: JWMediaCatalogCachePaths,
                 jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
                 jw_songs_store: JWSongsStore,
                 meeting_schedule_settings: MeetingScheduleSettingsStore,
                 media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
                 parent=None):
        super().__init__(parent)
        self._md  = md
        self._svc = service
        self._language_context = language_context
        self._meeting_tree_store = meeting_tree_store
        self._profile_media_store = profile_media_store
        self._meeting_thumbnail_store = meeting_thumbnail_store
        self._watched_folder_file_store = watched_folder_file_store
        self._profile_paths = profile_paths
        self._runtime_paths = runtime_paths
        self._cache_manager = cache_manager
        self._jw_catalog_cache_paths = jw_catalog_cache_paths
        self._jw_catalog_service_factory = jw_catalog_service_factory
        self._jw_songs_store = jw_songs_store
        self._meeting_schedule_settings = meeting_schedule_settings
        self._media_info_queue_factory = media_info_queue_factory
        self._qml_pointer_depth = 0
        self._disposed = False
        self.setAcceptDrops(True)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.pill_color = _GOLD
        self._refresh_shell_texts(update_context=False)

        self.qml_widget = QQuickWidget()
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        self.qml_widget.setFormat(fmt)
        self.qml_widget.setParent(self)
        self.qml_widget.setClearColor(QColor("#0d1117"))
        self.qml_widget.setMouseTracking(True)
        self.qml_widget.setAcceptDrops(False)
        self.qml_widget.installEventFilter(self)

        lang_code = self._language_context.api_code
        self.controller = MeetingTreeController(
            self._svc,
            meeting_type="memorial",
            language_code=lang_code,
            store=self._meeting_tree_store,
            profile_media_store=self._profile_media_store,
            meeting_thumbnail_store=self._meeting_thumbnail_store,
            watched_folder_file_store=self._watched_folder_file_store,
            profile_paths=self._profile_paths,
            runtime_paths=self._runtime_paths,
            cache_manager=self._cache_manager,
            schedule_settings=self._meeting_schedule_settings,
            media_info_queue_factory=self._media_info_queue_factory,
            fallback_language_code=self._language_context.fallback_code,
            parent=self,
        )
        self.controller.backRequested.connect(self.back_requested.emit)
        self.controller.projectRequested.connect(self.play_requested.emit)
        self.controller.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.controller.pointerExited.connect(self.end_qml_pointer_cursor)

        self.catalog_bridge = JWMediaCatalogBridge(
            self._jw_catalog_cache_paths,
            self._jw_catalog_service_factory,
            self,
        )
        self.catalog_bridge.set_language_code(lang_code)
        self.catalog_bridge.jwMediaConfirmed.connect(
            self.controller.add_from_jw_catalog
        )
        self.songs_bridge = JWSongsBridge(self._jw_songs_store, self)
        self._sync_songs_bridge_language()
        self.songs_bridge.jwMediaConfirmed.connect(
            self.controller.add_from_jw_catalog
        )
        self.controller.chromeChanged.connect(self._sync_catalog_placement)
        self.controller.stateChanged.connect(self._sync_catalog_placement)

        self.qml_widget.engine().addImageProvider(
            "playlistthumbs",
            PlaylistThumbnailProvider(
                self.controller.thumb_cache,
                self.controller.disk_thumbnail,
            ),
        )
        self.qml_widget.engine().addImageProvider("playlisticons", PlaylistIconProvider())

        ctx = self.qml_widget.rootContext()
        ctx.setContextProperty("controller", self.controller)
        ctx.setContextProperty("catalogBridge", self.catalog_bridge)
        ctx.setContextProperty("songsBridge", self.songs_bridge)
        ctx.setContextProperty("meetingPill", self.pill_text)
        ctx.setContextProperty("meetingDate", self.date_text)
        ctx.setContextProperty("pillColor", self.pill_color)
        ctx.setContextProperty("noItemsText", self.no_items_text)
        
        load_qml_type(self.qml_widget, "MeetingDetailView")
        self.qml_widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        
        root.addWidget(self.qml_widget, stretch=1)
        self.controller.load_memorial(self._md)
        self._sync_catalog_placement()

    def _populate(self):
        if hasattr(self, "controller"):
            self.controller.load_memorial(self._md)

    def _sync_catalog_placement(self):
        if not hasattr(self, "controller"):
            return
        playlist_ref = self.controller.placement_playlist_ref()
        if hasattr(self, "catalog_bridge"):
            self.catalog_bridge.set_playlist_ref(playlist_ref)
        if hasattr(self, "songs_bridge"):
            self.songs_bridge.set_playlist_ref(playlist_ref)

    def _sync_songs_bridge_language(self):
        if not hasattr(self, "songs_bridge"):
            return
        _configure_songs_bridge_language(self.songs_bridge, self._language_context)

    def _refresh_shell_texts(self, *, update_context: bool = True):
        self.pill_text = _tr_ctx("_MemorialCard", "MEMORIAL")
        self.date_text = (
            format_single_date(self._md.memorial_date)
            if self._md.memorial_date else ""
        )
        self.no_items_text = _tr_ctx("_MemorialDetailView", "No media items found.")
        if update_context and hasattr(self, "qml_widget"):
            ctx = self.qml_widget.rootContext()
            ctx.setContextProperty("meetingPill", self.pill_text)
            ctx.setContextProperty("meetingDate", self.date_text)
            ctx.setContextProperty("noItemsText", self.no_items_text)
            engine = self.qml_widget.engine()
            if hasattr(engine, "retranslate"):
                engine.retranslate()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self._refresh_shell_texts()
        super().changeEvent(event)

    def begin_qml_pointer_cursor(self):
        self._qml_pointer_depth += 1
        begin_qml_pointer_cursor(self.qml_widget)

    def end_qml_pointer_cursor(self):
        self._qml_pointer_depth = max(0, self._qml_pointer_depth - 1)
        if self._qml_pointer_depth == 0:
            end_qml_pointer_cursor(self.qml_widget)

    def _reset_qml_pointer_cursor(self):
        self._qml_pointer_depth = 0
        end_qml_pointer_cursor(self.qml_widget)

    def eventFilter(self, obj, event):
        if obj is getattr(self, "qml_widget", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    def _has_valid_urls(self, mime_data) -> bool:
        accepted = _meeting_drop_exts()
        return bool(
            mime_data
            and mime_data.hasUrls()
            and any(
                Path(url.toLocalFile()).suffix.lower() in accepted
                for url in mime_data.urls()
                if url.isLocalFile()
            )
        )

    def dragEnterEvent(self, event):
        if self._has_valid_urls(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if not self._has_valid_urls(event.mimeData()):
            super().dragMoveEvent(event)
            return
        event.acceptProposedAction()
        root_obj = self.qml_widget.rootObject()
        if root_obj:
            local_pos = self.qml_widget.mapFrom(self, event.position().toPoint())
            delegate_idx = root_obj.getIndexAt(local_pos.y())
            root_obj.setProperty("dropIndicatorIndex", delegate_idx)

    def dragLeaveEvent(self, event):
        root_obj = self.qml_widget.rootObject()
        if root_obj:
            root_obj.clearExternalDropPreview()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if not self._has_valid_urls(event.mimeData()):
            super().dropEvent(event)
            return
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
            and Path(url.toLocalFile()).suffix.lower() in _meeting_drop_exts()
        ]
        list_id = "root"
        insert_idx = 2**31 - 1
        root_obj = self.qml_widget.rootObject()
        if root_obj:
            local_pos = self.qml_widget.mapFrom(self, event.position().toPoint())
            root_obj.getIndexAt(local_pos.y())
            list_id = root_obj.property("externalDropListId") or "root"
            value = root_obj.property("externalDropIndex")
            if isinstance(value, int) and value >= 0:
                insert_idx = value
            root_obj.clearExternalDropPreview()
        self.controller.add_files(paths, list_id, insert_idx)
        event.acceptProposedAction()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        if hasattr(self, "controller"):
            self.controller.cleanup()
        if hasattr(self, "catalog_bridge"):
            self.catalog_bridge.cleanup()
        if hasattr(self, "songs_bridge"):
            self.songs_bridge.cleanup()
        if hasattr(self, "qml_widget"):
            try:
                self.qml_widget.removeEventFilter(self)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                log_ignored_exception(__name__, "Could not remove meeting detail event filter")
            self.qml_widget.setSource(QUrl())

    def deleteLater(self):
        self.dispose()
        super().deleteLater()
# ── Main widget ───────────────────────────────────────────────────────────────

class MeetingsWidget(QWidget):
    """
    Main meetings page.
    Stack index 0 = overview  (navbar visible)
    Stack index 1+ = detail views (navbar hidden)
    """
    project_media = Signal(object)  # MeetingMedia

    def __init__(
        self,
        lang_manager=None,
        *,
        meeting_tree_store: MeetingTreeStore,
        profile_media_store: ProfileMediaStore,
        meeting_thumbnail_store: ThumbnailStore,
        watched_folder_file_store: WatchedFolderFileStore,
        watched_folder_watcher_factory: Callable[[QObject], WatchedFolderWatcher],
        profile_paths: ProfilePaths,
        runtime_paths: RuntimePaths,
        cache_manager: MediaCacheManager,
        jw_catalog_cache_paths: JWMediaCatalogCachePaths,
        jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
        jw_songs_store: JWSongsStore,
        media_settings: MediaSettingsStore,
        meeting_schedule_settings: MeetingScheduleSettingsStore,
        jwpub_service_factory: Callable[[QObject], JwpubService],
        memorial_service_factory: Callable[[QObject], MemorialService],
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        parent=None,
    ):
        super().__init__(parent)
        self._lang_mgr  = lang_manager
        self._monday    = current_monday()
        self._cache:   dict[str, WeekData]       = {}
        self._details: dict[str, StudyDetailView] = {}
        self._clear_details_pending = False
        self._watched_folder: str = ""
        self._meeting_tree_store = meeting_tree_store
        self._profile_media_store = profile_media_store
        self._meeting_thumbnail_store = meeting_thumbnail_store
        self._watched_folder_file_store = watched_folder_file_store
        self._watched_folder_watcher_factory = watched_folder_watcher_factory
        self._profile_paths = profile_paths
        self._runtime_paths = runtime_paths
        self._cache_manager = cache_manager
        self._jw_catalog_cache_paths = jw_catalog_cache_paths
        self._jw_catalog_service_factory = jw_catalog_service_factory
        self._jw_songs_store = jw_songs_store
        self._media_settings = media_settings
        self._meeting_schedule_settings = meeting_schedule_settings
        self._media_info_queue_factory = media_info_queue_factory

        self._service = jwpub_service_factory(self)
        self._set_lang_from_mgr()

        self._service.mwb_ready.connect(self._on_mwb_ready)
        self._service.wt_ready.connect(self._on_wt_ready)
        self._service.cbs_ready.connect(self._on_cbs_ready)
        self._service.progress.connect(self._on_progress)
        self._service.error_sig.connect(self._on_error)

        # ── Memorial service ───────────────────────────────────────────────────
        self._memorial_svc = memorial_service_factory(self)
        self._set_memorial_lang_from_mgr()
        self._memorial_svc.memorial_ready.connect(self._on_memorial_ready)
        self._memorial_svc.memorial_status.connect(self._on_memorial_status)
        self._memorial_svc.memorial_progress.connect(self._on_memorial_progress)

        if lang_manager and hasattr(lang_manager, "language_changed"):
            lang_manager.language_changed.connect(self._on_lang_changed)

        # Recarrega quando o idioma de mídia JW muda (independente da UI)
        if lang_manager and hasattr(lang_manager, "jw_lang_service"):
            lang_manager.jw_lang_service.media_language_changed.connect(
                self._on_media_lang_changed
            )

        self._build()
        self._navigate_to(self._monday, reload=True)
        self._auto_download_timer = QTimer(self)
        self._auto_download_timer.setSingleShot(True)
        self._auto_download_timer.timeout.connect(self._service.auto_download_if_enabled)
        self._auto_download_timer.start(3000)

        self._folder_watcher = self._watched_folder_watcher_factory(self)
        self._folder_watcher.changed.connect(self._on_folder_changed)
        self._folder_watcher.subfolder_changed.connect(self._on_folder_changed)
        self._wf_debounce = QTimer(self)
        self._wf_debounce.setSingleShot(True)
        self._wf_debounce.setInterval(600)
        self._wf_debounce.timeout.connect(self._do_folder_refresh)

    def _current_media_context(self) -> JWMediaLanguageContext:
        return jw_media_language_context(self._lang_mgr, default_api_code="T")

    def _set_lang_from_mgr(self):
        context = self._current_media_context()
        self._service.set_lang(context.api_code)
        self._service.set_sign_language(context.is_sign_language)

    def _set_memorial_lang_from_mgr(self):
        """
        Aplica o idioma de mídia JW ao serviço do Memorial.
        Sempre usa o idioma de mídia efetivo, não o fallback da interface.
        """
        context = self._current_media_context()
        self._memorial_svc.set_lang(context.api_code)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._navbar = WeekNavBar()
        self._navbar.prev_week.connect(self._go_prev)
        self._navbar.next_week.connect(self._go_next)
        self._navbar.home_requested.connect(self._go_home)
        self._navbar.pick_requested.connect(self._show_picker)
        root.addWidget(self._navbar)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background:{_BG};")
        self._overview = _Overview()
        self._overview.open_mwb.connect(lambda: self._open_detail("mwb"))
        self._overview.open_wt.connect(lambda: self._open_detail("wt"))
        self._overview.open_memorial.connect(self._open_memorial_detail)
        self._stack.addWidget(self._overview)
        root.addWidget(self._stack, stretch=1)

        self._navbar.update_week(self._monday)

    # ── Navigation ────────────────────────────────────────────────────────────

    def _go_prev(self):
        self._go_overview()
        self._navigate_to(self._monday - timedelta(weeks=1))

    def _go_next(self):
        self._go_overview()
        self._navigate_to(self._monday + timedelta(weeks=1))

    def _go_home(self):
        self._go_overview()
        self._navigate_to(current_monday())

    def _go_overview(self):
        self._stack.setCurrentIndex(0)
        self._navbar.setVisible(True)
        self._schedule_clear_details()

    def _schedule_clear_details(self):
        if self._clear_details_pending:
            return
        self._clear_details_pending = True
        QTimer.singleShot(0, self._clear_details)

    def _discard_detail(self, detail_key: str):
        detail = self._details.pop(detail_key, None)
        if detail is None:
            return
        if self._stack.currentWidget() is detail:
            self._stack.setCurrentIndex(0)
            self._navbar.setVisible(True)
        self._stack.removeWidget(detail)
        if hasattr(detail, "dispose"):
            detail.dispose()
        detail.deleteLater()

    def _clear_details(self):
        self._clear_details_pending = False
        for detail_key in list(self._details.keys()):
            self._discard_detail(detail_key)

    def _show_picker(self):
        popup = _WeekPicker(self._monday, cache=self._cache)
        popup.week_selected.connect(self._on_week_picked)
        popup.show_near(self._navbar)

    @Slot(object)
    def _on_week_picked(self, monday: date):
        self._go_overview()
        self._navigate_to(monday)

    def _navigate_to(self, monday: date, reload: bool = False):
        self._monday = monday
        wd = self._cache.get(monday.isoformat())
        db_label = (wd.mwb_date_label or wd.mwb_week_title) if wd else ""
        self._navbar.update_week(monday, db_label)
        self._refresh_overview_cards(monday)
        if reload or not self._service.get_week_data(monday):
            self._service.load_week(monday, force=reload)
        # Carrega mídias do Memorial se estamos na semana correta
        # (MemorialService decide internamente se está na janela de 7 dias)
        if self._memorial_svc.is_memorial_week(monday):
            self._memorial_svc.load(force=reload)

    def _refresh_overview_cards(self, monday: date):
        key = monday.isoformat()
        wd  = self._cache.get(key)
        if not wd:
            self._overview.mwb_card.set_loading()
            self._overview.wt_card.set_loading()
        else:
            if wd.mwb_status == "ready":
                self._overview.mwb_card.set_ready(wd)
            elif wd.mwb_status == "empty":
                self._overview.mwb_card.set_empty()
            elif wd.mwb_status == "not_found":
                self._overview.mwb_card.set_not_found()
            elif wd.mwb_status == "error":
                self._overview.mwb_card.set_error()
            else:
                self._overview.mwb_card.set_loading()

            if wd.wt_status == "ready":
                self._overview.wt_card.set_ready(wd)
            elif wd.wt_status == "empty":
                self._overview.wt_card.set_empty()
            elif wd.wt_status == "not_found":
                self._overview.wt_card.set_not_found()
            elif wd.wt_status == "error":
                self._overview.wt_card.set_error()
            else:
                self._overview.wt_card.set_loading()

        # ── Memorial card ──────────────────────────────────────────────────────
        is_memorial_week = self._memorial_svc.is_memorial_week(monday)
        mc = self._overview.memorial_card
        if not is_memorial_week:
            mc.setVisible(False)
        else:
            mc.setVisible(True)
            md = self._memorial_svc.get_data()
            if md is None:
                mc.set_loading()
            elif md.status == "ready":
                mc.set_ready(md)
            elif md.status in ("not_yet", "past"):
                mc.setVisible(False)
            elif md.status == "not_found":
                mc.set_not_found()
            elif md.status == "deleted":
                mc.set_deleted()
            elif md.status == "empty":
                mc.set_empty()
            elif md.status == "error":
                mc.set_error()
            else:
                mc.set_loading()

    # ── Detail ────────────────────────────────────────────────────────────────

    def _open_detail(self, pub_type: str):
        key    = self._monday.isoformat()
        wd     = self._cache.get(key)
        if not wd:
            return
        status = wd.mwb_status if pub_type == "mwb" else wd.wt_status

        # Error / not-found: retry the load instead of silently ignoring the click.
        if status in ("error", "not_found"):
            card = self._overview.mwb_card if pub_type == "mwb" else self._overview.wt_card
            card.set_loading()
            self._service.load_week(self._monday, force=True)
            return

        # Still loading, empty, or in an unknown transient state — do nothing.
        if status in ("empty", "loading", "idle"):
            return

        # Status is "ready" — open the detail view.
        detail_key = f"{pub_type}:{key}"
        if detail_key not in self._details:
            d = StudyDetailView(pub_type, wd, self._service,
                                language_context=self._current_media_context(),
                                meeting_tree_store=self._meeting_tree_store,
                                profile_media_store=self._profile_media_store,
                                meeting_thumbnail_store=self._meeting_thumbnail_store,
                                watched_folder_file_store=self._watched_folder_file_store,
                                profile_paths=self._profile_paths,
                                runtime_paths=self._runtime_paths,
                                cache_manager=self._cache_manager,
                                jw_catalog_cache_paths=self._jw_catalog_cache_paths,
                                jw_catalog_service_factory=self._jw_catalog_service_factory,
                                jw_songs_store=self._jw_songs_store,
                                meeting_schedule_settings=self._meeting_schedule_settings,
                                media_info_queue_factory=self._media_info_queue_factory,
                                watched_folder=self._watched_folder)
            d.back_requested.connect(self._on_detail_back)
            d.play_requested.connect(self.project_media)
            self._stack.addWidget(d)
            self._details[detail_key] = d

        self._navbar.setVisible(False)
        self._stack.setCurrentWidget(self._details[detail_key])

    def _on_detail_back(self):
        self._go_overview()

    def _open_memorial_detail(self):
        md = self._memorial_svc.get_data()

        # Error / not-found: retry the load instead of silently ignoring the click.
        if md and md.status in ("error", "not_found"):
            self._overview.memorial_card.set_loading()
            self._memorial_svc.load(force=True)
            return

        if not md or md.status != "ready":
            return

        detail_key = f"memorial:{self._monday.isoformat()}"
        if detail_key not in self._details:
            d = _MemorialDetailView(
                md,
                self._service,
                language_context=self._current_media_context(),
                meeting_tree_store=self._meeting_tree_store,
                profile_media_store=self._profile_media_store,
                meeting_thumbnail_store=self._meeting_thumbnail_store,
                watched_folder_file_store=self._watched_folder_file_store,
                profile_paths=self._profile_paths,
                runtime_paths=self._runtime_paths,
                cache_manager=self._cache_manager,
                jw_catalog_cache_paths=self._jw_catalog_cache_paths,
                jw_catalog_service_factory=self._jw_catalog_service_factory,
                jw_songs_store=self._jw_songs_store,
                meeting_schedule_settings=self._meeting_schedule_settings,
                media_info_queue_factory=self._media_info_queue_factory,
            )
            d.back_requested.connect(self._on_detail_back)
            d.play_requested.connect(self.project_media)
            self._stack.addWidget(d)
            self._details[detail_key] = d

        self._navbar.setVisible(False)
        self._stack.setCurrentWidget(self._details[detail_key])

    # ── Service callbacks ─────────────────────────────────────────────────────

    @Slot(str, object)
    def _on_mwb_ready(self, key: str, wd: "WeekData"):
        self._cache[key] = wd
        # If a detail is open, refresh it in place (merging the user's edits)
        # instead of discarding it — so a background update never yanks the user
        # back to the overview. On the first (cache) emit no detail exists yet.
        detail = self._details.get(f"mwb:{key}")
        if detail is not None:
            detail.update_week(wd)
        if key == self._monday.isoformat():
            self._navbar.update_week(
                self._monday, wd.mwb_date_label or wd.mwb_week_title
            )
            self._refresh_overview_cards(self._monday)

    @Slot(str, object)
    def _on_wt_ready(self, key: str, wd: "WeekData"):
        self._cache[key] = wd
        detail = self._details.get(f"wt:{key}")
        if detail is not None:
            detail.update_week(wd)
        if key == self._monday.isoformat():
            self._refresh_overview_cards(self._monday)

    @Slot(str, object)
    def _on_cbs_ready(self, key: str, wd: "WeekData"):
        self._cache[key] = wd
        if key == self._monday.isoformat():
            self._overview.mwb_card.set_ready(wd)
        dk = f"mwb:{key}"
        detail = self._details.get(dk)
        if detail:
            detail.refresh_cbs()

    @Slot(str, str, int)
    def _on_progress(self, key: str, pub: str, pct: int):
        if key != self._monday.isoformat():
            return
        if pub == "mwb":
            self._overview.mwb_card.set_progress(pct)
        else:
            self._overview.wt_card.set_progress(pct)

    @Slot(str, str, str)
    def _on_error(self, key: str, pub: str, _msg: str):
        if key != self._monday.isoformat():
            return
        is_not_found = (_msg == "NOT_FOUND")
        if pub == "mwb":
            if is_not_found:
                self._overview.mwb_card.set_not_found()
            else:
                self._overview.mwb_card.set_error()
        else:
            if is_not_found:
                self._overview.wt_card.set_not_found()
            else:
                self._overview.wt_card.set_error()

    # ── Memorial callbacks ────────────────────────────────────────────────────

    @Slot(object)
    def _on_memorial_ready(self, md: "MemorialData"):
        """Memorial data carregada — atualiza card se estamos na semana certa."""
        if self._memorial_svc.is_memorial_week(self._monday):
            mc = self._overview.memorial_card
            mc.setVisible(True)
            mc.set_ready(md)
            # Invalida detalhe cacheado para forçar rebuild com dados frescos
            self._discard_detail(f"memorial:{self._monday.isoformat()}")

    @Slot(str)
    def _on_memorial_status(self, status: str):
        if not self._memorial_svc.is_memorial_week(self._monday):
            return
        mc = self._overview.memorial_card
        if status in ("not_yet", "past"):
            mc.setVisible(False)
        elif status == "empty":
            mc.setVisible(True)
            mc.set_empty()
        elif status == "not_found":
            mc.setVisible(True)
            mc.set_not_found()
        elif status == "deleted":
            mc.setVisible(True)
            mc.set_deleted()
        elif status == "error":
            mc.setVisible(True)
            mc.set_error()
        elif status == "loading":
            mc.setVisible(True)
            mc.set_loading()

    @Slot(int)
    def _on_memorial_progress(self, pct: int):
        if self._memorial_svc.is_memorial_week(self._monday):
            self._overview.memorial_card.set_progress(pct)

    # ── Language ──────────────────────────────────────────────────────────────

    @Slot(str)
    def _on_lang_changed(self, code: str):
        self._set_lang_from_mgr()
        self._set_memorial_lang_from_mgr()
        for key in list(self._cache.keys()):
            self._service.clear_week(date.fromisoformat(key))
        self._cache.clear()
        self._clear_details()
        self._overview.mwb_card.set_loading()
        self._overview.wt_card.set_loading()
        self._overview.memorial_card.set_loading()
        self._navigate_to(self._monday, reload=True)

    @Slot(str)
    def _on_media_lang_changed(self, _code: str):
        """
        Idioma de mídia JW mudou → recarrega reuniões com o novo código.
        Usa o contexto JW centralizado em vez do fallback da interface.
        """
        self._set_lang_from_mgr()
        self._set_memorial_lang_from_mgr()
        for key in list(self._cache.keys()):
            self._service.clear_week(date.fromisoformat(key))
        self._cache.clear()
        self._clear_details()
        self._overview.mwb_card.set_loading()
        self._overview.wt_card.set_loading()
        self._overview.memorial_card.set_loading()
        self._navigate_to(self._monday, reload=True)

    # ── Public ────────────────────────────────────────────────────────────────

    def set_watched_folder(self, path: str) -> None:
        """Update the linked-folder path used for meeting auto-assignment."""
        self._watched_folder = path or ""
        if hasattr(self, "_folder_watcher"):
            self._folder_watcher.set_root(self._watched_folder)
        for detail_view in self._details.values():
            if hasattr(detail_view, "controller"):
                detail_view.controller.set_sync_root(self._watched_folder)
        self._do_folder_refresh()

    def _on_folder_changed(self, *args):
        self._wf_debounce.start()

    def _do_folder_refresh(self):
        if not getattr(self, "_watched_folder", ""):
            return
        for detail_view in self._details.values():
            if hasattr(detail_view, "controller"):
                detail_view.controller.inject_linked_folder_media(self._watched_folder)

    def get_service(self) -> "JwpubService":
        return self._service

    def cleanup(self) -> None:
        """Para serviços com QThread antes da janela principal ser destruída."""
        timer = getattr(self, "_auto_download_timer", None)
        if timer:
            timer.stop()
        self._clear_details()
        try:
            self._service.shutdown(wait_ms=100, delete_when_stopped=True)
        except Exception:  # noqa: BLE001 - background service shutdown boundary
            log_ignored_exception(__name__, "Could not shut down meeting service")
        try:
            self._memorial_svc.shutdown(wait_ms=100, delete_when_stopped=True)
        except Exception:  # noqa: BLE001 - background service shutdown boundary
            log_ignored_exception(__name__, "Could not shut down memorial service")
