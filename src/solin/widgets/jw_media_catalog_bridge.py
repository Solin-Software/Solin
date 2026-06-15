"""
jw_media_catalog_bridge.py — QML bridge + model for JW Media Catalog browsing.

Architecture
────────────
- JWMediaCatalogModel(QAbstractListModel)
    Grid model exposing video items fetched from the JW.org media catalog.
    Each row represents a single video with thumbnail, title, duration, and
    download metadata. Thumbnails are supplied by an injected owned session
    and the model is updated in-place via ``dataChanged``.

- JWMediaCatalogBridge(QObject)
    Main bridge exposed to QML as ``catalogBridge``.  Coordinates fetching the
    full JW video catalog from ``JWMediaCatalogService``, page-based
    navigation, client-side title search, item selection, and placement logic
    for inserting a video into the current playlist.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Optional, TYPE_CHECKING

from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Property,
    Signal,
    Slot,
)

from ..core.meetings.colors import accent_from_hue

if TYPE_CHECKING:
    from ..core.jw.catalog import JWMediaCatalogService
    from ..core.jw.thumbnail_fetch import JWCatalogThumbnailSessionFactory

log = logging.getLogger(__name__)


# ── Duration formatting ───────────────────────────────────────────────────────


def _format_duration(seconds: float) -> str:
    """Format *seconds* into a human-readable ``m:ss`` or ``h:mm:ss`` string."""
    if seconds <= 0:
        return ""
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _file_url(path: str) -> str:
    """Convert a local file-system *path* to a ``file:///`` URL string."""
    if not path:
        return ""
    return QUrl.fromLocalFile(path).toString()


def build_jw_media_placement_options(
    pl: dict[str, Any] | None,
    *,
    translate_context: str = "JWMediaCatalogBridge",
) -> list[dict[str, Any]]:
    """Compute where a JW media item can be placed within a playlist/tree."""
    if not pl:
        return []

    items = pl.get("items", [])
    sections = [
        s for s in pl.get("sections", [])
        if not s.get("parent_id")
    ]
    total = len(items)
    num_sections = len(sections)
    has_many_items = total > 13
    has_multiple_sections = num_sections >= 2
    should_prompt = has_many_items or has_multiple_sections

    _tr = lambda text: QCoreApplication.translate(translate_context, text)  # noqa: E731

    if not should_prompt:
        return []

    options: list[dict[str, Any]] = [
        {"id": "top", "label": _tr("Top of playlist"), "type": "position"},
        {"id": "bottom", "label": _tr("End of playlist"), "type": "position"},
    ]
    for sec in sections:
        section_id = sec.get("id")
        if not section_id:
            continue
        hue = sec.get("color_hue", 215)
        options.append({
            "id": f"section:{section_id}",
            "label": sec.get("name", "Section"),
            "type": "section",
            "color": accent_from_hue(hue),
        })
    return options


# ── Catalog list model ────────────────────────────────────────────────────────


