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
    QCoreApplication, QEvent,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QStackedWidget,
)

from ...core.meetings.meeting_weeks import (
    current_monday,
    is_selectable_meeting_week,
)
from ...core.meetings.models import MemorialData, WeekData
from ...core.meetings.preparation import (
    MeetingPreparationKey,
    MeetingPreparationPriority,
    MeetingPreparationRequest,
    MeetingPreparationService,
    MeetingPreparationState,
)
from ...core.i18n.date import week_label, format_single_date
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from ...core.foundation.constants import (
    DOCX_EXTS,
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
    PPTX_EXTS,
)
from ...core.media.formats import MEDIA_EXTS
from ...core.jw.language_context import (
    JWMediaLanguageContext,
    jw_media_language_context,
)
from ...core.jw.songs import JWSongsStore
from ...ui.helpers import (
    QmlPointerCursorState,
)
from ...ui.media_insertion_feedback import connect_media_picker_feedback
from ...core.meetings.tree_store import (
    MeetingTreeSnapshot,
    MeetingTreeStore,
    parse_meeting_tree_key,
)
from ...core.media.cache import MediaCacheManager
from ...core.media.settings import MediaSettingsStore
from solin.ui.qml.jw_media_catalog import JWMediaCatalogBridge
from solin.ui.qml.jw_songs import JWSongsBridge
from solin.ui.qml.meeting_detail import MeetingDetailQmlHost
from ...styles.theme import PALETTE
from .controller_factory import (
    MeetingTreeControllerDependencies,
    MeetingTreeControllerFactory,
)
from .destinations import MeetingDestinationSession
from .overview import Overview
from .visuals import (
    MEETING_PURPLE,
)
from .week_nav import WeekNavBar, WeekPicker
from ...ui.media_info import MediaInfoQueue
from ...ui.async_load import AsyncLoadHandle
from ...ui.incremental_load import IncrementalLoadHandle
from ...ui.loading_placeholder import DeferredLoadingPlaceholder


_POST_FRAME_TASK_FALLBACK_MS = 500

if TYPE_CHECKING:
    from ...core.ingest.watched_folder_files import WatchedFolderFileStore
    from ...core.ingest.watched_folder import WatchedFolderWatcher
    from ...core.jw.catalog_service import JWMediaCatalogService
    from ...core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from ...core.jw.thumbnail_fetch import JWCatalogThumbnailSessionFactory
    from ...core.media.profile_store import ProfileMediaStore
    from ...core.media.thumbnail_store import ThumbnailStore
    from ...core.meetings.linked_folder_sync import MeetingLinkedFolderSync
    from ...core.meetings.memorial import MemorialService
    from ...core.meetings.publications import JwpubService
    from ...core.rendering.document_conversion import DocumentConversionService


def _meeting_drop_exts(
    document_conversion_service: DocumentConversionService,
) -> frozenset[str]:
    exts = MEDIA_EXTS | PDF_EXTS | JWPUB_EXTS | PLAYLIST_EXTS
    if document_conversion_service.office_conversion_available():
        exts = exts | PPTX_EXTS | DOCX_EXTS
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


def _notify_meeting_tree_save_failed(
    notifications,
    tree_key: str,
    error: str,
) -> None:
    if notifications is None:
        return
    detail = (error or "").strip() or _tr_ctx(
        "MeetingTreeStorage",
        "Unknown storage error.",
    )
    if len(detail) > 500:
        detail = f"{detail[:497]}..."
    message = _tr_ctx(
        "MeetingTreeStorage",
        "Could not save meeting changes.\nFree up disk space or check permissions, then try again.\n%1",
    ).replace("%1", detail)
    notifications.error(
        message,
        title=_tr_ctx("MeetingTreeStorage", "Meeting changes not saved"),
        dedupe_key=f"meeting-tree-save:{tree_key}:{detail}",
    )


# ── Study detail view (QML-based) ─────────────────────────────────────────────

