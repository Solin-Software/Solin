from __future__ import annotations

import os

from PySide6.QtCore import QByteArray, QCoreApplication, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtQuick import QQuickImageProvider
from PySide6.QtSvg import QSvgRenderer

from ...core.foundation import paths as _paths
from ...styles.icons import (
    ICON_ARROW_LEFT,
    ICON_CHEVRON_DOWN,
    ICON_CHEVRON_UP,
    ICON_CLOUD_DOWNLOAD,
    ICON_EDIT,
    ICON_EXPORT,
    ICON_GRIP,
    ICON_IMAGE,
    ICON_MARKER,
    ICON_MORE_VERT,
    ICON_MUSIC,
    ICON_PALETTE,
    ICON_PLAY_ALL,
    ICON_PLAY_SHUFFLE,
    ICON_PLUS,
    ICON_SAVE_PLAYLIST,
    ICON_SECTION,
    ICON_TRASH,
    ICON_VIDEO,
)

_PLACEHOLDER_COLOR = "#0a0e14"
_UNSECTIONED_BG    = "transparent"
_SUBSECTION_BG     = "#0f1318"
_SUBSECTION_BORDER = "#1a1f28"


def _format_duration(ticks: int) -> str:
    if not ticks or ticks <= 0:
        return ""
    total_seconds = ticks // 10_000_000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _media_badge(media_type: str) -> str:
    if media_type == "image":
        return QCoreApplication.translate("PlaylistPanel", "Image")
    if media_type == "audio":
        return QCoreApplication.translate("PlaylistPanel", "Audio")
    return QCoreApplication.translate("PlaylistPanel", "Video")


# ── Thumbnail provider ────────────────────────────────────────────────────────

_THUMB_W, _THUMB_H = 208, 120


def _thumb_cache_path(item_id: str) -> str:
    return os.path.join(_paths.THUMB_CACHE_DIR, f"{item_id}.jpg")


def _round_pixmap(pixmap: QPixmap, radius: int) -> QPixmap:
    """Return a copy with rounded corners."""
    if pixmap is None or pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
        fallback = QPixmap(_THUMB_W, _THUMB_H)
        fallback.fill(QColor(_PLACEHOLDER_COLOR))
        pixmap = fallback
    result = QPixmap(pixmap.size())
    result.fill(Qt.GlobalColor.transparent)
    p = QPainter(result)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pixmap.width(), pixmap.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pixmap)
    p.end()
    return result


class PlaylistThumbnailProvider(QQuickImageProvider):
    """Serves thumbnails keyed by item ID.

    QML requests: ``image://playlistthumbs/<item_id>/<version>``
    The *version* suffix is for cache-busting; we parse only the *item_id*.
    """

    def __init__(self, thumb_cache: dict, disk_loader_cb=None):
        super().__init__(QQuickImageProvider.ImageType.Pixmap)
        self._cache = thumb_cache
        self._disk_loader_cb = disk_loader_cb

    def requestPixmap(self, id_str: str, size, requestedSize):
        item_id = id_str.split("/")[0]
        w = requestedSize.width() if requestedSize.width() > 0 else _THUMB_W
        h = requestedSize.height() if requestedSize.height() > 0 else _THUMB_H
        pixmap = self._cache.get(item_id)
        if (not pixmap or pixmap.isNull()) and self._disk_loader_cb:
            pixmap = self._disk_loader_cb(item_id)
            if pixmap and not pixmap.isNull():
                self._cache[item_id] = pixmap

        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                w, h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = max(0, (scaled.width()  - w) // 2)
            y = max(0, (scaled.height() - h) // 2)
            cropped = scaled.copy(x, y, w, h)
            return _round_pixmap(cropped, 7)
        # Placeholder
        ph = QPixmap(w, h)
        ph.fill(QColor(_PLACEHOLDER_COLOR))
        return _round_pixmap(ph, 7)


# ── Icon provider ─────────────────────────────────────────────────────────────

_ICON_MAP = {
    "grip":           ICON_GRIP,
    "more":           ICON_MORE_VERT,
    "play_all":       ICON_PLAY_ALL,
    "shuffle":        ICON_PLAY_SHUFFLE,
    "back":           ICON_ARROW_LEFT,
    "plus":           ICON_PLUS,
    "export":         ICON_EXPORT,
    "save":           ICON_SAVE_PLAYLIST,
    "cloud":          ICON_CLOUD_DOWNLOAD,
    "section":        ICON_SECTION,
    "marker":         ICON_MARKER,
    "chevron_down":   ICON_CHEVRON_DOWN,
    "chevron_up":     ICON_CHEVRON_UP,
    "edit":           ICON_EDIT,
    "trash":          ICON_TRASH,
    "palette":        ICON_PALETTE,
    "media_image":    ICON_IMAGE,
    "media_audio":    ICON_MUSIC,
    "media_video":    ICON_VIDEO,
}


class PlaylistIconProvider(QQuickImageProvider):
    """Renders SVG icons for QML.

    QML requests: ``image://playlisticons/<name>/<size>/<color_hex>``
    Example: ``image://playlisticons/grip/14/484f58``
    """

    def __init__(self):
        super().__init__(QQuickImageProvider.ImageType.Pixmap)

    def requestPixmap(self, id_str: str, size, requestedSize):
        parts = id_str.split("/")
        name = parts[0] if len(parts) > 0 else "grip"
        px_size = int(parts[1]) if len(parts) > 1 else 16
        color = f"#{parts[2]}" if len(parts) > 2 else "#8b949e"

        svg_str = _ICON_MAP.get(name, ICON_GRIP)
        svg = svg_str.replace("currentColor", color)
        data = QByteArray(svg.encode("utf-8"))
        renderer = QSvgRenderer(data)
        pix = QPixmap(px_size, px_size)
        pix.fill(Qt.GlobalColor.transparent)
        if renderer.isValid():
            p = QPainter(pix)
            renderer.render(p)
            p.end()
        return pix