class JWMediaCatalogModel(QAbstractListModel):
    """Grid model exposing JW media catalog items to QML.

    Items are plain dicts with keys matching ``JWMediaItem.to_dict()``.
    The model adds a computed ``thumbnailSource`` field (``file:///`` URL) so
    QML ``Image`` elements can bind directly.
    """

    # Roles
    TitleRole            = Qt.ItemDataRole.UserRole + 1
    ThumbnailSourceRole  = Qt.ItemDataRole.UserRole + 2
    DurationTextRole     = Qt.ItemDataRole.UserRole + 3
    ItemIdRole           = Qt.ItemDataRole.UserRole + 4
    DownloadUrlRole      = Qt.ItemDataRole.UserRole + 5
    LabelRole            = Qt.ItemDataRole.UserRole + 6
    PubRole              = Qt.ItemDataRole.UserRole + 7
    TrackRole            = Qt.ItemDataRole.UserRole + 8
    DocIdRole            = Qt.ItemDataRole.UserRole + 9
    LanguageRole         = Qt.ItemDataRole.UserRole + 10
    DurationSecondsRole  = Qt.ItemDataRole.UserRole + 11
    DurationTicksRole    = Qt.ItemDataRole.UserRole + 12

    _ROLE_NAMES: dict[int, bytes] = {
        TitleRole:           b"title",
        ThumbnailSourceRole: b"thumbnailSource",
        DurationTextRole:    b"durationText",
        ItemIdRole:          b"itemId",
        DownloadUrlRole:     b"downloadUrl",
        LabelRole:           b"label",
        PubRole:             b"pub",
        TrackRole:           b"track",
        DocIdRole:           b"docId",
        LanguageRole:        b"language",
        DurationSecondsRole: b"durationSeconds",
        DurationTicksRole:   b"durationTicks",
    }

    _ROLE_KEY: dict[int, str] = {
        TitleRole:           "title",
        ThumbnailSourceRole: "_thumb_source",
        DurationTextRole:    "_duration_text",
        ItemIdRole:          "id",
        DownloadUrlRole:     "download_url",
        LabelRole:           "label",
        PubRole:             "pub",
        TrackRole:           "track",
        DocIdRole:           "docid",
        LanguageRole:        "language",
        DurationSecondsRole: "duration_seconds",
        DurationTicksRole:   "duration_ticks",
    }
    _SIGNATURE_KEYS: tuple[str, ...] = tuple(_ROLE_KEY.values())

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, Any]] = []

    # ── QAbstractListModel overrides ──────────────────────────────────────

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        """Return the number of items in the model."""
        parent = parent or QModelIndex()
        return len(self._items)

    def roleNames(self) -> dict[int, bytes]:
        """Return role-name mapping for QML property binding."""
        return self._ROLE_NAMES

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Return data for *role* at *index*."""
        if not index.isValid() or index.row() >= len(self._items):
            return None
        key = self._ROLE_KEY.get(role)
        if key:
            return self._items[index.row()].get(key)
        return None

    # ── Mutation helpers ──────────────────────────────────────────────────

    def set_items(self, items: list[dict[str, Any]]) -> None:
        """Replace all items with a full reset."""
        self.beginResetModel()
        self._items = [self._enrich(item) for item in items]
        self.endResetModel()

    def set_items_if_changed(self, items: list[dict[str, Any]]) -> bool:
        """Replace items only when visible role data actually changed."""
        enriched = [self._enrich(item) for item in items]
        if self._items_match(enriched):
            return False
        self.beginResetModel()
        self._items = enriched
        self.endResetModel()
        return True

    def append_items(self, items: list[dict[str, Any]]) -> None:
        """Append *items* to the end of the model."""
        if not items:
            return
        first = len(self._items)
        last = first + len(items) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        self._items.extend(self._enrich(item) for item in items)
        self.endInsertRows()

    def update_thumbnail(self, item_id: str, local_path: str) -> None:
        """Update thumbnail fields for a single item and emit ``dataChanged``."""
        for i, item in enumerate(self._items):
            if item.get("id") == item_id:
                item["thumbnail_path"] = local_path
                item["_thumb_source"] = _file_url(local_path) if local_path else ""
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [self.ThumbnailSourceRole])
                return

    def item_at(self, index: int) -> dict[str, Any] | None:
        """Return the raw item dict at *index*, or ``None``."""
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def clear(self) -> None:
        """Remove all items."""
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()

    # ── Internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _enrich(item: dict[str, Any]) -> dict[str, Any]:
        """Add computed fields (``_thumb_source``, ``_duration_text``)."""
        enriched = dict(item)
        thumb_path = enriched.get("thumbnail_path", "")
        enriched["_thumb_source"] = _file_url(thumb_path) if thumb_path else ""
        enriched["_duration_text"] = _format_duration(
            float(enriched.get("duration_seconds", 0) or 0)
        )
        return enriched

    def _items_match(self, items: list[dict[str, Any]]) -> bool:
        if len(items) != len(self._items):
            return False
        return all(
            self._item_signature(left) == self._item_signature(right)
            for left, right in zip(self._items, items, strict=False)
        )

    @classmethod
    def _item_signature(cls, item: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(item.get(key) for key in cls._SIGNATURE_KEYS)


_PROGRESS_UI_UPDATE_INTERVAL_MS = 160


# ── Main bridge ──────────────────────────────────────────────────────────────


class JWMediaCatalogBridge(QObject):
    """QML ↔ Python bridge for the JW media catalog browsing modal.

    Exposed to QML as a context property (``catalogBridge``).  Coordinates
    category fetches, local title search, paginated grid display, thumbnail
    background downloads, item selection, and placement logic.
    """

    # ── Notify signals ────────────────────────────────────────────────────

    modelChanged           = Signal()
    isLoadingChanged       = Signal()
    hasMoreChanged         = Signal()
    errorMessageChanged    = Signal()
    currentCategoryChanged = Signal()
    searchQueryChanged     = Signal()
    resultCountChanged     = Signal()
    pageChanged            = Signal()
    pageCountChanged       = Signal()
    loadProgressChanged    = Signal()
    includeAudioDescriptionChanged = Signal()
    catalogPageRefreshAboutToStart = Signal()
    catalogPageRefreshFinished = Signal()
    showPlacementChanged   = Signal()
    placementOptionsChanged = Signal()
    pendingItemChanged     = Signal()

    # ── Action signals ────────────────────────────────────────────────────

    itemAddedSuccessfully = Signal(str)            # toast message
    modalShouldClose      = Signal()               # hide modal
    jwMediaConfirmed      = Signal(dict, str, int)  # item_data, list_id, index

    def __init__(
        self,
        catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
        thumbnail_session_factory: JWCatalogThumbnailSessionFactory,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)

        # Catalog service (async fetch backend)
        self._catalog_service = catalog_service_factory(self)
        self._catalog_service.videos_progress.connect(self._on_videos_progress)
        self._catalog_service.videos_ready.connect(self._on_videos_ready)
        self._catalog_service.fetch_failed.connect(self._on_fetch_failed)

        # List model
        self._model = JWMediaCatalogModel(self)

        # Item data
        self._all_items: list[dict[str, Any]] = []
        self._filtered_items: list[dict[str, Any]] = []

        # Pagination
        self._page_size: int = 24
        self._current_page: int = 1

        # UI state
        self._search_query: str = ""
        self._current_category: str = ""
        self._is_loading: bool = False
        self._error_message: str = ""
        self._load_completed: int = 0
        self._load_total: int = 0
        self._catalog_complete: bool = False
        self._include_audio_description: bool = False
        self._active_catalog_request_id: str = ""
        self._pending_progress: tuple[str, list[dict[str, Any]], int, int] | None = None
        self._progress_apply_timer = QTimer(self)
        self._progress_apply_timer.setSingleShot(True)
        self._progress_apply_timer.timeout.connect(self._flush_pending_progress)

        # Pending item / placement
        self._pending_item: dict[str, Any] | None = None
        self._placement_options: list[dict[str, Any]] = []
        self._show_placement: bool = False

        # External references
        self._pl: dict[str, Any] | None = None
        self._lang_code: str = "E"

        self._thumbnail_session = thumbnail_session_factory.create(parent=self)
        self._thumbnail_session.ready.connect(self._on_thumb_ready)

    # ── QML Properties ────────────────────────────────────────────────────

    @Property(QObject, notify=modelChanged)
    def model(self) -> QObject:
        """The catalog list model for QML GridView."""
        return self._model

    @Property(bool, notify=isLoadingChanged)
    def isLoading(self) -> bool:
        """True while a network fetch is in progress."""
        return self._is_loading

    @Property(bool, notify=hasMoreChanged)
    def hasMore(self) -> bool:
        """True if additional items can be loaded with ``loadMore()``."""
        return self._current_page < self._page_count()

    @Property(str, notify=errorMessageChanged)
    def errorMessage(self) -> str:
        """Human-readable error from the last failed fetch, or empty."""
        return self._error_message

    @Property(str, notify=currentCategoryChanged)
    def currentCategory(self) -> str:
        """The ``id`` of the currently selected category."""
        return self._current_category

    @Property(str, notify=searchQueryChanged)
    def searchQuery(self) -> str:
        """The current client-side title filter string."""
        return self._search_query

    @Property(int, notify=resultCountChanged)
    def resultCount(self) -> int:
        """Number of items matching the current filter."""
        return len(self._filtered_items)

    @Property(int, notify=pageChanged)
    def currentPage(self) -> int:
        """Current 1-based catalog page."""
        return self._current_page

    @Property(int, notify=pageCountChanged)
    def pageCount(self) -> int:
        """Total number of pages in the filtered catalog."""
        return self._page_count()

    @Property(bool, notify=loadProgressChanged)
    def catalogComplete(self) -> bool:
        """True after all discovered categories have been processed."""
        return self._catalog_complete

    @Property(str, notify=loadProgressChanged)
    def loadStatusText(self) -> str:
        """Short loading status for the modal header."""
        if self._catalog_complete:
            return QCoreApplication.translate("JWMediaCatalogBridge", "Catalog loaded")
        if self._load_total > 0:
            return QCoreApplication.translate(
                "JWMediaCatalogBridge", "Loading {done}/{total}"
            ).format(done=self._load_completed, total=self._load_total)
        return QCoreApplication.translate("JWMediaCatalogBridge", "Loading")

    @Property(bool, notify=includeAudioDescriptionChanged)
    def includeAudioDescription(self) -> bool:
        """Whether audio-description videos are included in the catalog."""
        return self._include_audio_description

    @Property(bool, notify=showPlacementChanged)
    def showPlacement(self) -> bool:
        """True when the placement options popup should be visible."""
        return self._show_placement

    @Property("QVariant", notify=placementOptionsChanged)
    def placementOptions(self) -> list[dict[str, Any]]:
        """List of placement option dicts for the QML placement popup."""
        return self._placement_options

    @Property(str, notify=pendingItemChanged)
    def pendingItemTitle(self) -> str:
        """Title of the item currently awaiting placement."""
        if self._pending_item:
            return str(self._pending_item.get("title", ""))
        return ""

    @Property(str, notify=pendingItemChanged)
    def pendingItemThumb(self) -> str:
        """Thumbnail source of the item currently awaiting placement."""
        if self._pending_item:
            path = self._pending_item.get("thumbnail_path", "")
            return _file_url(path) if path else ""
        return ""

    # ── Slots (QML actions) ───────────────────────────────────────────────

    @Slot(str)
    def fetchCategory(self, category_id: str) -> None:
        """Compatibility slot.  The modal now loads the full catalog."""
        self.fetchAll()

    @Slot()
    def fetchAll(self) -> None:
        """Fetch the full JW.org video catalog."""
        self._current_category = "all"
        self.currentCategoryChanged.emit()

        self._search_query = ""
        self.searchQueryChanged.emit()

        self._error_message = ""
        self.errorMessageChanged.emit()

        self._set_loading(True)
        self._all_items.clear()
        self._filtered_items.clear()
        self._current_page = 1
        self._model.clear()
        self._catalog_complete = False
        self._progress_apply_timer.stop()
        self._pending_progress = None
        self._load_completed = 0
        self._load_total = 0
        self.resultCountChanged.emit()
        self.pageChanged.emit()
        self.pageCountChanged.emit()
        self.hasMoreChanged.emit()
        self.loadProgressChanged.emit()

        self._active_catalog_request_id = self._catalog_service.fetch_all_videos(
            self._lang_code,
            cache_thumbnails=False,
        )

    @Slot()
    def loadMore(self) -> None:
        """Compatibility slot: advance to the next page."""
        self.nextPage()

    @Slot()
    def nextPage(self) -> None:
        """Navigate to the next page."""
        self.setPage(self._current_page + 1)

    @Slot()
    def previousPage(self) -> None:
        """Navigate to the previous page."""
        self.setPage(self._current_page - 1)

    @Slot(int)
    def setPage(self, page: int) -> None:
        """Navigate to a 1-based page number."""
        target = max(1, min(int(page), max(1, self._page_count())))
        if target == self._current_page and self._model.rowCount() > 0:
            return
        self._current_page = target
        self.pageChanged.emit()
        self._update_page()

    @Slot(bool)
    def setIncludeAudioDescription(self, value: bool) -> None:
        """Toggle audio-description videos in the client-side filter."""
        if self._include_audio_description == value:
            return
        self._include_audio_description = value
        self.includeAudioDescriptionChanged.emit()
        self._apply_filter()
        self._update_page()

    @Slot(str)
    def setSearchQuery(self, query: str) -> None:
        """Filter displayed items by title (local, client-side)."""
        self._search_query = query
        self.searchQueryChanged.emit()

        self._apply_filter()
        self._update_page()

    @Slot(int)
    def selectItem(self, index: int) -> None:
        """Handle user click on a video card at *index*."""
        item = self._model.item_at(index)
        if not item:
            return

        self._pending_item = dict(item)
        self.pendingItemChanged.emit()

        options = self._compute_placement_options()
        if not options:
            # No placement needed — add directly to end of playlist.
            self._emit_confirmed("root", 2**31 - 1)
            return

        self._placement_options = options
        self.placementOptionsChanged.emit()
        self._show_placement = True
        self.showPlacementChanged.emit()

    @Slot(str)
    def confirmPlacement(self, placement_id: str) -> None:
        """Confirm the placement location chosen by the user."""
        if not self._pending_item:
            return

        if placement_id == "top":
            target_list_id = "root"
            target_index = 0
        elif placement_id == "bottom":
            target_list_id = "root"
            target_index = 2**31 - 1
        elif placement_id.startswith("section:"):
            target_list_id = placement_id
            target_index = 0
        else:
            target_list_id = "root"
            target_index = 2**31 - 1

        self._emit_confirmed(target_list_id, target_index)
        self.modalShouldClose.emit()

    @Slot()
    def cancelSelection(self) -> None:
        """Cancel the pending item selection."""
        self._pending_item = None
        self.pendingItemChanged.emit()

        self._show_placement = False
        self.showPlacementChanged.emit()

        self._placement_options = []
        self.placementOptionsChanged.emit()

    @Slot()
    def openModal(self) -> None:
        """Called when the modal is opened.  Fetch catalog if needed."""
        if not self._all_items and not self._is_loading:
            self.fetchAll()

    @Slot()
    def reset(self) -> None:
        """Full reset of all internal state."""
        self._catalog_service.cancel_all()
        self._progress_apply_timer.stop()
        self._pending_progress = None
        self._all_items.clear()
        self._filtered_items.clear()
        self._current_page = 1
        self._search_query = ""
        self._current_category = ""
        self._is_loading = False
        self._error_message = ""
        self._active_catalog_request_id = ""
        self._load_completed = 0
        self._load_total = 0
        self._catalog_complete = False
        self._include_audio_description = False
        self._pending_item = None
        self._placement_options = []
        self._show_placement = False
        self._thumbnail_session.reset()
        self._model.clear()

        self.searchQueryChanged.emit()
        self.currentCategoryChanged.emit()
        self.isLoadingChanged.emit()
        self.errorMessageChanged.emit()
        self.hasMoreChanged.emit()
        self.resultCountChanged.emit()
        self.pageChanged.emit()
        self.pageCountChanged.emit()
        self.loadProgressChanged.emit()
        self.includeAudioDescriptionChanged.emit()
        self.showPlacementChanged.emit()
        self.placementOptionsChanged.emit()
        self.pendingItemChanged.emit()

    def cleanup(self) -> None:
        """Stop background work owned by this bridge before teardown."""
        self._catalog_service.cancel_all(wait_ms=-1)
        self._active_catalog_request_id = ""
        self._progress_apply_timer.stop()
        self._pending_progress = None
        self._thumbnail_session.close()

    # ── Python-facing setters (called by host view) ───────────────────────

    def set_playlist_ref(self, pl: dict[str, Any] | None) -> None:
        """Set reference to the current playlist for placement logic."""
        self._pl = pl

    def set_language_code(self, code: str) -> None:
        """Set the JW API language code (e.g. ``'T'`` for Portuguese)."""
        if code and code != self._lang_code:
            self._lang_code = code
            self.reset()

    def _on_videos_progress(
        self,
        request_id: str,
        items: list[dict[str, Any]],
        completed: int,
        total: int,
    ) -> None:
        """Handle partial catalog progress from the service."""
        if request_id != self._active_catalog_request_id:
            return
        self._pending_progress = (request_id, items, completed, total)
        if not self._model.rowCount() or bool(total and completed >= total):
            self._flush_pending_progress()
            return
        if not self._progress_apply_timer.isActive():
            self._progress_apply_timer.start(_PROGRESS_UI_UPDATE_INTERVAL_MS)

    def _flush_pending_progress(self) -> None:
        pending = self._pending_progress
        self._pending_progress = None
        if pending is None:
            return
        request_id, items, completed, total = pending
        if request_id != self._active_catalog_request_id:
            return
        self._all_items = list(items)
        self._load_completed = completed
        self._load_total = total
        self._catalog_complete = bool(total and completed >= total)
        self._apply_filter(keep_page=True)
        self._update_page(preserve_viewport=True)
        self.loadProgressChanged.emit()
        if self._filtered_items:
            self._set_loading(True)

    # ── Service signal handlers ───────────────────────────────────────────

    def _on_videos_ready(
        self,
        request_id: str,
        items: list[dict[str, Any]],
        fetched_at: float,
        from_cache: bool,
    ) -> None:
        """Handle ``JWMediaCatalogService.videos_ready``."""
        if request_id != self._active_catalog_request_id:
            return
        self._progress_apply_timer.stop()
        self._pending_progress = None
        self._all_items = list(items)
        self._catalog_complete = True
        self._load_completed = self._load_total or self._load_completed
        self._apply_filter(keep_page=True)
        self._update_page(preserve_viewport=True)
        self.loadProgressChanged.emit()
        self._set_loading(False)

    def _on_fetch_failed(self, request_id: str, error: str) -> None:
        """Handle ``JWMediaCatalogService.fetch_failed``."""
        if self._active_catalog_request_id and request_id != self._active_catalog_request_id:
            return
        self._progress_apply_timer.stop()
        self._pending_progress = None
        log.warning("[CatalogBridge] Fetch failed (rid=%s): %s", request_id, error)
        self._error_message = error
        self.errorMessageChanged.emit()
        self._set_loading(False)

    # ── Filtering / pagination ────────────────────────────────────────────

    def _apply_filter(self, *, keep_page: bool = False) -> None:
        """Filter ``_all_items`` by ``_search_query`` → ``_filtered_items``."""
        query = self._search_query.strip().lower()
        source_items = [
            item for item in self._all_items
            if self._include_audio_description
            or not str(item.get("primary_category", "")).endswith("AD")
        ]
        if query:
            search_terms = [term for term in query.split() if term]
            self._filtered_items = [
                item for item in source_items
                if all(term in (item.get("title") or "").lower() for term in search_terms)
            ]
        else:
            self._filtered_items = list(source_items)
        if not keep_page:
            self._current_page = 1
            self.pageChanged.emit()
        elif self._current_page > self._page_count():
            self._current_page = max(1, self._page_count())
            self.pageChanged.emit()
        self.resultCountChanged.emit()
        self.pageCountChanged.emit()
        self.hasMoreChanged.emit()

    def _update_page(self, *, preserve_viewport: bool = False) -> None:
        """Rebuild model with the current page of ``_filtered_items``."""
        if preserve_viewport:
            self.catalogPageRefreshAboutToStart.emit()
        if not self._filtered_items:
            self._model.clear()
            self.hasMoreChanged.emit()
            if preserve_viewport:
                self.catalogPageRefreshFinished.emit()
            return
        start = (self._current_page - 1) * self._page_size
        end = start + self._page_size
        page_items = self._filtered_items[start:end]
        self._model.set_items_if_changed(page_items)
        self._queue_thumbnails(page_items)
        self.hasMoreChanged.emit()
        if preserve_viewport:
            self.catalogPageRefreshFinished.emit()

    def _page_count(self) -> int:
        if not self._filtered_items:
            return 1
        return max(1, (len(self._filtered_items) + self._page_size - 1) // self._page_size)

    # ── Thumbnail queue ───────────────────────────────────────────────────

    def _queue_thumbnails(self, items: list[dict[str, Any]]) -> None:
        """Enqueue thumbnail downloads for *items* missing a local path."""
        for item in items:
            item_id = item.get("id", "")
            thumb_url = item.get("thumbnail_url", "")
            thumb_path = item.get("thumbnail_path", "")
            if not thumb_url or thumb_path:
                continue
            self._thumbnail_session.enqueue(item_id, thumb_url)

    def _on_thumb_ready(self, item_id: str, local_path: str) -> None:
        """Handle a completed thumbnail download."""
        if local_path:
            # Update master list so re-filtering preserves the path.
            for item in self._all_items:
                if item.get("id") == item_id:
                    item["thumbnail_path"] = local_path
                    break

            self._model.update_thumbnail(item_id, local_path)

    # ── Placement logic ───────────────────────────────────────────────────

    def _compute_placement_options(self) -> list[dict[str, Any]]:
        """Compute where the video can be placed within the playlist."""
        return build_jw_media_placement_options(self._pl)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _set_loading(self, value: bool) -> None:
        if self._is_loading != value:
            self._is_loading = value
            self.isLoadingChanged.emit()

    def _emit_confirmed(self, target_list_id: str, target_index: int) -> None:
        """Emit ``jwMediaConfirmed``, clean up pending state, and notify."""
        if not self._pending_item:
            return

        # Build a clean item-data dict for consumers.
        item_data: dict[str, Any] = {
            k: v for k, v in self._pending_item.items()
            if not k.startswith("_")
        }

        self.jwMediaConfirmed.emit(item_data, target_list_id, target_index)

        title = item_data.get("title", "")
        self.itemAddedSuccessfully.emit(
            QCoreApplication.translate(
                "JWMediaCatalogBridge", "{title} added"
            ).format(title=title)
        )

        # Reset pending state.
        self._pending_item = None
        self.pendingItemChanged.emit()

        self._show_placement = False
        self.showPlacementChanged.emit()

        self._placement_options = []
        self.placementOptionsChanged.emit()


__all__ = [
    "build_jw_media_placement_options",
    "JWMediaCatalogBridge",
    "JWMediaCatalogModel",
]
