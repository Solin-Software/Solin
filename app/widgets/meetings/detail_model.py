from __future__ import annotations

import os

from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QModelIndex,
    QObject,
    Qt,
    Slot,
)
from PySide6.QtGui import QPixmap
from PySide6.QtQuick import QQuickImageProvider

from ...core.i18n.strings import (
    tr_offline_download,
    tr_offline_downloading,
    tr_offline_downloading_progress,
)
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.meetings.publications import JwpubService, MeetingMedia
from ...styles.icons import ICON_ARROW_LEFT, ICON_CLOUD_DOWNLOAD
from ..media_info_extractor import MediaInfoQueue
from .visuals import (
    _AMBER,
    _MUTED,
    _SEC_ICONS,
    _rounded_pixmap,
    _svg_pixmap,
)


def _tr_media_row(source: str) -> str:
    return QCoreApplication.translate("_MediaRow", source)

# ── Media detail view helper functions ─────────────────────────────────────────

def _create_default_row():
    return {
        "kind": "",
        "label": "",
        "sectionColor": "",
        "sectionBg": "",
        "iconSource": "",
        "title": "",
        "badge": "",
        "badgeBg": "",
        "badgeFg": "",
        "isSong": False,
        "isVideo": False,
        "thumbSource": "",
        "itemCount": 0,
        "isIndented": False,
        "mediaRef": -1,
        "topPad": 0,
        "resolvedUrl": "",
        "cloudVisible": False,
        "cloudDownloading": False,
        "cloudProgress": -1.0,
        "cloudTooltip": "",
        "req_id": "",
        "media_item": None,
        "queue_idx": None,
        "thumb_version": 0,
    }


def _get_badge_info(item: "MeetingMedia", tr_func=None):
    is_vid = "video" in item.mime_type
    is_song = item.is_song
    if is_song:
        return _tr_media_row("Song"), "#2d2000", _AMBER
    elif is_vid:
        return _tr_media_row("Video"), "#0d2035", "#79c0ff"
    else:
        return _tr_media_row("Image"), "#0d200d", "#56d364"


# ── QQuickImageProvider for thumbnails and icons ──────────────────────────────

class MeetingThumbnailProvider(QQuickImageProvider):
    def __init__(self):
        super().__init__(QQuickImageProvider.ImageType.Pixmap)
        self.thumbs = {}  # maps string queue_idx -> QPixmap

    def requestPixmap(self, id: str, size, requestedSize):
        if id == "icon:arrow_left":
            return _svg_pixmap(ICON_ARROW_LEFT, 16, _MUTED)

        if id == "icon:cloud_download":
            return _svg_pixmap(ICON_CLOUD_DOWNLOAD, 14, "#8b949e")

        if id.startswith("secicon/"):
            parts = id.split("/")
            if len(parts) >= 3:
                sec = parts[1]
                colorHex = parts[2]
                if not colorHex.startswith("#"):
                    colorHex = "#" + colorHex
                if sec in _SEC_ICONS:
                    svg_str, sz = _SEC_ICONS[sec]
                    return _svg_pixmap(svg_str, sz, colorHex)

        if id.startswith("thumb/"):
            thumb_idx = id.split("/")[-1]
            if "?" in thumb_idx:
                thumb_idx = thumb_idx.split("?")[0]
            pix = self.thumbs.get(thumb_idx)
            if pix and not pix.isNull():
                return pix

        fallback = QPixmap(1, 1)
        fallback.fill(Qt.GlobalColor.transparent)
        return fallback


# ── QML ListModel ─────────────────────────────────────────────────────────────