class StudyDetailView(QWidget):
    back_requested = Signal()
    play_requested = Signal(object)
    meeting_tree_saved = Signal(str)

    def __init__(self, pub_type: str, wd: "WeekData", *,
                 notifications=None,
                 playback_protection,
                 language_context: JWMediaLanguageContext,
                 controller_factory: MeetingTreeControllerFactory,
                 document_conversion_service: DocumentConversionService,
                 jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
                 jw_catalog_thumbnail_session_factory: JWCatalogThumbnailSessionFactory,
                 jw_songs_store: JWSongsStore,
                 saved_snapshot: MeetingTreeSnapshot,
                 meeting_tree_saved_handler: Callable[[str], None] | None = None,
                 watched_folder: str = "",
                 parent=None):
        super().__init__(parent)
        self._pub   = pub_type
        self._wd    = wd
        self._notifications = notifications
        self._playback_protection = playback_protection
        self._language_context = language_context
        self._controller_factory = controller_factory
        self._document_conversion_service = document_conversion_service
        self._jw_catalog_service_factory = jw_catalog_service_factory
        self._jw_catalog_thumbnail_session_factory = (
            jw_catalog_thumbnail_session_factory
        )
        self._jw_songs_store = jw_songs_store
        self._meeting_tree_saved_handler = meeting_tree_saved_handler
        self._saved_snapshot = saved_snapshot
        self._watched_folder = watched_folder
        self._disposed = False
        self._post_frame_tasks_pending = False
        self._post_frame_window = None
        self.setAcceptDrops(True)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        is_mwb = self._pub == "mwb"
        self.pill_color = PALETTE.accent if is_mwb else MEETING_PURPLE
        self._refresh_shell_texts(update_context=False)

        lang_code = self._language_context.api_code
        self.controller = self._controller_factory.create(
            self._pub,
            self._language_context,
            parent=self,
        )
        self.controller.backRequested.connect(self.back_requested.emit)
        self.controller.projectRequested.connect(self.play_requested.emit)
        self.controller.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.controller.pointerCursorEntered.connect(
            self.begin_qml_shaped_pointer_cursor
        )
        self.controller.pointerCursorChanged.connect(self.update_qml_pointer_cursor)
        self.controller.pointerCursorExited.connect(
            self.end_qml_shaped_pointer_cursor
        )
        self.controller.pointerExited.connect(self.end_qml_pointer_cursor)
        self.controller.storageSaved.connect(self.meeting_tree_saved.emit)
        if self._meeting_tree_saved_handler is not None:
            self.controller.storageSaved.connect(self._meeting_tree_saved_handler)
        self.controller.storageSaveFailed.connect(self._on_storage_save_failed)
        self.controller.set_sync_root(self._watched_folder)
        loaded_snapshot = self.controller.load_saved_tree(
            self._saved_snapshot,
            start_media_requests=False,
        )
        if loaded_snapshot is not None:
            self._saved_snapshot = loaded_snapshot

        self.catalog_bridge = JWMediaCatalogBridge(
            self._jw_catalog_service_factory,
            self._jw_catalog_thumbnail_session_factory,
            insertion_handler=self.controller.add_from_jw_catalog,
            parent=self,
        )
        self.catalog_bridge.set_language_code(lang_code)
        self.songs_bridge = JWSongsBridge(
            self._jw_songs_store,
            insertion_handler=self.controller.add_from_jw_catalog,
            parent=self,
        )
        self._sync_songs_bridge_language()
        connect_media_picker_feedback(self.catalog_bridge, self._notifications)
        connect_media_picker_feedback(self.songs_bridge, self._notifications)
        self.controller.chromeChanged.connect(self._sync_catalog_placement)
        self.controller.stateChanged.connect(self._sync_catalog_placement)

        self.qml_widget = MeetingDetailQmlHost(
            controller=self.controller,
            catalog_bridge=self.catalog_bridge,
            songs_bridge=self.songs_bridge,
            meeting_pill=self.pill_text,
            meeting_date=self.date_text,
            pill_color=self.pill_color,
            no_items_text=self.no_items_text,
            playback_protection=self._playback_protection,
            parent=self,
        )
        self._qml_pointer_cursor = QmlPointerCursorState(self.qml_widget)
        self.qml_widget.installEventFilter(self)
        root.addWidget(self.qml_widget, stretch=1)
        self._schedule_post_frame_tasks()
        self._sync_catalog_placement()

    def _schedule_post_frame_tasks(self) -> None:
        if self._disposed or self._post_frame_tasks_pending:
            return
        self._post_frame_tasks_pending = True
        self._post_frame_window = self.qml_widget.quickWindow()
        if self._post_frame_window is not None:
            self._post_frame_window.frameSwapped.connect(
                self._on_first_detail_frame
            )
            self.qml_widget.update()
        QTimer.singleShot(
            _POST_FRAME_TASK_FALLBACK_MS,
            self._run_post_frame_tasks,
        )

    def _on_first_detail_frame(self) -> None:
        self._disconnect_post_frame_signal()
        QTimer.singleShot(0, self._run_post_frame_tasks)

    def _disconnect_post_frame_signal(self) -> None:
        window = self._post_frame_window
        self._post_frame_window = None
        if window is None:
            return
        try:
            window.frameSwapped.disconnect(self._on_first_detail_frame)
        except (RuntimeError, TypeError):
            log_ignored_exception(
                __name__,
                "Could not disconnect meeting detail frame callback",
            )

    def _run_post_frame_tasks(self) -> None:
        if self._disposed or not self._post_frame_tasks_pending:
            return
        self._post_frame_tasks_pending = False
        self._disconnect_post_frame_signal()
        self.controller.start_media_enrichment()
        if self._watched_folder:
            self.controller.inject_linked_folder_media(self._watched_folder)

    def update_snapshot(self, snapshot: MeetingTreeSnapshot) -> None:
        if self._disposed or snapshot.pub_type != self._pub:
            return
        if (
            snapshot.tree_key == self._saved_snapshot.tree_key
            and snapshot.revision <= self._saved_snapshot.revision
        ):
            return
        loaded_snapshot = self.controller.load_saved_tree(
            snapshot,
            start_media_requests=False,
        )
        if loaded_snapshot is None:
            return
        self._saved_snapshot = loaded_snapshot
        self._schedule_post_frame_tasks()
        self._sync_catalog_placement()

    def _sync_catalog_placement(self):
        if not hasattr(self, "controller"):
            return
        playlist_ref = self.controller.placement_playlist_ref()
        if hasattr(self, "catalog_bridge"):
            self.catalog_bridge.set_playlist_ref(playlist_ref)
        if hasattr(self, "songs_bridge"):
            self.songs_bridge.set_playlist_ref(playlist_ref)

    def _on_storage_save_failed(self, tree_key: str, error: str) -> None:
        _notify_meeting_tree_save_failed(self._notifications, tree_key, error)

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
            self.qml_widget.update_shell_texts(
                meeting_pill=self.pill_text,
                meeting_date=self.date_text,
                no_items_text=self.no_items_text,
            )

    def apply_theme(self) -> None:
        self.pill_color = PALETTE.accent if self._pub == "mwb" else MEETING_PURPLE
        if hasattr(self, "qml_widget"):
            self.qml_widget.set_pill_color(self.pill_color)
            self.qml_widget.apply_theme()
        if hasattr(self, "controller"):
            self.controller.refresh_theme()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self._refresh_shell_texts()
            if hasattr(self, "controller"):
                self.controller.refresh_language()
        super().changeEvent(event)

    def begin_qml_pointer_cursor(self):
        self._qml_pointer_cursor.enter()

    def begin_qml_shaped_pointer_cursor(
        self,
        cursor_source: str,
        cursor_shape: int,
    ):
        self._qml_pointer_cursor.enter_shaped(cursor_source, cursor_shape)

    def update_qml_pointer_cursor(
        self,
        cursor_source: str,
        cursor_shape: int,
    ):
        self._qml_pointer_cursor.update_shaped(cursor_source, cursor_shape)

    def end_qml_shaped_pointer_cursor(self, cursor_source: str):
        self._qml_pointer_cursor.exit_shaped(cursor_source)

    def end_qml_pointer_cursor(self):
        self._qml_pointer_cursor.exit()

    def _reset_qml_pointer_cursor(self):
        self._qml_pointer_cursor.reset()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "qml_widget", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    def _has_valid_urls(self, mime_data) -> bool:
        accepted = _meeting_drop_exts(self._document_conversion_service)
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
        self.qml_widget.preview_external_drop(
            self,
            event.position().toPoint(),
        )

    def dragLeaveEvent(self, event):
        self.qml_widget.clear_external_drop_preview()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if not self._has_valid_urls(event.mimeData()):
            super().dropEvent(event)
            return
        accepted = _meeting_drop_exts(self._document_conversion_service)
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
            and Path(url.toLocalFile()).suffix.lower() in accepted
        ]
        list_id, insert_idx, tree_id, structure_revision = (
            self.qml_widget.external_drop_target(
            self,
            event.position().toPoint(),
            )
        )
        if (
            tree_id != self.controller.treeIdentity()
            or structure_revision != self.controller.treeStructureRevision()
        ):
            event.ignore()
            return
        self.controller.add_files(paths, list_id, insert_idx)
        event.acceptProposedAction()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._post_frame_tasks_pending = False
        self._disconnect_post_frame_signal()
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
            self.qml_widget.clear_scene()

    def deleteLater(self):
        self.dispose()
        super().deleteLater()


