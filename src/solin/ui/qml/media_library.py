"""QML model, bridge, and icon provider for the media library views."""

from __future__ import annotations

import os

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Property,
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
from solin.core.media.download_storage import cached_path_for
from solin.styles.icons import (
    ICON_CLOUD_DOWNLOAD,
    ICON_MUSIC,
    ICON_PLAY_ALL,
    ICON_PLAY_SHUFFLE,
    ICON_VIDEO,
)
from solin.styles.theme import PALETTE


def java_to_py_fmt(fmt: str) -> str:
    result = fmt
    for java_tok, py_tok in [
        ("yyyy", "%Y"), ("MM", "%m"), ("dd", "%d"),
        ("HH", "%H"), ("hh", "%I"), ("mm", "%M"), ("ss", "%S"), ("a", "%p"),
    ]:
        result = result.replace(java_tok, py_tok)
    return result


_ICON_REFRESH = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M20 6v5h-5"/>'
    '<path d="M19.1 13.5a7.5 7.5 0 1 1-2.2-7.1L20 11"/>'
    '</svg>'
)

_ICON_SEARCH = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="11" cy="11" r="7"/>'
    '<path d="m21 21-4.3-4.3"/>'
    '</svg>'
)


def _svg_pixmap(svg_data: str, size: int, color: str) -> QPixmap:
    svg = svg_data.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    return pix


class MediaLibraryIconProvider(QQuickImageProvider):
    def __init__(self) -> None:
        super().__init__(QQuickImageProvider.ImageType.Pixmap)

    def requestPixmap(self, id_str: str, size, requestedSize):  # noqa: N802
        parts = id_str.split("/")
        name = parts[0] if parts else ""
        px = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 16
        color = f"#{parts[2]}" if len(parts) > 2 else PALETTE.text_muted
        icons = {
            "cloud_download": ICON_CLOUD_DOWNLOAD,
            "music": ICON_MUSIC,
            "video": ICON_VIDEO,
            "play_all": ICON_PLAY_ALL,
            "shuffle": ICON_PLAY_SHUFFLE,
            "refresh": _ICON_REFRESH,
            "search": _ICON_SEARCH,
        }
        svg = icons.get(name)
        if svg:
            return _svg_pixmap(svg, px, color)
        fallback = QPixmap(1, 1)
        fallback.fill(Qt.GlobalColor.transparent)
        return fallback