class MeetingDetailModel(QAbstractListModel):
    KindRole = Qt.ItemDataRole.UserRole + 1
    LabelRole = Qt.ItemDataRole.UserRole + 2
    SectionColorRole = Qt.ItemDataRole.UserRole + 3
    SectionBgRole = Qt.ItemDataRole.UserRole + 4
    IconSourceRole = Qt.ItemDataRole.UserRole + 5
    TitleRole = Qt.ItemDataRole.UserRole + 6
    BadgeRole = Qt.ItemDataRole.UserRole + 7
    BadgeBgRole = Qt.ItemDataRole.UserRole + 8
    BadgeFgRole = Qt.ItemDataRole.UserRole + 9
    IsSongRole = Qt.ItemDataRole.UserRole + 10
    IsVideoRole = Qt.ItemDataRole.UserRole + 11
    ThumbSourceRole = Qt.ItemDataRole.UserRole + 12
    ItemCountRole = Qt.ItemDataRole.UserRole + 13
    IsIndentedRole = Qt.ItemDataRole.UserRole + 14
    MediaRefRole = Qt.ItemDataRole.UserRole + 15
    TopPadRole = Qt.ItemDataRole.UserRole + 16
    ResolvedUrlRole = Qt.ItemDataRole.UserRole + 17
    CloudVisibleRole = Qt.ItemDataRole.UserRole + 18
    CloudDownloadingRole = Qt.ItemDataRole.UserRole + 19
    CloudTooltipRole = Qt.ItemDataRole.UserRole + 20
    CloudProgressRole = Qt.ItemDataRole.UserRole + 21

    def __init__(self, service: JwpubService, provider: MeetingThumbnailProvider, view_id: str = "", parent=None):
        super().__init__(parent)
        self._svc = service
        self._provider = provider
        self._view_id = view_id
        self._rows = []
        self._queue_idx_to_row_index = {}
        
        self._queue = MediaInfoQueue(self)
        self._queue.info_ready.connect(self._on_thumb_ready)
        
        self._svc.video_resolved.connect(self._on_video_resolved)
        
        from ...core.media.cache import MediaCacheManager
        MediaCacheManager.instance().cache_changed.connect(self._on_cache_changed)
        MediaCacheManager.instance().cache_removed.connect(self._on_cache_removed)
        MediaCacheManager.instance().prefetch_progress.connect(self._on_prefetch_progress)
        MediaCacheManager.instance().prefetch_error.connect(self._on_prefetch_error)

    def roleNames(self):
        return {
            self.KindRole: b"kind",
            self.LabelRole: b"label",
            self.SectionColorRole: b"sectionColor",
            self.SectionBgRole: b"sectionBg",
            self.IconSourceRole: b"iconSource",
            self.TitleRole: b"title",
            self.BadgeRole: b"badge",
            self.BadgeBgRole: b"badgeBg",
            self.BadgeFgRole: b"badgeFg",
            self.IsSongRole: b"isSong",
            self.IsVideoRole: b"isVideo",
            self.ThumbSourceRole: b"thumbSource",
            self.ItemCountRole: b"itemCount",
            self.IsIndentedRole: b"isIndented",
            self.MediaRefRole: b"mediaRef",
            self.TopPadRole: b"topPad",
            self.ResolvedUrlRole: b"resolvedUrl",
            self.CloudVisibleRole: b"cloudVisible",
            self.CloudDownloadingRole: b"cloudDownloading",
            self.CloudTooltipRole: b"cloudTooltip",
            self.CloudProgressRole: b"cloudProgress",
        }

    def rowCount(self, parent=None):
        parent = parent or QModelIndex()
        if parent.isValid():
            return 0
        return len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row_idx = index.row()
        if row_idx >= len(self._rows):
            return None
        row = self._rows[row_idx]
        
        role_map = {
            self.KindRole: "kind",
            self.LabelRole: "label",
            self.SectionColorRole: "sectionColor",
            self.SectionBgRole: "sectionBg",
            self.IconSourceRole: "iconSource",
            self.TitleRole: "title",
            self.BadgeRole: "badge",
            self.BadgeBgRole: "badgeBg",
            self.BadgeFgRole: "badgeFg",
            self.IsSongRole: "isSong",
            self.IsVideoRole: "isVideo",
            self.ThumbSourceRole: "thumbSource",
            self.ItemCountRole: "itemCount",
            self.IsIndentedRole: "isIndented",
            self.MediaRefRole: "mediaRef",
            self.TopPadRole: "topPad",
            self.ResolvedUrlRole: "resolvedUrl",
            self.CloudVisibleRole: "cloudVisible",
            self.CloudDownloadingRole: "cloudDownloading",
            self.CloudTooltipRole: "cloudTooltip",
            self.CloudProgressRole: "cloudProgress",
        }
        
        field = role_map.get(role)
        if field:
            return row.get(field)
        return None

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = rows
        self._queue_idx_to_row_index.clear()
        
        for i, row in enumerate(rows):
            queue_idx = row.get("queue_idx")
            if queue_idx is not None:
                self._queue_idx_to_row_index[queue_idx] = i
                self._start_row_requests(i)
                    
        self.endResetModel()

    def _start_row_requests(self, row_idx: int):
        row = self._rows[row_idx]
        item = row.get("media_item")
        queue_idx = row.get("queue_idx")
        if not item or queue_idx is None:
            return
            
        is_vid = "video" in item.mime_type
        is_song = item.is_song
        is_img = "image" in item.mime_type

        if is_img and item.file_path and os.path.exists(item.file_path):
            pix = QPixmap(item.file_path)
            if not pix.isNull():
                rounded = _rounded_pixmap(pix, 80, 52, 6)
                self._provider.thumbs[str(queue_idx)] = rounded
                row["thumbSource"] = f"image://mthumbs/thumb/{self._view_id}/{queue_idx}"
            return

        if is_vid or is_song:
            if item.file_path and os.path.exists(item.file_path):
                row["resolvedUrl"] = item.file_path
                self._refresh_row_cloud_state(row_idx)
                media_type = "audio" if is_song else "video"
                self._queue.request(queue_idx, item.file_path, media_type)
            elif item.key_symbol or item.meps_doc_id:
                req_id = f"row:{row_idx}:{item.meps_doc_id or item.key_symbol}"
                row["req_id"] = req_id
                self._svc.resolve_video_async(req_id, item)

    @Slot(str, str, str, str)
    def _on_video_resolved(self, request_id: str, url: str, title: str, thumb_url: str):
        if not request_id.startswith("row:"):
            return
        try:
            parts = request_id.split(":")
            row_index = int(parts[1])
        except (ValueError, IndexError):
            return

        if row_index >= len(self._rows):
            return
        row = self._rows[row_index]
        if row.get("req_id") != request_id:
            return

        if url:
            row["resolvedUrl"] = url
            item = row.get("media_item")
            if title and item and not (item.label or item.caption):
                t = title[:88] + ("…" if len(title) > 88 else "")
                row["title"] = t
            
            self._refresh_row_cloud_state(row_index)

        queue_idx = row.get("queue_idx")
        if queue_idx is not None:
            if thumb_url:
                self._queue.request(queue_idx, thumb_url, "image")
            elif url:
                media_type = "audio" if row.get("isSong") else "video"
                self._queue.request(queue_idx, url, media_type)

        idx = self.index(row_index, 0)
        self.dataChanged.emit(idx, idx)

    @Slot(int, object, str)
    def _on_thumb_ready(self, queue_idx: int, pixmap: QPixmap, title: str):
        row_index = self._queue_idx_to_row_index.get(queue_idx)
        if row_index is None or row_index >= len(self._rows):
            return
        
        row = self._rows[row_index]
        if pixmap and not pixmap.isNull():
            rounded = _rounded_pixmap(pixmap, 80, 52, 6)
            self._provider.thumbs[str(queue_idx)] = rounded
            v = row.get("thumb_version", 0) + 1
            row["thumb_version"] = v
            row["thumbSource"] = f"image://mthumbs/thumb/{self._view_id}/{queue_idx}?v={v}"
            
        item = row.get("media_item")
        if title and item and not (item.label or item.caption):
            t = title[:88] + ("…" if len(title) > 88 else "")
            row["title"] = t
            
        idx = self.index(row_index, 0)
        self.dataChanged.emit(idx, idx)

    def _refresh_row_cloud_state(self, row_index: int):
        from ...core.media.cache import MediaCacheManager
        row = self._rows[row_index]
        url = row.get("resolvedUrl")
        if not url:
            row["cloudVisible"] = False
            row["cloudProgress"] = -1.0
            return
        
        if not (row.get("isVideo") or row.get("isSong")):
            row["cloudVisible"] = False
            row["cloudProgress"] = -1.0
            return

        cached = MediaCacheManager.is_cached_static(url) if hasattr(MediaCacheManager, 'is_cached_static') else MediaCacheManager.instance().is_cached(url)
        prefetching = MediaCacheManager.instance().is_prefetching(url)
        
        if cached:
            row["cloudVisible"] = False
            row["cloudDownloading"] = False
            row["cloudProgress"] = 1.0
        elif prefetching:
            row["cloudVisible"] = True
            row["cloudDownloading"] = True
            row["cloudProgress"] = row.get("cloudProgress", -1.0)
            row["cloudTooltip"] = tr_offline_downloading()
        else:
            row["cloudVisible"] = True
            row["cloudDownloading"] = False
            row["cloudProgress"] = -1.0
            row["cloudTooltip"] = tr_offline_download()

    @Slot(str)
    def _on_cache_changed(self, url: str):
        if not url:
            return
        for i, row in enumerate(self._rows):
            if row.get("resolvedUrl") == url:
                self._refresh_row_cloud_state(i)
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx)

    @Slot(str)
    def _on_cache_removed(self, path: str):
        if not path:
            return
        from ...core.media.cache import MediaCacheManager, cached_path_for
        removed_path = os.path.normcase(os.path.abspath(path))
        for i, row in enumerate(self._rows):
            url = row.get("resolvedUrl", "")
            if not MediaCacheManager.is_remote(url):
                continue
            cached_path = os.path.normcase(os.path.abspath(cached_path_for(url)))
            if cached_path == removed_path:
                self._refresh_row_cloud_state(i)
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx)

    @Slot(str, int, int)
    def _on_prefetch_progress(self, url: str, downloaded: int, total: int):
        if not url or total <= 0:
            return
        pct = int(downloaded * 100 / total)
        for i, row in enumerate(self._rows):
            if row.get("resolvedUrl") == url:
                row["cloudProgress"] = pct / 100.0
                row["cloudTooltip"] = tr_offline_downloading_progress(pct)
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx)

    @Slot(str, str)
    def _on_prefetch_error(self, url: str, _message: str):
        if not url:
            return
        for i, row in enumerate(self._rows):
            if row.get("resolvedUrl") == url:
                self._refresh_row_cloud_state(i)
                idx = self.index(i, 0)
                self.dataChanged.emit(idx, idx)

    def cleanup(self):
        try:
            self._queue.info_ready.disconnect(self._on_thumb_ready)
        except Exception:
            log_ignored_exception(__name__, "Could not disconnect meeting thumbnail queue")
        try:
            self._svc.video_resolved.disconnect(self._on_video_resolved)
        except Exception:
            log_ignored_exception(__name__, "Could not disconnect meeting video resolver")
        try:
            from ...core.media.cache import MediaCacheManager
            MediaCacheManager.instance().cache_changed.disconnect(self._on_cache_changed)
            MediaCacheManager.instance().cache_removed.disconnect(self._on_cache_removed)
            MediaCacheManager.instance().prefetch_progress.disconnect(self._on_prefetch_progress)
            MediaCacheManager.instance().prefetch_error.disconnect(self._on_prefetch_error)
        except Exception:
            log_ignored_exception(__name__, "Could not disconnect meeting cache signals")