# ── Memorial detail view (QML-based) ──────────────────────────────────────────

class _MemorialDetailView(QWidget):
    back_requested = Signal()
    play_requested = Signal(object)

    def __init__(self, md: "MemorialData", *,
                 notifications=None,
                 playback_protection,
                 language_context: JWMediaLanguageContext,
                 controller_factory: MeetingTreeControllerFactory,
                 document_conversion_service: DocumentConversionService,
                 jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
                 jw_catalog_thumbnail_session_factory: JWCatalogThumbnailSessionFactory,
                 jw_songs_store: JWSongsStore,
                 parent=None):
        super().__init__(parent)
        self._md  = md
        self._notifications = notifications
        self._playback_protection = playback_protection
        self._language_context = language_context
        self._controller_factory = controller_factory
        self._document_conversion_service = document_conversion_service
        self._jw_catalog_service_factory = jw_catalog_service_factory
        self._jw_catalog_thumbnail_session_factory = (
            jw_catalog_thumbnail_session_factory
        )
        self._jw_songs_store = jw_songs_store
        self._disposed = False
        self.setAcceptDrops(True)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.pill_color = PALETTE.warning
        self._refresh_shell_texts(update_context=False)

        lang_code = self._language_context.api_code
        self.controller = self._controller_factory.create(
            "memorial",
            self._language_context,
            parent=self,
        )
        self.controller.backRequested.connect(self.back_requested.emit)
        self.controller.projectRequested.connect(self.play_requested.emit)
        self.controller.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.controller.pointerCursorEntered.connect(
            self.begin_qml_shaped_pointer_cursor
        )
        self.controller.pointerCursorChanged.connect(self.update_qml_pointer_cursor)
        self.controller.pointerCursorExited.connect(
            self.end_qml_shaped_pointer_cursor
        )
        self.controller.pointerExited.connect(self.end_qml_pointer_cursor)
        self.controller.storageSaveFailed.connect(self._on_storage_save_failed)

        self.catalog_bridge = JWMediaCatalogBridge(
            self._jw_catalog_service_factory,
            self._jw_catalog_thumbnail_session_factory,
            insertion_handler=self.controller.add_from_jw_catalog,
            parent=self,
        )
        self.catalog_bridge.set_language_code(lang_code)
        self.songs_bridge = JWSongsBridge(
            self._jw_songs_store,
            insertion_handler=self.controller.add_from_jw_catalog,
            parent=self,
        )
        self._sync_songs_bridge_language()
        connect_media_picker_feedback(self.catalog_bridge, self._notifications)
        connect_media_picker_feedback(self.songs_bridge, self._notifications)
        self.controller.chromeChanged.connect(self._sync_catalog_placement)
        self.controller.stateChanged.connect(self._sync_catalog_placement)

        self.qml_widget = MeetingDetailQmlHost(
            controller=self.controller,
            catalog_bridge=self.catalog_bridge,
            songs_bridge=self.songs_bridge,
            meeting_pill=self.pill_text,
            meeting_date=self.date_text,
            pill_color=self.pill_color,
            no_items_text=self.no_items_text,
            playback_protection=self._playback_protection,
            parent=self,
        )
        self._qml_pointer_cursor = QmlPointerCursorState(self.qml_widget)
        self.qml_widget.installEventFilter(self)
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

    def _on_storage_save_failed(self, tree_key: str, error: str) -> None:
        _notify_meeting_tree_save_failed(self._notifications, tree_key, error)

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
            self.qml_widget.update_shell_texts(
                meeting_pill=self.pill_text,
                meeting_date=self.date_text,
                no_items_text=self.no_items_text,
            )

    def apply_theme(self) -> None:
        self.pill_color = PALETTE.warning
        if hasattr(self, "qml_widget"):
            self.qml_widget.set_pill_color(self.pill_color)
            self.qml_widget.apply_theme()
        if hasattr(self, "controller"):
            self.controller.refresh_theme()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self._refresh_shell_texts()
            if hasattr(self, "controller"):
                self.controller.refresh_language()
        super().changeEvent(event)

    def begin_qml_pointer_cursor(self):
        self._qml_pointer_cursor.enter()

    def begin_qml_shaped_pointer_cursor(
        self,
        cursor_source: str,
        cursor_shape: int,
    ):
        self._qml_pointer_cursor.enter_shaped(cursor_source, cursor_shape)

    def update_qml_pointer_cursor(
        self,
        cursor_source: str,
        cursor_shape: int,
    ):
        self._qml_pointer_cursor.update_shaped(cursor_source, cursor_shape)

    def end_qml_shaped_pointer_cursor(self, cursor_source: str):
        self._qml_pointer_cursor.exit_shaped(cursor_source)

    def end_qml_pointer_cursor(self):
        self._qml_pointer_cursor.exit()

    def _reset_qml_pointer_cursor(self):
        self._qml_pointer_cursor.reset()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "qml_widget", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    def _has_valid_urls(self, mime_data) -> bool:
        accepted = _meeting_drop_exts(self._document_conversion_service)
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
        self.qml_widget.preview_external_drop(
            self,
            event.position().toPoint(),
        )

    def dragLeaveEvent(self, event):
        self.qml_widget.clear_external_drop_preview()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        if not self._has_valid_urls(event.mimeData()):
            super().dropEvent(event)
            return
        accepted = _meeting_drop_exts(self._document_conversion_service)
        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
            and Path(url.toLocalFile()).suffix.lower() in accepted
        ]
        list_id, insert_idx, tree_id, structure_revision = (
            self.qml_widget.external_drop_target(
            self,
            event.position().toPoint(),
            )
        )
        if (
            tree_id != self.controller.treeIdentity()
            or structure_revision != self.controller.treeStructureRevision()
        ):
            event.ignore()
            return
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
            self.qml_widget.clear_scene()

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
    destinationTargetsChanged = Signal(str)

    def __init__(
        self,
        lang_manager=None,
        *,
        notifications=None,
        playback_protection,
        meeting_tree_store: MeetingTreeStore,
        profile_media_store: ProfileMediaStore,
        meeting_thumbnail_store: ThumbnailStore,
        watched_folder_file_store: WatchedFolderFileStore,
        jwpub_import_thread_factory: JwpubImportThreadFactory,
        document_conversion_service: DocumentConversionService,
        watched_folder_watcher_factory: Callable[[QObject], WatchedFolderWatcher],
        profile_paths: ProfilePaths,
        runtime_paths: RuntimePaths,
        cache_manager: MediaCacheManager,
        media_tree_runtime,
        jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
        jw_catalog_thumbnail_session_factory: JWCatalogThumbnailSessionFactory,
        jw_songs_store: JWSongsStore,
        media_settings: MediaSettingsStore,
        meeting_linked_folder_sync: MeetingLinkedFolderSync,
        publication_service: JwpubService,
        preparation_service: MeetingPreparationService,
        memorial_service_factory: Callable[[QObject], MemorialService],
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        projection_aspect_ratio_provider: Callable[[], object] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        from ...bootstrap.startup_timeline import startup_timeline

        startup = startup_timeline()
        startup.mark("meetings_constructor_started")
        self._loading_placeholder = DeferredLoadingPlaceholder(
            self.tr("Loading…"),
            self,
        )
        self._lang_mgr  = lang_manager
        self._notifications = notifications
        self._playback_protection = playback_protection
        self._monday    = current_monday()
        self._saved_snapshots: dict[str, dict[str, MeetingTreeSnapshot]] = {}
        self._details: dict[str, StudyDetailView | _MemorialDetailView] = {}
        self._destination_sessions: set[MeetingDestinationSession] = set()
        self._clear_details_pending = False
        self._watched_folder: str = ""
        self._meeting_tree_store = meeting_tree_store
        self._profile_media_store = profile_media_store
        self._meeting_thumbnail_store = meeting_thumbnail_store
        self._watched_folder_file_store = watched_folder_file_store
        self._jwpub_import_thread_factory = jwpub_import_thread_factory
        self._document_conversion_service = document_conversion_service
        self._watched_folder_watcher_factory = watched_folder_watcher_factory
        self._profile_paths = profile_paths
        self._runtime_paths = runtime_paths
        self._cache_manager = cache_manager
        self._media_tree_runtime = media_tree_runtime
        self._jw_catalog_service_factory = jw_catalog_service_factory
        self._jw_catalog_thumbnail_session_factory = (
            jw_catalog_thumbnail_session_factory
        )
        self._jw_songs_store = jw_songs_store
        self._media_settings = media_settings
        self._meeting_linked_folder_sync = meeting_linked_folder_sync
        self._media_info_queue_factory = media_info_queue_factory
        self._projection_aspect_ratio_provider = projection_aspect_ratio_provider

        self._service = publication_service
        self._preparation = preparation_service
        self._tree_controller_factory = MeetingTreeControllerFactory(
            MeetingTreeControllerDependencies(
                service=self._service,
                store=self._meeting_tree_store,
                profile_media_store=self._profile_media_store,
                meeting_thumbnail_store=self._meeting_thumbnail_store,
                watched_folder_file_store=self._watched_folder_file_store,
                jwpub_import_thread_factory=self._jwpub_import_thread_factory,
                document_conversion_service=self._document_conversion_service,
                profile_paths=self._profile_paths,
                runtime_paths=self._runtime_paths,
                cache_manager=self._cache_manager,
                media_tree_runtime=self._media_tree_runtime,
                linked_folder_sync=self._meeting_linked_folder_sync,
                media_info_queue_factory=self._media_info_queue_factory,
                projection_aspect_ratio_provider=(
                    self._projection_aspect_ratio_provider
                ),
            )
        )
        startup.mark("meetings_controller_factory_ready")
        self._set_lang_from_mgr()

        self._preparation.tree_changed.connect(self._on_tree_changed)
        self._preparation.state_changed.connect(self._on_preparation_state_changed)
        self._preparation.progress.connect(self._on_preparation_progress)
        self._preparation.error.connect(self._on_preparation_error)

        # ── Memorial service ───────────────────────────────────────────────────
        self._memorial_svc = memorial_service_factory(self)
        self._set_memorial_lang_from_mgr()
        self._memorial_svc.memorial_ready.connect(self._on_memorial_ready)
        self._memorial_svc.memorial_status.connect(self._on_memorial_status)
        self._memorial_svc.memorial_progress.connect(self._on_memorial_progress)
        startup.mark("meetings_memorial_service_ready")

        if lang_manager and hasattr(lang_manager, "language_changed"):
            lang_manager.language_changed.connect(self._on_lang_changed)

        # Recarrega quando o idioma de mídia JW muda (independente da UI)
        if lang_manager and hasattr(lang_manager, "jw_lang_service"):
            lang_manager.jw_lang_service.media_language_changed.connect(
                self._on_media_lang_changed
            )

        self._auto_download_timer = QTimer(self)
        self._auto_download_timer.setSingleShot(True)
        self._auto_download_timer.timeout.connect(self.sync_automatic_downloads)

        self._folder_watcher = self._watched_folder_watcher_factory(self)
        self._folder_watcher.changed.connect(self._on_folder_changed)
        self._folder_watcher.subfolder_changed.connect(self._on_folder_changed)
        self._wf_debounce = QTimer(self)
        self._wf_debounce.setSingleShot(True)
        self._wf_debounce.setInterval(600)
        self._wf_debounce.timeout.connect(self._do_folder_refresh)
        startup.mark("meetings_watchers_ready")
        initial_monday = self._monday
        initial_context = self._current_media_context()
        self._snapshot_preparation = AsyncLoadHandle(
            lambda: (
                initial_monday,
                initial_context.api_code,
                initial_context.is_sign_language,
                self._meeting_tree_store.snapshots_for_week(
                    initial_monday,
                    initial_context.api_code,
                    initial_context.is_sign_language,
                ),
            ),
            self._apply_initial_snapshots,
            self,
            thread_name_prefix="meeting-startup",
        )
        self.preparation_handle = IncrementalLoadHandle(
            (
                self._build_root,
                self._build_navbar,
                self._build_overview,
                self._install_overview,
                lambda: self._overview.install_content(),
                lambda: self._overview.build_mwb_card(),
                lambda: self._overview.populate_mwb_card(),
                lambda: self._overview.build_wt_card(),
                lambda: self._overview.populate_wt_card(),
                lambda: self._overview.build_memorial_card(),
                lambda: self._overview.populate_memorial_card(),
                self._finish_initial_shell,
            ),
            self,
        )
        startup.mark("meetings_preparation_ready")

    def _finish_initial_shell(self) -> None:
        self._overview.finish_build()
        self._navbar.update_week(self._monday)
        self._loading_placeholder.finish()
        from ...bootstrap.startup_timeline import startup_timeline

        startup_timeline().mark("meetings_shell_built")
        self._auto_download_timer.start(3000)
        self._snapshot_preparation.start()
        self.update()

    def _apply_initial_snapshots(
        self,
        result: tuple[date, str, bool, dict[str, MeetingTreeSnapshot]],
    ) -> None:
        monday, language, is_sign_language, snapshots = result
        context = self._current_media_context()
        if (
            monday != self._monday
            or language != context.api_code
            or bool(is_sign_language) != context.is_sign_language
        ):
            return
        cache_key = self._saved_snapshot_cache_key_for(
            monday,
            language,
            is_sign_language,
        )
        self._saved_snapshots[cache_key] = snapshots
        db_label = snapshots.get("mwb").overview.title if snapshots.get("mwb") else ""
        self._navbar.update_week(monday, db_label)
        self._refresh_overview_cards(monday)
        self._ensure_week(monday)
        if self._memorial_svc.is_memorial_week(monday):
            self._memorial_svc.load()

    def _current_media_context(self) -> JWMediaLanguageContext:
        return jw_media_language_context(self._lang_mgr, default_api_code="T")

    def _preparation_key(self, monday: date) -> MeetingPreparationKey:
        return MeetingPreparationKey.from_context(
            monday,
            self._current_media_context(),
        )

    def _ensure_week(
        self,
        monday: date,
        *,
        force: bool = False,
        download_media: bool = False,
        priority: MeetingPreparationPriority = MeetingPreparationPriority.INTERACTIVE,
    ) -> MeetingPreparationState:
        return self._preparation.ensure_week(
            MeetingPreparationRequest(
                key=self._preparation_key(monday),
                force_refresh=force,
                download_media=download_media,
                priority=priority,
            )
        )

    def sync_automatic_downloads(self) -> None:
        if not self._media_settings.meetings_auto_download():
            self._preparation.cancel_automatic_downloads()
            return
        monday = current_monday()
        for target in (monday, monday + timedelta(weeks=1)):
            self._ensure_week(
                target,
                download_media=True,
                priority=MeetingPreparationPriority.BACKGROUND,
            )

    @Slot(bool)
    def set_automatic_download_enabled(self, enabled: bool) -> None:
        if enabled:
            self.sync_automatic_downloads()
        else:
            self._preparation.cancel_automatic_downloads()

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
        self._build_root()
        self._build_navbar()
        self._build_overview()
        self._install_overview()

    def _build_root(self) -> None:
        self._root_layout = QVBoxLayout(self)
        self._root_layout.setContentsMargins(0, 0, 0, 0)
        self._root_layout.setSpacing(0)

    def _build_navbar(self) -> None:
        self._navbar = WeekNavBar()
        self._navbar.prev_week.connect(self._go_prev)
        self._navbar.next_week.connect(self._go_next)
        self._navbar.home_requested.connect(self._go_home)
        self._navbar.pick_requested.connect(self._show_picker)
        self._root_layout.addWidget(self._navbar)

    def _build_overview(self) -> None:
        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background:{PALETTE.bg0};")
        self._overview = Overview(defer_cards=True)
        self._overview.open_mwb.connect(lambda: self._open_detail("mwb"))
        self._overview.open_wt.connect(lambda: self._open_detail("wt"))
        self._overview.open_memorial.connect(self._open_memorial_detail)

    def _install_overview(self) -> None:
        self._stack.addWidget(self._overview)
        self._root_layout.addWidget(self._stack, stretch=1)

    def apply_theme(self) -> None:
        self.setStyleSheet(f"background:{PALETTE.bg0};")
        if not hasattr(self, "_stack"):
            self._loading_placeholder.refresh_theme()
        if hasattr(self, "_stack"):
            self._stack.setStyleSheet(f"background:{PALETTE.bg0};")
        if hasattr(self, "_navbar"):
            self._navbar.apply_theme()
        if hasattr(self, "_overview"):
            self._overview.apply_theme()
            self._refresh_overview_cards(self._monday)
        for detail in getattr(self, "_details", {}).values():
            if hasattr(detail, "apply_theme"):
                detail.apply_theme()

    def changeEvent(self, event) -> None:
        if (
            event.type() == QEvent.Type.LanguageChange
            and not hasattr(self, "_stack")
        ):
            self._loading_placeholder.set_text(self.tr("Loading…"))
        super().changeEvent(event)

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
        popup = WeekPicker(
            self._monday,
            cache=self._preparation.loaded_week_data(self._current_media_context()),
        )
        popup.week_selected.connect(self._on_week_picked)
        popup.show_near(self._navbar)

    @Slot(object)
    def _on_week_picked(self, monday: date):
        self._go_overview()
        self._navigate_to(monday)

    def _navigate_to(self, monday: date, reload: bool = False):
        self._monday = monday
        self._refresh_saved_snapshots(monday)
        snapshots = self._saved_snapshots_for(monday)
        db_label = snapshots.get("mwb").overview.title if snapshots.get("mwb") else ""
        self._navbar.update_week(monday, db_label)
        self._refresh_overview_cards(monday)
        self._ensure_week(monday, force=reload)
        # Carrega mídias do Memorial se estamos na semana correta
        # (MemorialService decide internamente se está na janela de 7 dias)
        if self._memorial_svc.is_memorial_week(monday):
            self._memorial_svc.load(force=reload)

    def _refresh_overview_cards(self, monday: date):
        snapshots = self._saved_snapshots_for(monday)
        mwb_snapshot = snapshots.get("mwb")
        wt_snapshot = snapshots.get("wt")
        if mwb_snapshot is not None:
            self._overview.mwb_card.set_saved(mwb_snapshot)
        else:
            self._apply_unavailable_card_state(self._overview.mwb_card, monday, "mwb")
        if wt_snapshot is not None:
            self._overview.wt_card.set_saved(wt_snapshot)
        else:
            self._apply_unavailable_card_state(self._overview.wt_card, monday, "wt")

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

    def _apply_unavailable_card_state(self, card, monday: date, pub_type: str) -> None:
        state = self._preparation.state(self._preparation_key(monday), pub_type)
        if state.source_status == "empty":
            card.set_empty()
        elif state.source_status == "not_found" or state.error == "NOT_FOUND":
            card.set_not_found()
        elif state.error:
            card.set_error()
        else:
            card.set_loading()

    # ── Detail ────────────────────────────────────────────────────────────────

    def _open_detail(self, pub_type: str):
        key    = self._monday.isoformat()
        snapshot = self._saved_snapshot_for(pub_type, self._monday)
        if snapshot is None:
            state = self._preparation.state(
                self._preparation_key(self._monday),
                pub_type,
            )
            if not state.error:
                return
            card = self._overview.mwb_card if pub_type == "mwb" else self._overview.wt_card
            card.set_loading()
            self._ensure_week(self._monday, force=True)
            return
        wd = self._preparation.week_data(self._preparation_key(self._monday))
        if wd is None:
            wd = WeekData(monday=self._monday)
        detail_key = f"{pub_type}:{key}"
        if detail_key not in self._details:
            self._show_study_detail(pub_type, wd, snapshot)

        self._navbar.setVisible(False)
        self._stack.setCurrentWidget(self._details[detail_key])

    def _show_study_detail(
        self,
        pub_type: str,
        wd: WeekData,
        saved_snapshot: MeetingTreeSnapshot,
    ) -> None:
        detail_key = f"{pub_type}:{wd.monday.isoformat()}"
        d = StudyDetailView(
            pub_type,
            wd,
            notifications=self._notifications,
            playback_protection=self._playback_protection,
            language_context=self._current_media_context(),
            controller_factory=self._tree_controller_factory,
            document_conversion_service=self._document_conversion_service,
            jw_catalog_service_factory=self._jw_catalog_service_factory,
            jw_catalog_thumbnail_session_factory=(
                self._jw_catalog_thumbnail_session_factory
            ),
            jw_songs_store=self._jw_songs_store,
            meeting_tree_saved_handler=self._on_detail_tree_saved,
            saved_snapshot=saved_snapshot,
            watched_folder=self._watched_folder,
        )
        d.back_requested.connect(self._on_detail_back)
        d.play_requested.connect(self.project_media)
        self._stack.addWidget(d)
        self._details[detail_key] = d

    def _on_detail_back(self):
        self._go_overview()

    @Slot(str)
    def _on_detail_tree_saved(self, tree_key: str) -> None:
        key = parse_meeting_tree_key(tree_key)
        if key is None or key.pub_type not in {"mwb", "wt"}:
            return
        self._saved_snapshots.pop(
            self._saved_snapshot_cache_key_for(
                key.monday,
                key.language,
                key.is_sign_language,
            ),
            None,
        )
        if key.monday == self._monday:
            self._refresh_overview_cards(self._monday)

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
                notifications=self._notifications,
                playback_protection=self._playback_protection,
                language_context=self._current_media_context(),
                controller_factory=self._tree_controller_factory,
                document_conversion_service=self._document_conversion_service,
                jw_catalog_service_factory=self._jw_catalog_service_factory,
                jw_catalog_thumbnail_session_factory=(
                    self._jw_catalog_thumbnail_session_factory
                ),
                jw_songs_store=self._jw_songs_store,
            )
            d.back_requested.connect(self._on_detail_back)
            d.play_requested.connect(self.project_media)
            self._stack.addWidget(d)
            self._details[detail_key] = d

        self._navbar.setVisible(False)
        self._stack.setCurrentWidget(self._details[detail_key])

    # ── Preparation callbacks ─────────────────────────────────────────────────

    @Slot(object, str, object)
    def _on_tree_changed(
        self,
        key: MeetingPreparationKey,
        pub_type: str,
        snapshot: MeetingTreeSnapshot,
    ) -> None:
        cache_key = self._saved_snapshot_cache_key_for(
            key.monday,
            key.language_code,
            key.is_sign_language,
        )
        self._saved_snapshots[cache_key] = self._meeting_tree_store.snapshots_for_week(
            key.monday,
            key.language_code,
            key.is_sign_language,
        )
        monday_text = key.monday.isoformat()
        self.destinationTargetsChanged.emit(monday_text)
        detail = self._details.get(f"{pub_type}:{monday_text}")
        if isinstance(detail, StudyDetailView):
            detail.update_snapshot(snapshot)
        for session in list(self._destination_sessions):
            if (
                not (
                    isinstance(detail, StudyDetailView)
                    and session.controller is detail.controller
                )
            ):
                session.update_snapshot(snapshot)
        if key == self._preparation_key(self._monday):
            if pub_type == "mwb":
                self._navbar.update_week(self._monday, snapshot.overview.title)
            self._refresh_overview_cards(self._monday)

    @Slot(object, str, object)
    def _on_preparation_state_changed(
        self,
        key: MeetingPreparationKey,
        _pub_type: str,
        _state: MeetingPreparationState,
    ) -> None:
        self.destinationTargetsChanged.emit(key.monday.isoformat())
        if key == self._preparation_key(self._monday):
            self._refresh_overview_cards(self._monday)

    @Slot(object, str, int)
    def _on_preparation_progress(
        self,
        key: MeetingPreparationKey,
        pub_type: str,
        percent: int,
    ) -> None:
        if key != self._preparation_key(self._monday):
            return
        if self._saved_snapshot_for(pub_type, key.monday) is not None:
            return
        card = self._overview.mwb_card if pub_type == "mwb" else self._overview.wt_card
        card.set_progress(percent)

    @Slot(object, str, str)
    def _on_preparation_error(
        self,
        key: MeetingPreparationKey,
        pub_type: str,
        _message: str,
    ) -> None:
        self.destinationTargetsChanged.emit(key.monday.isoformat())
        if key == self._preparation_key(self._monday):
            self._refresh_overview_cards(self._monday)

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
        self._preparation.cancel_other_language_contexts(
            self._current_media_context()
        )
        self._set_lang_from_mgr()
        self._set_memorial_lang_from_mgr()
        self._saved_snapshots.clear()
        self._clear_details()
        self._overview.mwb_card.set_loading()
        self._overview.wt_card.set_loading()
        self._overview.memorial_card.set_loading()
        self._navigate_to(self._monday, reload=True)
        self.sync_automatic_downloads()

    @Slot(str)
    def _on_media_lang_changed(self, _code: str):
        """
        Idioma de mídia JW mudou → recarrega reuniões com o novo código.
        Usa o contexto JW centralizado em vez do fallback da interface.
        """
        self._preparation.cancel_other_language_contexts(
            self._current_media_context()
        )
        self._set_lang_from_mgr()
        self._set_memorial_lang_from_mgr()
        self._saved_snapshots.clear()
        self._clear_details()
        self._overview.mwb_card.set_loading()
        self._overview.wt_card.set_loading()
        self._overview.memorial_card.set_loading()
        self._navigate_to(self._monday, reload=True)
        self.sync_automatic_downloads()

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

    def request_destination_week(self, monday: date, *, force: bool = False) -> None:
        """Load destination availability without changing the meetings page."""

        if not is_selectable_meeting_week(monday):
            return
        self._refresh_saved_snapshots(monday)
        self.destinationTargetsChanged.emit(monday.isoformat())
        self._ensure_week(monday, force=force)

    def destination_targets(self, monday: date) -> list[dict[str, object]]:
        if not is_selectable_meeting_week(monday):
            return []
        snapshots = self._saved_snapshots_for(monday)
        week_data = self._preparation.week_data(self._preparation_key(monday))
        return [
            self._destination_target(
                pub_type,
                monday,
                week_data,
                snapshots.get(pub_type),
            )
            for pub_type in ("mwb", "wt")
        ]

    def open_destination_session(
        self,
        monday: date,
        pub_type: str,
    ) -> MeetingDestinationSession | None:
        """Open a stable controller session for one external insertion."""

        if pub_type not in {"mwb", "wt"} or not is_selectable_meeting_week(monday):
            return None
        key = monday.isoformat()
        detail = self._details.get(f"{pub_type}:{key}")
        if isinstance(detail, StudyDetailView):
            session = MeetingDestinationSession(
                detail.controller,
                owned_controller=False,
                parent=self,
            )
            return self._track_destination_session(session)

        snapshots = self._saved_snapshots_for(monday)
        snapshot = snapshots.get(pub_type)
        if snapshot is None:
            return None

        context = self._current_media_context()
        controller = self._tree_controller_factory.create(
            pub_type,
            context,
            parent=self,
        )
        controller.storageSaved.connect(self._on_detail_tree_saved)
        controller.storageSaveFailed.connect(self._on_destination_storage_failed)
        controller.set_sync_root(self._watched_folder)
        controller.load_saved_tree(snapshot)
        if self._watched_folder:
            controller.inject_linked_folder_media(self._watched_folder)

        session = MeetingDestinationSession(
            controller,
            owned_controller=True,
            parent=self,
        )
        return self._track_destination_session(session)

    def _track_destination_session(
        self,
        session: MeetingDestinationSession,
    ) -> MeetingDestinationSession:
        self._destination_sessions.add(session)
        session.closed.connect(self._destination_sessions.discard)
        return session

    def _destination_target(
        self,
        pub_type: str,
        monday: date,
        week_data: WeekData | None,
        snapshot: MeetingTreeSnapshot | None,
    ) -> dict[str, object]:
        state = self._preparation.state(self._preparation_key(monday), pub_type)
        available = snapshot is not None
        if available:
            display_status = "ready"
        elif state.source_status == "empty":
            display_status = "unavailable"
        elif state.error:
            display_status = "error"
        else:
            display_status = "loading"

        if pub_type == "mwb":
            title = _tr_ctx("_PubCard", "LIFE & MINISTRY")
            detail = (
                str(getattr(week_data, "mwb_date_label", "") or "")
                or str(getattr(week_data, "mwb_week_title", "") or "")
            )
            color = str(PALETTE.accent)
        else:
            title = _tr_ctx("_PubCard", "WATCHTOWER STUDY")
            detail = str(getattr(week_data, "wt_study_title", "") or "")
            color = str(MEETING_PURPLE)

        if snapshot is not None:
            detail = snapshot.overview.title or detail
        if not detail:
            detail = week_label(monday)
        if display_status == "unavailable" and snapshot is None:
            detail = _tr_ctx("_PubCard", "No meeting this week")
        if display_status == "error" and snapshot is None:
            detail = _tr_ctx("MediaDestinationDialog", "Unavailable — try again")

        return {
            "pub_type": pub_type,
            "title": title,
            "subtitle": detail,
            "status": display_status,
            "available": available,
            "color": color,
        }

    def _on_destination_storage_failed(self, tree_key: str, error: str) -> None:
        _notify_meeting_tree_save_failed(self._notifications, tree_key, error)

    def _saved_snapshot_cache_key(self, monday: date) -> str:
        context = self._current_media_context()
        return self._saved_snapshot_cache_key_for(
            monday,
            context.api_code,
            context.is_sign_language,
        )

    def _saved_snapshot_cache_key_for(
        self,
        monday: date,
        language: str,
        is_sign_language: bool = False,
    ) -> str:
        variant = "sign" if is_sign_language else "spoken"
        return f"{monday.isoformat()}:{language}:{variant}"

    def _refresh_saved_snapshots(
        self,
        monday: date,
    ) -> dict[str, MeetingTreeSnapshot]:
        context = self._current_media_context()
        snapshots = self._meeting_tree_store.snapshots_for_week(
            monday,
            context.api_code,
            context.is_sign_language,
        )
        cache_key = self._saved_snapshot_cache_key_for(
            monday,
            context.api_code,
            context.is_sign_language,
        )
        self._saved_snapshots[cache_key] = snapshots
        return snapshots

    def _saved_snapshots_for(self, monday: date) -> dict[str, MeetingTreeSnapshot]:
        cache_key = self._saved_snapshot_cache_key(monday)
        if cache_key not in self._saved_snapshots:
            return self._refresh_saved_snapshots(monday)
        return self._saved_snapshots[cache_key]

    def _saved_snapshot_for(
        self,
        pub_type: str,
        monday: date,
    ) -> MeetingTreeSnapshot | None:
        return self._saved_snapshots_for(monday).get(pub_type)

    def cleanup(self) -> None:
        """Para serviços com QThread antes da janela principal ser destruída."""
        self.preparation_handle.cancel()
        self._snapshot_preparation.cancel()
        timer = getattr(self, "_auto_download_timer", None)
        if timer:
            timer.stop()
        for session in list(self._destination_sessions):
            session.close()
        self._destination_sessions.clear()
        self._clear_details()
        try:
            self._preparation.shutdown()
        except Exception:  # noqa: BLE001 - background service shutdown boundary
            log_ignored_exception(__name__, "Could not shut down meeting preparation")
        try:
            self._memorial_svc.shutdown(delete_when_stopped=True)
        except Exception:  # noqa: BLE001 - background service shutdown boundary
            log_ignored_exception(__name__, "Could not shut down memorial service")