class MediaLibraryModel(QAbstractListModel):
    NumberTextRole = Qt.ItemDataRole.UserRole + 1
    TitleRole = Qt.ItemDataRole.UserRole + 2
    DurationTextRole = Qt.ItemDataRole.UserRole + 3
    CloudVisibleRole = Qt.ItemDataRole.UserRole + 4
    CloudDownloadingRole = Qt.ItemDataRole.UserRole + 5
    CloudTooltipRole = Qt.ItemDataRole.UserRole + 6
    MediaIconRole = Qt.ItemDataRole.UserRole + 7
    CloudProgressRole = Qt.ItemDataRole.UserRole + 8
    CloudQueuedRole = Qt.ItemDataRole.UserRole + 9

    def __init__(
        self,
        cache_manager: MediaCacheManager,
        parent=None,
    ) -> None:
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

    def roleNames(self):
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

    def rowCount(self, parent=None):
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self._items):
            return None
        item = self._items[index.row()]
        if role == self.NumberTextRole:
            number = item.get("number")
            return f"{int(number):>3}." if number is not None else ""
        if role == self.TitleRole:
            return item.get("title", "")
        if role == self.DurationTextRole:
            duration = int(item.get("duration") or 0)
            return f"{duration // 60}:{duration % 60:02d}" if duration else ""
        if role == self.CloudVisibleRole:
            return self._cloud_visible(item)
        if role == self.CloudDownloadingRole:
            return self._cache_manager.is_prefetching(item.get("url", ""))
        if role == self.CloudTooltipRole:
            url = item.get("url", "")
            mgr = self._cache_manager
            if mgr.is_queued(url):
                return tr_offline_queued()
            if mgr.is_prefetching(url):
                return item.get("_cloud_tooltip") or tr_offline_downloading()
            return item.get("_cloud_tooltip") or tr_offline_download()
        if role == self.MediaIconRole:
            return "music" if self._audio_mode else "video"
        if role == self.CloudProgressRole:
            return float(item.get("_cloud_progress", -1.0))
        if role == self.CloudQueuedRole:
            return self._cache_manager.is_queued(item.get("url", ""))
        return None

    def set_items(self, items: list[dict], audio_mode: bool) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._rebuild_url_rows()
        self._audio_mode = audio_mode
        self.endResetModel()

    def item_at(self, row_index: int) -> dict | None:
        if 0 <= row_index < len(self._items):
            return self._items[row_index]
        return None

    def cleanup(self) -> None:
        try:
            self._cache_manager.cache_changed.disconnect(self._on_cache_changed)
            self._cache_manager.cache_removed.disconnect(self._on_cache_removed)
            self._cache_manager.prefetch_progress.disconnect(self._on_prefetch_progress)
            self._cache_manager.prefetch_error.disconnect(self._on_prefetch_error)
            self._cache_manager.prefetch_queued.disconnect(self._on_prefetch_queued)
            self._cache_manager.prefetch_dequeued.disconnect(self._on_prefetch_dequeued)
        except Exception:  # noqa: BLE001 - Qt signal cleanup boundary
            log_ignored_exception(__name__, "Could not disconnect media cache prefetch signals")

    def _cloud_visible(self, item: dict) -> bool:
        url = item.get("url", "")
        if not MediaCacheManager.is_remote(url):
            return False
        if self._cache_manager.is_cached(url):
            return False
        return True

    @Slot(str)
    def _on_cache_changed(self, url: str) -> None:
        self._emit_url_changed(url, [
            self.CloudVisibleRole,
            self.CloudDownloadingRole,
            self.CloudQueuedRole,
            self.CloudProgressRole,
            self.CloudTooltipRole,
        ])

    @Slot(str)
    def _on_cache_removed(self, path: str) -> None:
        if not path:
            return
        removed_path = os.path.normcase(os.path.abspath(path))
        for row, item in enumerate(self._items):
            url = item.get("url", "")
            cached_path = (
                os.path.normcase(
                    os.path.abspath(
                        cached_path_for(url, self._cache_manager.media_cache_dir)
                    )
                )
                if url
                else ""
            )
            if cached_path == removed_path:
                item.pop("_cloud_tooltip", None)
                item.pop("_cloud_progress", None)
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [
                    self.CloudVisibleRole,
                    self.CloudDownloadingRole,
                    self.CloudQueuedRole,
                    self.CloudProgressRole,
                    self.CloudTooltipRole,
                ])

    @Slot(str, int, int)
    def _on_prefetch_progress(self, url: str, downloaded: int, total: int) -> None:
        if total > 0:
            pct = int(downloaded * 100 / total)
            for row in self._url_rows.get(url, []):
                item = self._items[row]
                item["_cloud_progress"] = pct / 100.0
                item["_cloud_tooltip"] = tr_offline_downloading_progress(pct)
        self._emit_url_changed(url, [self.CloudProgressRole, self.CloudTooltipRole])

    @Slot(str, str)
    def _on_prefetch_error(self, url: str, _message: str) -> None:
        for row in self._url_rows.get(url, []):
            item = self._items[row]
            item.pop("_cloud_progress", None)
            item.pop("_cloud_tooltip", None)
        self._emit_url_changed(url, [
            self.CloudDownloadingRole,
            self.CloudQueuedRole,
            self.CloudProgressRole,
            self.CloudTooltipRole,
        ])

    @Slot(str)
    def _on_prefetch_queued(self, url: str) -> None:
        self._emit_url_changed(url, [self.CloudQueuedRole, self.CloudTooltipRole])

    @Slot(str)
    def _on_prefetch_dequeued(self, url: str) -> None:
        self._emit_url_changed(url, [
            self.CloudDownloadingRole,
            self.CloudQueuedRole,
            self.CloudTooltipRole,
        ])

    def _emit_url_changed(self, url: str, roles: list[int] | None = None) -> None:
        if not url:
            return
        for row in self._url_rows.get(url, []):
            idx = self.index(row, 0)
            if roles is None:
                self.dataChanged.emit(idx, idx)
            else:
                self.dataChanged.emit(idx, idx, roles)

    def _rebuild_url_rows(self) -> None:
        self._url_rows = {}
        for row, item in enumerate(self._items):
            url = str(item.get("url", ""))
            if url:
                self._url_rows.setdefault(url, []).append(row)


class MediaLibraryBridge(QObject):
    stateChanged = Signal()
    tooltipsChanged = Signal()

    refreshRequested = Signal()
    playAllRequested = Signal()
    shuffleRequested = Signal()
    downloadAllRequested = Signal()
    searchChanged = Signal(str)
    modeChanged = Signal(str)
    itemPlayRequested = Signal(int)
    itemDownloadRequested = Signal(int)
    pointerEntered = Signal()
    pointerExited = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = ""
        self.subtitle = ""
        self.search_placeholder = ""
        self.status_text = ""
        self.count_text = ""
        self.cache_text = ""
        self.refresh_tooltip = ""
        self.play_all_tooltip = ""
        self.shuffle_tooltip = ""
        self.download_all_tooltip = ""
        self.video_tooltip = ""
        self.audio_tooltip = ""
        self.loading = False
        self.has_items = False
        self.supports_audio = False
        self.audio_mode = False
        self.refresh_enabled = True
        self.show_download_all = False
        self.download_all_enabled = False
        self.download_all_active = False

    @Property(str, notify=stateChanged)
    def titleText(self):  # noqa: N802
        return self.title

    @Property(str, notify=stateChanged)
    def subtitleText(self):  # noqa: N802
        return self.subtitle

    @Property(str, notify=stateChanged)
    def searchPlaceholder(self):  # noqa: N802
        return self.search_placeholder

    @Property(str, notify=stateChanged)
    def statusText(self):  # noqa: N802
        return self.status_text

    @Property(str, notify=stateChanged)
    def countText(self):  # noqa: N802
        return self.count_text

    @Property(str, notify=stateChanged)
    def cacheText(self):  # noqa: N802
        return self.cache_text

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
    def pointerEnter(self):  # noqa: N802
        self.pointerEntered.emit()

    @Slot()
    def pointerExit(self):  # noqa: N802
        self.pointerExited.emit()

    def update_state(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.stateChanged.emit()


