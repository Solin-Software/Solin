"""Models, bridge, and themed icons for the unified Library view."""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Property,
    QSortFilterProxyModel,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtSvg import QSvgRenderer

from solin.core.foundation.exception_logging import log_ignored_exception
from solin.core.i18n.strings import (
    tr_offline_download,
    tr_offline_downloading,
    tr_offline_downloading_progress,
    tr_offline_queued,
)
from solin.core.media.cache import MediaCacheManager
from solin.core.media.cache_listing import CachedMediaItem
from solin.core.media.download_storage import cached_path_for
from solin.styles.icons import (
    ICON_CHECK,
    ICON_CLOUD_DOWNLOAD,
    ICON_IMAGE,
    ICON_MUSIC,
    ICON_PLAY_ALL,
    ICON_PLAY_SHUFFLE,
    ICON_REFRESH,
    ICON_TRASH,
    ICON_VIDEO,
)
from solin.styles.theme import PALETTE


_ICON_CLOSE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round">'
    '<path d="m7 7 10 10M17 7 7 17"/></svg>'
)
_ICON_DOWNLOADS = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    ' stroke-linejoin="round"><path d="M12 3v11"/>'
    '<path d="m8 10 4 4 4-4"/><path d="M5 19h14"/></svg>'
)
_ICON_SELECT_ALL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    ' stroke-linejoin="round"><rect x="5" y="5" width="14" height="14" rx="3"/>'
    '<path d="m9 12 2 2 4-5"/></svg>'
)


def format_file_size(size: int) -> str:
    value = max(0, int(size))
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            precision = 0 if amount >= 100 else 1
            return f"{amount:.{precision}f} {unit}"
        amount /= 1024
    return f"{value} B"


def _svg_pixmap(svg_data: str, size: int, color: str) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg_data.replace("currentColor", color).encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pixmap


class LibraryIconProvider(QQuickImageProvider):
    """Render the existing application icon language inside QML."""

    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Pixmap)

    def requestPixmap(self, id_str: str, size, requestedSize):  # noqa: N802
        del size, requestedSize
        parts = id_str.split("/")
        name = parts[0] if parts else ""
        pixels = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 16
        color = f"#{parts[2]}" if len(parts) > 2 and parts[2] else PALETTE.text_muted
        svg = {
            "check": ICON_CHECK,
            "close": _ICON_CLOSE,
            "cloud_download": ICON_CLOUD_DOWNLOAD,
            "downloads": _ICON_DOWNLOADS,
            "image": ICON_IMAGE,
            "music": ICON_MUSIC,
            "play_all": ICON_PLAY_ALL,
            "refresh": ICON_REFRESH,
            "select_all": _ICON_SELECT_ALL,
            "shuffle": ICON_PLAY_SHUFFLE,
            "trash": ICON_TRASH,
            "video": ICON_VIDEO,
        }.get(name)
        if svg:
            return _svg_pixmap(svg, pixels, color)
        fallback = QPixmap(1, 1)
        fallback.fill(Qt.GlobalColor.transparent)
        return fallback