# ── QML Controller Bridge ─────────────────────────────────────────────────────

class MeetingDetailBridge(QObject):
    def __init__(self, model: MeetingDetailModel, parent=None):
        super().__init__(parent)
        self._model = model

    @Slot()
    def backClicked(self):
        self.parent().back_requested.emit()

    @Slot(int)
    def playItem(self, row_index: int):
        if row_index < len(self._model._rows):
            row = self._model._rows[row_index]
            media = row.get("media_item")
            if media:
                self.parent().play_requested.emit(media)

    @Slot(int)
    def downloadItem(self, row_index: int):
        if row_index < len(self._model._rows):
            row = self._model._rows[row_index]
            url = row.get("resolvedUrl")
            if url:
                from ...core.media.cache import MediaCacheManager
                mgr = MediaCacheManager.instance()
                if not mgr.is_cached(url) and not mgr.is_prefetching(url):
                    mgr.prefetch(url, priority=True)
                    self._model._refresh_row_cloud_state(row_index)
                    idx = self._model.index(row_index, 0)
                    self._model.dataChanged.emit(idx, idx)

    @Slot()
    def pointerEntered(self):
        parent = self.parent()
        if hasattr(parent, "begin_qml_pointer_cursor"):
            parent.begin_qml_pointer_cursor()

    @Slot()
    def pointerExited(self):
        parent = self.parent()
        if hasattr(parent, "end_qml_pointer_cursor"):
            parent.end_qml_pointer_cursor()


