from __future__ import annotations

import os
import random
import threading
import uuid
from datetime import datetime

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    Property,
    QByteArray,
    QUrl,
    QTimer,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QPainter, QPixmap, QSurfaceFormat
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from ..core.jw.language_context import jw_media_language_context
from ..core.jw.media_api import fetch_clips
from ..core.jw.songs import JWSongsStore
from ..core.foundation.exception_logging import log_ignored_exception
from ..core.media.cache import MediaCacheManager, cached_path_for
from ..core.i18n.strings import (
    tr_offline_download,
    tr_offline_downloading,
    tr_offline_downloading_progress,
    tr_offline_queued,
)
from ..core.i18n.manager import LanguageManager
from ..core.ui.helpers import begin_qml_pointer_cursor, end_qml_pointer_cursor
from ..qml_module import load_qml_type
from ..styles.icons import (
    ICON_CLOUD_DOWNLOAD,
    ICON_MUSIC,
    ICON_PLAY_ALL,
    ICON_PLAY_SHUFFLE,
    ICON_VIDEO,
)

def _java_to_py_fmt(fmt: str) -> str:
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
        color = f"#{parts[2]}" if len(parts) > 2 else "#8b949e"
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[dict] = []
        self._url_rows: dict[str, list[int]] = {}
        self._audio_mode = False
        MediaCacheManager.instance().cache_changed.connect(self._on_cache_changed)
        MediaCacheManager.instance().cache_removed.connect(self._on_cache_removed)
        MediaCacheManager.instance().prefetch_progress.connect(self._on_prefetch_progress)
        MediaCacheManager.instance().prefetch_error.connect(self._on_prefetch_error)
        MediaCacheManager.instance().prefetch_queued.connect(self._on_prefetch_queued)
        MediaCacheManager.instance().prefetch_dequeued.connect(self._on_prefetch_dequeued)

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
            return MediaCacheManager.instance().is_prefetching(item.get("url", ""))
        if role == self.CloudTooltipRole:
            url = item.get("url", "")
            mgr = MediaCacheManager.instance()
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
            return MediaCacheManager.instance().is_queued(item.get("url", ""))
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
            MediaCacheManager.instance().cache_changed.disconnect(self._on_cache_changed)
            MediaCacheManager.instance().cache_removed.disconnect(self._on_cache_removed)
            MediaCacheManager.instance().prefetch_progress.disconnect(self._on_prefetch_progress)
            MediaCacheManager.instance().prefetch_error.disconnect(self._on_prefetch_error)
            MediaCacheManager.instance().prefetch_queued.disconnect(self._on_prefetch_queued)
            MediaCacheManager.instance().prefetch_dequeued.disconnect(self._on_prefetch_dequeued)
        except Exception:
            log_ignored_exception(__name__, "Could not disconnect media cache prefetch signals")

    def _cloud_visible(self, item: dict) -> bool:
        url = item.get("url", "")
        if not MediaCacheManager.is_remote(url):
            return False
        if MediaCacheManager.instance().is_cached(url):
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
            cached_path = os.path.normcase(os.path.abspath(cached_path_for(url))) if url else ""
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