class DownloadedMediaThumbnailProvider(QQuickImageProvider):
    """Serve a bounded set of thumbnails for virtualized downloaded rows."""

    _DEFAULT_WIDTH = 124
    _DEFAULT_HEIGHT = 76

    def __init__(self, *, capacity: int = 256) -> None:
        super().__init__(QQuickImageProvider.ImageType.Pixmap)
        self._capacity = max(32, int(capacity))
        self._pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._versions: dict[str, int] = {}

    @staticmethod
    def _key(path: str) -> str:
        identity = os.path.normcase(os.path.abspath(path)).encode(
            "utf-8",
            errors="surrogatepass",
        )
        return hashlib.sha256(identity).hexdigest()

    def store(self, path: str, pixmap: QPixmap) -> str:
        if not pixmap or pixmap.isNull():
            return ""
        key = self._key(path)
        bounded = pixmap.scaled(
            self._DEFAULT_WIDTH * 2,
            self._DEFAULT_HEIGHT * 2,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._pixmaps[key] = bounded
        self._pixmaps.move_to_end(key)
        while len(self._pixmaps) > self._capacity:
            self._pixmaps.popitem(last=False)
        version = self._versions.get(key, 0) + 1
        self._versions[key] = version
        return f"image://librarythumbs/{key}/{version}"

    def discard(self, paths: Iterable[str]) -> None:
        for path in paths:
            key = self._key(path)
            self._pixmaps.pop(key, None)
            self._versions.pop(key, None)

    def requestPixmap(self, id_str: str, size, requestedSize):  # noqa: N802
        del size
        key = id_str.split("/", 1)[0]
        pixmap = self._pixmaps.get(key)
        width = requestedSize.width() if requestedSize.width() > 0 else self._DEFAULT_WIDTH
        height = requestedSize.height() if requestedSize.height() > 0 else self._DEFAULT_HEIGHT
        if pixmap is None or pixmap.isNull():
            fallback = QPixmap(1, 1)
            fallback.fill(Qt.GlobalColor.transparent)
            return fallback
        self._pixmaps.move_to_end(key)
        scaled = pixmap.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        left = max(0, (scaled.width() - width) // 2)
        top = max(0, (scaled.height() - height) // 2)
        return scaled.copy(left, top, width, height)


class LibrarySectionModel(QAbstractListModel):
    """Registry-backed navigation model for current and future Library sections."""

    SectionIdRole = Qt.ItemDataRole.UserRole + 1
    LabelRole = Qt.ItemDataRole.UserRole + 2
    ViewKindRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._sections: list[dict[str, str]] = []

    def roleNames(self):  # noqa: N802
        return {
            self.SectionIdRole: b"sectionId",
            self.LabelRole: b"label",
            self.ViewKindRole: b"viewKind",
        }

    def rowCount(self, parent=None):  # noqa: N802
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self._sections)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._sections):
            return None
        section = self._sections[index.row()]
        if role == self.SectionIdRole:
            return section["id"]
        if role == self.LabelRole:
            return section["label"]
        if role == self.ViewKindRole:
            return section["view_kind"]
        return None

    def set_sections(self, sections: Iterable[dict[str, str]]) -> None:
        self.beginResetModel()
        self._sections = [dict(section) for section in sections]
        self.endResetModel()


class MediaCatalogModel(QAbstractListModel):
    """Virtualized catalog rows shared by songs and music videos."""

    NumberTextRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = Qt.ItemDataRole.UserRole + 2
    DurationTextRole = Qt.ItemDataRole.UserRole + 3
    CloudVisibleRole = Qt.ItemDataRole.UserRole + 4
    CloudDownloadingRole = Qt.ItemDataRole.UserRole + 5
    CloudTooltipRole = Qt.ItemDataRole.UserRole + 6
    MediaIconRole = Qt.ItemDataRole.UserRole + 7
    CloudProgressRole = Qt.ItemDataRole.UserRole + 8
    CloudQueuedRole = Qt.ItemDataRole.UserRole + 9

    def __init__(self, cache_manager: MediaCacheManager, parent=None) -> None:
        super().__init__(parent)
        self._cache_manager = cache_manager
        self._items: list[dict] = []
        self._url_rows: dict[str, list[int]] = {}
        self._audio_mode = False
        cache_manager.cache_changed.connect(self._on_cache_changed)
        cache_manager.cache_removed.connect(self._on_cache_removed)
        cache_manager.prefetch_progress.connect(self._on_prefetch_progress)
        cache_manager.prefetch_error.connect(self._on_prefetch_error)
        cache_manager.prefetch_queued.connect(self._on_prefetch_queued)
        cache_manager.prefetch_dequeued.connect(self._on_prefetch_dequeued)

    def roleNames(self):  # noqa: N802
        return {
            self.NumberTextRole: b"numberText",
            self.TitleRole: b"title",
            self.DurationTextRole: b"durationText",
            self.CloudVisibleRole: b"cloudVisible",
            self.CloudDownloadingRole: b"cloudDownloading",
            self.CloudTooltipRole: b"cloudTooltip",
            self.MediaIconRole: b"mediaIcon",
            self.CloudProgressRole: b"cloudProgress",
            self.CloudQueuedRole: b"cloudQueued",
        }

    def rowCount(self, parent=None):  # noqa: N802
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.NumberTextRole:
            number = item.get("number")
            return f"{int(number)}." if number is not None else ""
        if role == self.TitleRole:
            return str(item.get("title", ""))
        if role == self.DurationTextRole:
            duration = int(item.get("duration") or 0)
            return f"{duration // 60}:{duration % 60:02d}" if duration else ""
        if role == self.CloudVisibleRole:
            return self._cloud_visible(item)
        if role == self.CloudDownloadingRole:
            return self._cache_manager.is_prefetching(str(item.get("url", "")))
        if role == self.CloudTooltipRole:
            return self._cloud_tooltip(item)
        if role == self.MediaIconRole:
            return self._media_icon(item)
        if role == self.CloudProgressRole:
            return float(item.get("_cloud_progress", -1.0))
        if role == self.CloudQueuedRole:
            return self._cache_manager.is_queued(str(item.get("url", "")))
        return None

    def set_items(self, items: Iterable[dict], *, audio_mode: bool) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._audio_mode = bool(audio_mode)
        self._url_rows = {}
        for row, item in enumerate(self._items):
            url = str(item.get("url", ""))
            if url:
                self._url_rows.setdefault(url, []).append(row)
        self.endResetModel()

    def item_at(self, row_index: int) -> dict | None:
        return self._items[row_index] if 0 <= row_index < len(self._items) else None

    def cleanup(self) -> None:
        manager = self._cache_manager
        try:
            manager.cache_changed.disconnect(self._on_cache_changed)
            manager.cache_removed.disconnect(self._on_cache_removed)
            manager.prefetch_progress.disconnect(self._on_prefetch_progress)
            manager.prefetch_error.disconnect(self._on_prefetch_error)
            manager.prefetch_queued.disconnect(self._on_prefetch_queued)
            manager.prefetch_dequeued.disconnect(self._on_prefetch_dequeued)
        except Exception:  # noqa: BLE001 - Qt cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect Library cache signals")

    def _cloud_visible(self, item: dict) -> bool:
        url = str(item.get("url", ""))
        return bool(MediaCacheManager.is_remote(url) and not self._cache_manager.is_cached(url))

    def _cloud_tooltip(self, item: dict) -> str:
        url = str(item.get("url", ""))
        if self._cache_manager.is_queued(url):
            return tr_offline_queued()
        if self._cache_manager.is_prefetching(url):
            return str(item.get("_cloud_tooltip") or tr_offline_downloading())
        return str(item.get("_cloud_tooltip") or tr_offline_download())

    def _media_icon(self, item: dict) -> str:
        if self._audio_mode:
            return "music"
        media_type = str(item.get("type", "")).casefold()
        if media_type in {"audio", "video", "image"}:
            return "music" if media_type == "audio" else media_type
        suffix = Path(urlsplit(str(item.get("url", ""))).path).suffix.casefold()
        return "music" if suffix in {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav"} else "video"

    @Slot(str)
    def _on_cache_changed(self, url: str) -> None:
        self._emit_url_changed(url)

    @Slot(str)
    def _on_cache_removed(self, path: str) -> None:
        if not path:
            return
        removed_path = os.path.normcase(os.path.abspath(path))
        roles = self._cache_roles()
        for row, item in enumerate(self._items):
            url = str(item.get("url", ""))
            cached_path = (
                os.path.normcase(
                    os.path.abspath(cached_path_for(url, self._cache_manager.media_cache_dir))
                )
                if url
                else ""
            )
            if cached_path == removed_path:
                item.pop("_cloud_tooltip", None)
                item.pop("_cloud_progress", None)
                index = self.index(row, 0)
                self.dataChanged.emit(index, index, roles)

    @Slot(str, int, int)
    def _on_prefetch_progress(self, url: str, downloaded: int, total: int) -> None:
        if total > 0:
            percent = int(downloaded * 100 / total)
            for row in self._url_rows.get(url, ()):
                self._items[row]["_cloud_progress"] = percent / 100.0
                self._items[row]["_cloud_tooltip"] = tr_offline_downloading_progress(percent)
        self._emit_url_changed(url)

    @Slot(str, str)
    def _on_prefetch_error(self, url: str, _message: str) -> None:
        for row in self._url_rows.get(url, ()):
            self._items[row].pop("_cloud_progress", None)
            self._items[row].pop("_cloud_tooltip", None)
        self._emit_url_changed(url)

    @Slot(str)
    def _on_prefetch_queued(self, url: str) -> None:
        self._emit_url_changed(url)

    @Slot(str)
    def _on_prefetch_dequeued(self, url: str) -> None:
        self._emit_url_changed(url)

    def _emit_url_changed(self, url: str) -> None:
        if not url:
            return
        roles = self._cache_roles()
        for row in self._url_rows.get(url, ()):
            index = self.index(row, 0)
            self.dataChanged.emit(index, index, roles)

    def _cache_roles(self) -> list[int]:
        return [
            self.CloudVisibleRole,
            self.CloudDownloadingRole,
            self.CloudQueuedRole,
            self.CloudProgressRole,
            self.CloudTooltipRole,
        ]


class DownloadedMediaModel(QAbstractListModel):
    """Lightweight rows for large cached-media collections."""

    TitleRole = Qt.ItemDataRole.UserRole + 1
    FileNameRole = Qt.ItemDataRole.UserRole + 2
    SizeTextRole = Qt.ItemDataRole.UserRole + 3
    MediaIconRole = Qt.ItemDataRole.UserRole + 4
    MediaTypeRole = Qt.ItemDataRole.UserRole + 5
    SelectedRole = Qt.ItemDataRole.UserRole + 6
    PathRole = Qt.ItemDataRole.UserRole + 7
    ThumbnailSourceRole = Qt.ItemDataRole.UserRole + 8

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[CachedMediaItem] = []
        self._selected_paths: set[str] = set()
        self._path_rows: dict[str, int] = {}
        self._thumbnail_sources: dict[str, str] = {}

    def roleNames(self):  # noqa: N802
        return {
            self.TitleRole: b"title",
            self.FileNameRole: b"fileName",
            self.SizeTextRole: b"sizeText",
            self.MediaIconRole: b"mediaIcon",
            self.MediaTypeRole: b"mediaType",
            self.SelectedRole: b"selected",
            self.PathRole: b"sourcePath",
            self.ThumbnailSourceRole: b"thumbnailSource",
        }

    def rowCount(self, parent=None):  # noqa: N802
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.TitleRole:
            return item.display_title
        if role == self.FileNameRole:
            return item.filename
        if role == self.SizeTextRole:
            return format_file_size(item.size)
        if role == self.MediaIconRole:
            return "music" if item.media_type == "audio" else item.media_type
        if role == self.MediaTypeRole:
            return item.media_type
        if role == self.SelectedRole:
            return item.path in self._selected_paths
        if role == self.PathRole:
            return item.path
        if role == self.ThumbnailSourceRole:
            return self._thumbnail_sources.get(item.path, "")
        return None

    def set_items(
        self,
        items: Iterable[CachedMediaItem],
        selected_paths: set[str],
    ) -> None:
        previous_titles = {item.path: item.display_title for item in self._items}
        resolved_items: list[CachedMediaItem] = []
        for item in items:
            previous_title = previous_titles.get(item.path, "")
            if not previous_title or previous_title == item.display_title:
                resolved_items.append(item)
                continue
            resolved_items.append(
                CachedMediaItem(
                    path=item.path,
                    filename=item.filename,
                    display_title=previous_title,
                    size=item.size,
                    media_type=item.media_type,
                    original_url=item.original_url,
                )
            )
        self.beginResetModel()
        self._items = resolved_items
        self._selected_paths = set(selected_paths)
        self._path_rows = {item.path: row for row, item in enumerate(self._items)}
        self.endResetModel()

    def append_items(self, items: Iterable[CachedMediaItem]) -> None:
        additions = [item for item in items if item.path not in self._path_rows]
        if not additions:
            return
        first = len(self._items)
        last = first + len(additions) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        for item in additions:
            self._path_rows[item.path] = len(self._items)
            self._items.append(item)
        self.endInsertRows()

    def item_at(self, row_index: int) -> CachedMediaItem | None:
        return self._items[row_index] if 0 <= row_index < len(self._items) else None

    def item_for_path(self, path: str) -> CachedMediaItem | None:
        row = self._path_rows.get(path)
        return self._items[row] if row is not None else None

    def items(self) -> tuple[CachedMediaItem, ...]:
        return tuple(self._items)

    def set_selected_paths(self, selected_paths: set[str]) -> None:
        changed_rows = sorted(
            self._path_rows[path]
            for path in self._selected_paths.symmetric_difference(selected_paths)
            if path in self._path_rows
        )
        self._selected_paths = set(selected_paths)
        if not changed_rows:
            return
        range_start = range_end = changed_rows[0]
        for row in changed_rows[1:]:
            if row == range_end + 1:
                range_end = row
                continue
            self.dataChanged.emit(
                self.index(range_start, 0),
                self.index(range_end, 0),
                [self.SelectedRole],
            )
            range_start = range_end = row
        self.dataChanged.emit(
            self.index(range_start, 0),
            self.index(range_end, 0),
            [self.SelectedRole],
        )

    def update_title(self, path: str, title: str) -> CachedMediaItem | None:
        row = self._path_rows.get(path)
        cleaned_title = str(title or "").strip()
        if row is None or not cleaned_title:
            return None
        current = self._items[row]
        if current.display_title == cleaned_title:
            return current
        updated = CachedMediaItem(
            path=current.path,
            filename=current.filename,
            display_title=cleaned_title,
            size=current.size,
            media_type=current.media_type,
            original_url=current.original_url,
        )
        self._items[row] = updated
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [self.TitleRole])
        return updated

    def update_thumbnail_source(self, path: str, source: str) -> bool:
        row = self._path_rows.get(path)
        if row is None or not source or self._thumbnail_sources.get(path) == source:
            return False
        self._thumbnail_sources[path] = source
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, [self.ThumbnailSourceRole])
        return True

    def discard_metadata(self, paths: Iterable[str]) -> None:
        for path in paths:
            self._thumbnail_sources.pop(path, None)