class MediaLibraryWidget(QWidget):
    project_video_signal = Signal(str, str, object, str)
    _loaded_signal = Signal(list, str, float, bool)
    _error_signal = Signal(str)

    def __init__(self, kind: str, lang_manager: LanguageManager, media_ctrl=None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.lang = lang_manager
        self.items: list[dict] = []
        self.filtered_items: list[dict] = []
        if self.kind == "songs":
            self.songs = self.items
            self.filtered_songs = self.filtered_items
        else:
            self.clips = self.items
            self.filtered_clips = self.filtered_items
        self._query = ""
        self._audio_mode = False
        self._song_request_key = ""
        self._songs_store = JWSongsStore.instance() if self.kind == "songs" else None
        self._qml_pointer_depth = 0
        self._disposed = False
        self._download_all_batch_ids: dict[str, str] = {"video": "", "audio": ""}
        self._download_all_error_batches: set[str] = set()
        self._download_all_refresh_timer = QTimer(self)
        self._download_all_refresh_timer.setSingleShot(True)
        self._download_all_refresh_timer.setInterval(120)
        self._download_all_refresh_timer.timeout.connect(self._refresh_download_all_state)

        self.model = MediaLibraryModel(self)
        self.bridge = MediaLibraryBridge(self)
        self._connect_signals()
        self._build_qml()
        self._sync_static_text()
        self._update_mode_availability()
        self._load_items()

    def _build_qml(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.qml_widget = QQuickWidget()
        fmt = QSurfaceFormat()
        fmt.setAlphaBufferSize(8)
        self.qml_widget.setFormat(fmt)
        self.qml_widget.setParent(self)
        self.qml_widget.setClearColor(QColor("#0d1117"))
        self.qml_widget.setMouseTracking(True)
        self.qml_widget.installEventFilter(self)
        self.qml_widget.engine().addImageProvider("mediaicons", MediaLibraryIconProvider())

        ctx = self.qml_widget.rootContext()
        ctx.setContextProperty("libraryModel", self.model)
        ctx.setContextProperty("controller", self.bridge)

        load_qml_type(self.qml_widget, "MediaLibraryView")
        self.qml_widget.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        root.addWidget(self.qml_widget, stretch=1)

    def _connect_signals(self) -> None:
        self._loaded_signal.connect(self._on_items_loaded)
        self._error_signal.connect(self._on_items_error)
        if self._songs_store is not None:
            self._songs_store.songs_ready.connect(self._on_song_store_ready)
            self._songs_store.songs_failed.connect(self._on_song_store_failed)
            self._songs_store.loading_changed.connect(self._on_song_store_loading_changed)
        self.bridge.refreshRequested.connect(lambda: self._load_items(force=True))
        self.bridge.playAllRequested.connect(self._play_all)
        self.bridge.shuffleRequested.connect(self._play_shuffle)
        self.bridge.downloadAllRequested.connect(self._download_all_current_mode)
        self.bridge.searchChanged.connect(self._filter_items)
        self.bridge.modeChanged.connect(self._set_mode)
        self.bridge.itemPlayRequested.connect(self._on_item_play_by_row)
        self.bridge.itemDownloadRequested.connect(self._download_by_row)
        self.bridge.pointerEntered.connect(self.begin_qml_pointer_cursor)
        self.bridge.pointerExited.connect(self.end_qml_pointer_cursor)

        svc = getattr(self.lang, "jw_lang_service", None)
        if svc is not None:
            svc.media_language_changed.connect(self._on_media_language_changed)

        mgr = MediaCacheManager.instance()
        mgr.cache_changed.connect(self._on_cache_state_changed)
        mgr.cache_removed.connect(self._on_cache_state_changed)
        mgr.prefetch_queued.connect(self._on_cache_state_changed)
        mgr.prefetch_dequeued.connect(self._on_cache_state_changed)
        mgr.prefetch_batch_changed.connect(self._on_prefetch_batch_changed)
        mgr.prefetch_batch_error.connect(self._on_prefetch_batch_error)

    def _sync_static_text(self) -> None:
        self.bridge.update_state(
            title=self.tr("Songs") if self.kind == "songs" else self.tr("Original Songs"),
            search_placeholder=self.tr("Search song...") if self.kind == "songs" else self.tr("Search clip..."),
            refresh_tooltip=self.tr("Refresh"),
            play_all_tooltip=self.tr("Play all (in order)"),
            shuffle_tooltip=self.tr("Play in random order"),
            download_all_tooltip=self._download_all_title(),
            video_tooltip=self.tr("Video songs"),
            audio_tooltip=self.tr("Audio songs"),
            audio_mode=self._audio_mode,
        )
        self._refresh_download_all_state()

    def _update_mode_availability(self) -> None:
        context = jw_media_language_context(self.lang)
        supports_audio = self.kind == "songs" and not context.is_sign_language
        if not supports_audio and self._audio_mode:
            self._audio_mode = False
        self.bridge.update_state(supports_audio=supports_audio, audio_mode=self._audio_mode)
        self._refresh_download_all_state()

    def _set_loading(self, text: str) -> None:
        self.bridge.update_state(
            loading=True,
            status_text=text,
            has_items=False,
            refresh_enabled=False,
            cache_text="",
        )
        self._refresh_download_all_state()

    def _load_items(self, force: bool = False) -> None:
        loading_text = self.tr("Loading songs...") if self.kind == "songs" else self.tr("Loading clips...")
        self._set_loading(loading_text)
        self.model.set_items([], self._audio_mode)

        context = jw_media_language_context(self.lang)
        audio_mode = self._audio_mode
        kind = self.kind

        if kind == "songs" and self._songs_store is not None:
            request = self._songs_store.request_for(
                api_code=context.api_code,
                fallback_code=context.fallback_code,
                is_sign_language=context.is_sign_language,
                audio_mode=audio_mode,
            )
            self._song_request_key = self._songs_store.ensure_loaded(request, force=force)
            return

        def worker():
            try:
                items, fetched_at, from_cache = fetch_clips(
                    context.api_code,
                    force,
                    fallback_code=context.fallback_code,
                    is_sign_language=context.is_sign_language,
                )
                pub_name = ""
                self._loaded_signal.emit(items, pub_name, fetched_at, from_cache)
            except Exception as exc:
                self._error_signal.emit(str(exc))

        threading.Thread(target=worker, daemon=True).start()

    @Slot(str, list, str, float, bool)
    def _on_song_store_ready(
        self,
        key: str,
        items: list,
        pub_name: str,
        fetched_at: float,
        from_cache: bool,
    ) -> None:
        if key == self._song_request_key:
            self._on_items_loaded(items, pub_name, fetched_at, from_cache)

    @Slot(str, str)
    def _on_song_store_failed(self, key: str, error_msg: str) -> None:
        if key == self._song_request_key:
            self._on_items_error(error_msg)

    @Slot(str, bool)
    def _on_song_store_loading_changed(self, key: str, loading: bool) -> None:
        if key == self._song_request_key and loading:
            self._set_loading(self.tr("Loading songs..."))

    @Slot(str)
    def _on_media_language_changed(self, _code: str) -> None:
        self._update_mode_availability()
        self._load_items(force=True)

    @Slot(list, str, float, bool)
    def _on_items_loaded(self, items: list, pub_name: str, fetched_at: float, from_cache: bool) -> None:
        self.items = list(items or [])
        if self.kind == "songs":
            self.songs = self.items
        else:
            self.clips = self.items
        cache_text = ""
        if from_cache:
            cache_text = self.tr("Updated on {date}").replace("{date}", self._fmt_date(fetched_at))
        self.bridge.update_state(
            subtitle=pub_name,
            loading=False,
            status_text="",
            has_items=bool(self.items),
            count_text=self._count_text(len(self.items)),
            cache_text=cache_text,
            refresh_enabled=True,
        )
        self._filter_items(self._query)
        self._refresh_download_all_state()

    @Slot(str)
    def _on_items_error(self, error_msg: str) -> None:
        text = self.tr("Error loading songs. Check your connection.") if self.kind == "songs" else self.tr("Error loading clips. Check your connection.")
        self.bridge.update_state(
            loading=True,
            status_text=f"{text}\n{error_msg}",
            has_items=False,
            refresh_enabled=True,
        )
        self._refresh_download_all_state()

    def _filter_items(self, query: str) -> None:
        self._query = (query or "").lower().strip()
        if not self._query:
            filtered = self.items
        elif self.kind == "songs":
            filtered = [
                item for item in self.items
                if self._query in item.get("title", "").lower()
                or self._query == str(item.get("number", ""))
            ]
        else:
            filtered = [
                item for item in self.items
                if self._query in item.get("title", "").lower()
            ]
        self.filtered_items = list(filtered)
        if self.kind == "songs":
            self.filtered_songs = self.filtered_items
        else:
            self.filtered_clips = self.filtered_items
        self.model.set_items(self.filtered_items, self._audio_mode)
        self.bridge.update_state(
            has_items=bool(self.items),
            count_text=self._count_text(len(self.filtered_items)),
        )
        self._refresh_download_all_state()

    def _set_mode(self, mode: str) -> None:
        wants_audio = mode == "audio"
        if wants_audio == self._audio_mode:
            return
        if wants_audio and not self.bridge.supports_audio:
            return
        self._audio_mode = wants_audio
        self.bridge.update_state(audio_mode=self._audio_mode)
        self._load_items()
        self._refresh_download_all_state()

    def _on_item_play_by_row(self, row_index: int) -> None:
        item = self.model.item_at(row_index)
        if item:
            self._on_item_play(item)

    def _download_by_row(self, row_index: int) -> None:
        item = self.model.item_at(row_index)
        if not item:
            return
        url = item.get("url", "")
        mgr = MediaCacheManager.instance()
        if url and not mgr.is_cached(url):
            mgr.prefetch(url, priority=True)
            idx = self.model.index(row_index, 0)
            self.model.dataChanged.emit(idx, idx)
        self._refresh_download_all_state()

    def _download_all_current_mode(self) -> None:
        mgr = MediaCacheManager.instance()
        mode = self._download_all_mode()
        batch_id = self._download_all_batch_ids.get(mode, "")
        if batch_id and mgr.batch_is_active(batch_id):
            mgr.cancel_batch(batch_id)
            self._download_all_batch_ids[mode] = ""
            self._refresh_download_all_state()
            return

        urls = self._pending_download_all_urls()
        if not urls:
            self._refresh_download_all_state()
            return

        title = self._download_all_title(mode)
        body = self._download_all_confirm_text(mode, len(urls))
        reply = QMessageBox.question(
            self,
            title,
            body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        batch_id = f"songs-{mode}:{uuid.uuid4().hex}"
        added = mgr.prefetch_many(urls, batch_id)
        if added > 0:
            self._download_all_batch_ids[mode] = batch_id
            self._download_all_error_batches.discard(batch_id)
        self._refresh_download_all_state()

    def _pending_download_all_urls(self) -> list[str]:
        if self.kind != "songs":
            return []
        if self._audio_mode and not self.bridge.supports_audio:
            return []
        mgr = MediaCacheManager.instance()
        urls: list[str] = []
        seen: set[str] = set()
        for item in self._ordered_items(self.items):
            url = str(item.get("url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            if not MediaCacheManager.is_remote(url):
                continue
            if mgr.is_cached(url) or mgr.is_prefetching(url) or mgr.is_queued(url):
                continue
            urls.append(url)
        return urls

    def _download_all_mode(self) -> str:
        return "audio" if self._audio_mode else "video"

    def _download_all_title(self, mode: str | None = None) -> str:
        mode = mode or self._download_all_mode()
        if mode == "audio":
            return self.tr("Download all audio songs")
        return self.tr("Download all video songs")

    def _download_all_confirm_text(self, mode: str, count: int) -> str:
        if mode == "audio":
            return self.tr(
                "Download {count} audio songs for offline playback?"
            ).replace("{count}", str(count))
        return self.tr(
            "Download {count} video songs for offline playback?"
        ).replace("{count}", str(count))

    def _download_all_complete_text(self, mode: str) -> str:
        if mode == "audio":
            return self.tr("All audio songs downloaded")
        return self.tr("All video songs downloaded")

    def _download_all_failed_text(self, mode: str) -> str:
        if mode == "audio":
            return self.tr("Could not finish downloading all audio songs.")
        return self.tr("Could not finish downloading all video songs.")

    def _download_all_mode_for_batch(self, batch_id: str) -> str:
        for mode, active_batch_id in self._download_all_batch_ids.items():
            if active_batch_id == batch_id:
                return mode
        return ""

    def _refresh_download_all_state(self) -> None:
        if not hasattr(self, "bridge"):
            return
        mgr = MediaCacheManager.instance()
        mode = self._download_all_mode()
        batch_id = self._download_all_batch_ids.get(mode, "")
        active = bool(
            batch_id
            and mgr.batch_is_active(batch_id)
        )
        pending_count = len(self._pending_download_all_urls())
        show = self.kind == "songs"
        enabled = show and not self.bridge.loading and (active or pending_count > 0)
        if active:
            tooltip = self.tr("Cancel downloads")
        elif pending_count > 0:
            tooltip = self._download_all_title(mode)
        else:
            tooltip = self._download_all_complete_text(mode)
        self.bridge.update_state(
            show_download_all=show,
            download_all_enabled=enabled,
            download_all_active=active,
            download_all_tooltip=tooltip,
        )

    @Slot(str)
    def _on_cache_state_changed(self, _value: str) -> None:
        self._schedule_download_all_state_refresh()

    @Slot(str, int, int, int, int)
    def _on_prefetch_batch_changed(
        self,
        batch_id: str,
        queued: int,
        active: int,
        _done: int,
        _failed: int,
    ) -> None:
        mode = self._download_all_mode_for_batch(batch_id)
        if not mode:
            return
        if queued + active == 0:
            self._download_all_batch_ids[mode] = ""
        self._schedule_download_all_state_refresh()

    @Slot(str, str)
    def _on_prefetch_batch_error(self, batch_id: str, message: str) -> None:
        mode = self._download_all_mode_for_batch(batch_id)
        if not mode:
            return
        if batch_id in self._download_all_error_batches:
            return
        self._download_all_error_batches.add(batch_id)
        detail = str(message or "").strip()
        text = self._download_all_failed_text(mode)
        if detail:
            text = f"{text}\n\n{detail}"
        QMessageBox.warning(
            self,
            self.tr("Download failed"),
            text,
        )
        self._download_all_batch_ids[mode] = ""
        self._refresh_download_all_state()

    def _schedule_download_all_state_refresh(self) -> None:
        if self._disposed:
            return
        if not self._download_all_refresh_timer.isActive():
            self._download_all_refresh_timer.start()

    def _on_item_play(self, item: dict) -> None:
        url = item.get("url", "")
        if not url:
            return
        title = self._display_title(item)
        playlist = self._build_rotated_playlist(item)
        self.project_video_signal.emit(url, title, playlist, "")

    def _play_all(self) -> None:
        ordered = self._ordered_items(self.items)
        if not ordered:
            return
        playlist = [{"url": item["url"], "title": self._display_title(item)} for item in ordered]
        first = ordered[0]
        self.project_video_signal.emit(first["url"], self._display_title(first), playlist, "next")

    def _play_shuffle(self) -> None:
        ordered = self._ordered_items(self.items)
        if not ordered:
            return
        first = random.choice(ordered)
        start_idx = ordered.index(first)
        rotated = ordered[start_idx:] + ordered[:start_idx]
        playlist = [{"url": item["url"], "title": self._display_title(item)} for item in rotated]
        self.project_video_signal.emit(first["url"], self._display_title(first), playlist, "random")

    def _build_rotated_playlist(self, start_item: dict) -> list[dict]:
        ordered = self._ordered_items(self.items)
        if not ordered:
            return []
        try:
            if self.kind == "songs":
                start_idx = next(
                    i for i, item in enumerate(ordered)
                    if item.get("number") == start_item.get("number")
                )
            else:
                start_idx = next(
                    i for i, item in enumerate(ordered)
                    if item.get("title") == start_item.get("title")
                )
        except StopIteration:
            start_idx = 0
        rotated = ordered[start_idx:] + ordered[:start_idx]
        return [{"url": item["url"], "title": self._display_title(item)} for item in rotated]

    def _ordered_items(self, items: list[dict]) -> list[dict]:
        if self.kind == "songs":
            return sorted(items, key=lambda item: item.get("number", 0))
        return list(items)

    def _display_title(self, item: dict) -> str:
        if self.kind == "songs":
            return f"{item.get('number')}. {item.get('title', '')}"
        return item.get("title", "")

    def _count_text(self, count: int) -> str:
        if self.kind == "songs":
            return self.tr("{count} songs available").replace("{count}", str(count))
        return self.tr("{count} clips available").replace("{count}", str(count))

    def _fmt_date(self, fetched_at: float) -> str:
        py_fmt = _java_to_py_fmt(self.lang.date_format)
        dt = datetime.fromtimestamp(fetched_at)
        escaped_fmt = py_fmt.encode("unicode-escape").decode("utf-8")
        return dt.strftime(escaped_fmt).encode("utf-8").decode("unicode-escape")

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

    def eventFilter(self, obj, event):
        if obj is getattr(self, "qml_widget", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._sync_static_text()
        self._load_items()

    def refresh_language(self) -> None:
        self.retranslateUi()

    def cleanup(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            self.model.cleanup()
        except Exception:
            log_ignored_exception(__name__, "Could not cleanup media library model")
        if self._songs_store is not None:
            try:
                self._songs_store.songs_ready.disconnect(self._on_song_store_ready)
                self._songs_store.songs_failed.disconnect(self._on_song_store_failed)
                self._songs_store.loading_changed.disconnect(
                    self._on_song_store_loading_changed
                )
            except Exception:
                log_ignored_exception(__name__, "Could not disconnect JW songs store signals")
        try:
            svc = getattr(self.lang, "jw_lang_service", None)
            if svc is not None:
                svc.media_language_changed.disconnect(self._on_media_language_changed)
        except Exception:
            log_ignored_exception(__name__, "Could not disconnect media language signal")
        try:
            mgr = MediaCacheManager.instance()
            mgr.cache_changed.disconnect(self._on_cache_state_changed)
            mgr.cache_removed.disconnect(self._on_cache_state_changed)
            mgr.prefetch_queued.disconnect(self._on_cache_state_changed)
            mgr.prefetch_dequeued.disconnect(self._on_cache_state_changed)
            mgr.prefetch_batch_changed.disconnect(self._on_prefetch_batch_changed)
            mgr.prefetch_batch_error.disconnect(self._on_prefetch_batch_error)
        except Exception:
            log_ignored_exception(__name__, "Could not disconnect media cache batch signals")
        if hasattr(self, "qml_widget"):
            try:
                self.qml_widget.removeEventFilter(self)
            except Exception:
                log_ignored_exception(__name__, "Could not remove media library event filter")
            self.qml_widget.setSource(QUrl())

    def deleteLater(self):
        self.cleanup()
        super().deleteLater()