class DownloadedMediaProxyModel(QSortFilterProxyModel):
    """Filter downloaded media without rebuilding or resetting the source model."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._query = ""
        self._media_filter = "all"
        self.setDynamicSortFilter(True)

    def set_query(self, query: str) -> None:
        folded = str(query or "").strip().casefold()
        if folded != self._query:
            self.beginFilterChange()
            self._query = folded
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_media_filter(self, media_filter: str) -> None:
        if media_filter != self._media_filter:
            self.beginFilterChange()
            self._media_filter = media_filter
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        source = self.sourceModel()
        if not isinstance(source, DownloadedMediaModel):
            return False
        item = source.item_at(source_row)
        if item is None:
            return False
        if self._media_filter != "all" and item.media_type != self._media_filter:
            return False
        return bool(
            not self._query
            or self._query in item.display_title.casefold()
            or self._query in item.filename.casefold()
        )

    def visible_items(self) -> list[CachedMediaItem]:
        source = self.sourceModel()
        if not isinstance(source, DownloadedMediaModel):
            return []
        result: list[CachedMediaItem] = []
        for proxy_row in range(self.rowCount()):
            source_index = self.mapToSource(self.index(proxy_row, 0))
            item = source.item_at(source_index.row())
            if item is not None:
                result.append(item)
        return result


class LibraryBridge(QObject):
    """Stable QML API for the Library shell and its lazily loaded sections."""

    active_section: str
    page_title: str
    page_subtitle: str
    songs_label: str
    clips_label: str
    downloads_label: str
    section_title: str
    section_subtitle: str
    search_placeholder: str
    search_text: str
    status_text: str
    refresh_tooltip: str
    play_all_tooltip: str
    shuffle_tooltip: str
    download_all_tooltip: str
    video_tooltip: str
    audio_tooltip: str
    loading: bool
    has_items: bool
    supports_audio: bool
    audio_mode: bool
    refresh_enabled: bool
    show_download_all: bool
    download_all_enabled: bool
    download_all_active: bool
    downloads_summary: str
    downloads_filter: str
    downloads_all_label: str
    downloads_video_label: str
    downloads_audio_label: str
    downloads_image_label: str
    downloads_selection_text: str
    downloads_selection_size: str
    downloads_select_all_text: str
    downloads_delete_label: str
    downloads_delete_tooltip: str
    downloads_clear_selection_tooltip: str
    downloads_empty_text: str
    downloads_loading: bool
    downloads_has_items: bool
    downloads_has_selection: bool
    downloads_deleting: bool

    stateChanged = Signal()

    sectionRequested = Signal(str)
    refreshRequested = Signal()
    playAllRequested = Signal()
    shuffleRequested = Signal()
    downloadAllRequested = Signal()
    searchChanged = Signal(str)
    modeChanged = Signal(str)
    itemPlayRequested = Signal(int)
    itemDownloadRequested = Signal(int)

    downloadsRefreshRequested = Signal()
    downloadsSearchChanged = Signal(str)
    downloadsFilterChanged = Signal(str)
    downloadsItemPlayRequested = Signal(str)
    downloadsItemSelectionRequested = Signal(str)
    downloadsInfoRequested = Signal(str)
    downloadsToggleSelectAllRequested = Signal()
    downloadsClearSelectionRequested = Signal()
    downloadsDeleteRequested = Signal()

    pointerEntered = Signal()
    pointerExited = Signal()

    _DEFAULTS = {
        "active_section": "songs",
        "page_title": "",
        "page_subtitle": "",
        "songs_label": "",
        "clips_label": "",
        "downloads_label": "",
        "section_title": "",
        "section_subtitle": "",
        "search_placeholder": "",
        "search_text": "",
        "status_text": "",
        "refresh_tooltip": "",
        "play_all_tooltip": "",
        "shuffle_tooltip": "",
        "download_all_tooltip": "",
        "video_tooltip": "",
        "audio_tooltip": "",
        "loading": False,
        "has_items": False,
        "supports_audio": False,
        "audio_mode": False,
        "refresh_enabled": True,
        "show_download_all": False,
        "download_all_enabled": False,
        "download_all_active": False,
        "downloads_summary": "",
        "downloads_filter": "all",
        "downloads_all_label": "",
        "downloads_video_label": "",
        "downloads_audio_label": "",
        "downloads_image_label": "",
        "downloads_selection_text": "",
        "downloads_selection_size": "",
        "downloads_select_all_text": "",
        "downloads_delete_label": "",
        "downloads_delete_tooltip": "",
        "downloads_clear_selection_tooltip": "",
        "downloads_empty_text": "",
        "downloads_loading": False,
        "downloads_has_items": False,
        "downloads_has_selection": False,
        "downloads_deleting": False,
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        for name, value in self._DEFAULTS.items():
            setattr(self, name, value)

    def update_state(self, **changes) -> None:
        changed = False
        for name, value in changes.items():
            if name not in self._DEFAULTS:
                raise AttributeError(f"Unknown Library state field: {name}")
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if changed:
            self.stateChanged.emit()

    @Property(str, notify=stateChanged)
    def activeSection(self):  # noqa: N802
        return self.active_section

    @Property(str, notify=stateChanged)
    def pageTitle(self):  # noqa: N802
        return self.page_title

    @Property(str, notify=stateChanged)
    def pageSubtitle(self):  # noqa: N802
        return self.page_subtitle

    @Property(str, notify=stateChanged)
    def songsLabel(self):  # noqa: N802
        return self.songs_label

    @Property(str, notify=stateChanged)
    def clipsLabel(self):  # noqa: N802
        return self.clips_label

    @Property(str, notify=stateChanged)
    def downloadsLabel(self):  # noqa: N802
        return self.downloads_label

    @Property(str, notify=stateChanged)
    def sectionTitle(self):  # noqa: N802
        return self.section_title

    @Property(str, notify=stateChanged)
    def sectionSubtitle(self):  # noqa: N802
        return self.section_subtitle

    @Property(str, notify=stateChanged)
    def searchPlaceholder(self):  # noqa: N802
        return self.search_placeholder

    @Property(str, notify=stateChanged)
    def searchText(self):  # noqa: N802
        return self.search_text

    @Property(str, notify=stateChanged)
    def statusText(self):  # noqa: N802
        return self.status_text

    @Property(str, notify=stateChanged)
    def refreshTooltip(self):  # noqa: N802
        return self.refresh_tooltip

    @Property(str, notify=stateChanged)
    def playAllTooltip(self):  # noqa: N802
        return self.play_all_tooltip

    @Property(str, notify=stateChanged)
    def shuffleTooltip(self):  # noqa: N802
        return self.shuffle_tooltip

    @Property(str, notify=stateChanged)
    def downloadAllTooltip(self):  # noqa: N802
        return self.download_all_tooltip

    @Property(str, notify=stateChanged)
    def videoTooltip(self):  # noqa: N802
        return self.video_tooltip

    @Property(str, notify=stateChanged)
    def audioTooltip(self):  # noqa: N802
        return self.audio_tooltip

    @Property(bool, notify=stateChanged)
    def isLoading(self):  # noqa: N802
        return self.loading

    @Property(bool, notify=stateChanged)
    def hasItems(self):  # noqa: N802
        return self.has_items

    @Property(bool, notify=stateChanged)
    def supportsAudio(self):  # noqa: N802
        return self.supports_audio

    @Property(bool, notify=stateChanged)
    def audioMode(self):  # noqa: N802
        return self.audio_mode

    @Property(bool, notify=stateChanged)
    def refreshEnabled(self):  # noqa: N802
        return self.refresh_enabled

    @Property(bool, notify=stateChanged)
    def showDownloadAll(self):  # noqa: N802
        return self.show_download_all

    @Property(bool, notify=stateChanged)
    def downloadAllEnabled(self):  # noqa: N802
        return self.download_all_enabled

    @Property(bool, notify=stateChanged)
    def downloadAllActive(self):  # noqa: N802
        return self.download_all_active

    @Property(str, notify=stateChanged)
    def downloadsSummary(self):  # noqa: N802
        return self.downloads_summary

    @Property(str, notify=stateChanged)
    def downloadsFilter(self):  # noqa: N802
        return self.downloads_filter

    @Property(str, notify=stateChanged)
    def downloadsAllLabel(self):  # noqa: N802
        return self.downloads_all_label

    @Property(str, notify=stateChanged)
    def downloadsVideoLabel(self):  # noqa: N802
        return self.downloads_video_label

    @Property(str, notify=stateChanged)
    def downloadsAudioLabel(self):  # noqa: N802
        return self.downloads_audio_label

    @Property(str, notify=stateChanged)
    def downloadsImageLabel(self):  # noqa: N802
        return self.downloads_image_label

    @Property(str, notify=stateChanged)
    def downloadsSelectionText(self):  # noqa: N802
        return self.downloads_selection_text

    @Property(str, notify=stateChanged)
    def downloadsSelectionSize(self):  # noqa: N802
        return self.downloads_selection_size

    @Property(str, notify=stateChanged)
    def downloadsSelectAllText(self):  # noqa: N802
        return self.downloads_select_all_text

    @Property(str, notify=stateChanged)
    def downloadsDeleteTooltip(self):  # noqa: N802
        return self.downloads_delete_tooltip

    @Property(str, notify=stateChanged)
    def downloadsDeleteLabel(self):  # noqa: N802
        return self.downloads_delete_label

    @Property(str, notify=stateChanged)
    def downloadsClearSelectionTooltip(self):  # noqa: N802
        return self.downloads_clear_selection_tooltip

    @Property(str, notify=stateChanged)
    def downloadsEmptyText(self):  # noqa: N802
        return self.downloads_empty_text

    @Property(bool, notify=stateChanged)
    def downloadsLoading(self):  # noqa: N802
        return self.downloads_loading

    @Property(bool, notify=stateChanged)
    def downloadsHasItems(self):  # noqa: N802
        return self.downloads_has_items

    @Property(bool, notify=stateChanged)
    def downloadsHasSelection(self):  # noqa: N802
        return self.downloads_has_selection

    @Property(bool, notify=stateChanged)
    def downloadsDeleting(self):  # noqa: N802
        return self.downloads_deleting

    @Slot(str)
    def setSection(self, section: str):  # noqa: N802
        self.sectionRequested.emit(section)

    @Slot()
    def refreshClicked(self):  # noqa: N802
        self.refreshRequested.emit()

    @Slot()
    def playAllClicked(self):  # noqa: N802
        self.playAllRequested.emit()

    @Slot()
    def shuffleClicked(self):  # noqa: N802
        self.shuffleRequested.emit()

    @Slot()
    def downloadAllClicked(self):  # noqa: N802
        self.downloadAllRequested.emit()

    @Slot(str)
    def setSearchText(self, text: str):  # noqa: N802
        self.searchChanged.emit(text)

    @Slot(str)
    def setMode(self, mode: str):  # noqa: N802
        self.modeChanged.emit(mode)

    @Slot(int)
    def playItem(self, row_index: int):  # noqa: N802
        self.itemPlayRequested.emit(row_index)

    @Slot(int)
    def downloadItem(self, row_index: int):  # noqa: N802
        self.itemDownloadRequested.emit(row_index)

    @Slot()
    def refreshDownloads(self):  # noqa: N802
        self.downloadsRefreshRequested.emit()

    @Slot(str)
    def setDownloadsSearchText(self, text: str):  # noqa: N802
        self.downloadsSearchChanged.emit(text)

    @Slot(str)
    def setDownloadsFilter(self, media_filter: str):  # noqa: N802
        self.downloadsFilterChanged.emit(media_filter)

    @Slot(str)
    def playDownloadedItem(self, source_path: str):  # noqa: N802
        self.downloadsItemPlayRequested.emit(source_path)

    @Slot(str)
    def toggleDownloadedItem(self, source_path: str):  # noqa: N802
        self.downloadsItemSelectionRequested.emit(source_path)

    @Slot(str)
    def requestDownloadedInfo(self, source_path: str):  # noqa: N802
        self.downloadsInfoRequested.emit(source_path)

    @Slot()
    def toggleSelectAllDownloads(self):  # noqa: N802
        self.downloadsToggleSelectAllRequested.emit()

    @Slot()
    def clearDownloadsSelection(self):  # noqa: N802
        self.downloadsClearSelectionRequested.emit()

    @Slot()
    def deleteSelectedDownloads(self):  # noqa: N802
        self.downloadsDeleteRequested.emit()

    @Slot()
    def pointerEnter(self):  # noqa: N802
        self.pointerEntered.emit()

    @Slot()
    def pointerExit(self):  # noqa: N802
        self.pointerExited.emit()
