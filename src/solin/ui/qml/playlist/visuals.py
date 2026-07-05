from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPixmap
from PySide6.QtQuick import QQuickImageProvider

from solin.styles.icons import (
    ICON_ARROW_LEFT,
    ICON_CHEVRON_DOWN,
    ICON_CHEVRON_UP,
    ICON_CLOUD_DOWNLOAD,
    ICON_EDIT,
    ICON_EXPORT,
    ICON_FOLDER_LINK,
    ICON_GRIP,
    ICON_IMAGE,
    ICON_MARKER,
    ICON_MEDIA_RANGE,
    ICON_MORE_VERT,
    ICON_MUSIC,
    ICON_PALETTE,
    ICON_PLAY,
    ICON_PLAY_ALL,
    ICON_PLAY_SHUFFLE,
    ICON_PLUS,
    ICON_SAVE_PLAYLIST,
    ICON_SECTION,
    ICON_TRASH,
    ICON_VIDEO,
)
from solin.styles.theme import QML_THEME
from solin.ui.qml.svg_icons import SvgIconProvider

UNSECTIONED_CARD_BACKGROUND = "transparent"


def playlist_placeholder_color() -> str:
    return str(QML_THEME["mediaPlaceholder"])


def playlist_media_badge(media_type: str) -> str:
    if media_type == "image":
        return QCoreApplication.translate("PlaylistPanel", "Image")
    if media_type == "audio":
        return QCoreApplication.translate("PlaylistPanel", "Audio")
    return QCoreApplication.translate("PlaylistPanel", "Video")


# ── Thumbnail provider ────────────────────────────────────────────────────────

PLAYLIST_THUMBNAIL_WIDTH, PLAYLIST_THUMBNAIL_HEIGHT = 208, 120


def round_playlist_pixmap(pixmap: QPixmap, radius: int) -> QPixmap:
    """Return a copy with rounded corners."""
    if pixmap is None or pixmap.isNull() or pixmap.width() <= 0 or pixmap.height() <= 0:
        fallback = QPixmap(PLAYLIST_THUMBNAIL_WIDTH, PLAYLIST_THUMBNAIL_HEIGHT)
        fallback.fill(QColor(playlist_placeholder_color()))
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
        parts = id_str.split("/")
        item_id = parts[0]
        preserve_full_aspect = "fit" in parts[1:]
        w = (
            requestedSize.width()
            if requestedSize.width() > 0
            else PLAYLIST_THUMBNAIL_WIDTH
        )
        h = (
            requestedSize.height()
            if requestedSize.height() > 0
            else PLAYLIST_THUMBNAIL_HEIGHT
        )
        pixmap = self._cache.get(item_id)
        if (not pixmap or pixmap.isNull()) and self._disk_loader_cb:
            pixmap = self._disk_loader_cb(item_id)
            if pixmap and not pixmap.isNull():
                self._cache[item_id] = pixmap

        if pixmap and not pixmap.isNull():
            if preserve_full_aspect:
                return pixmap.scaled(
                    w,
                    h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            scaled = pixmap.scaled(
                w, h,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = max(0, (scaled.width()  - w) // 2)
            y = max(0, (scaled.height() - h) // 2)
            cropped = scaled.copy(x, y, w, h)
            return round_playlist_pixmap(cropped, 7)
        # Placeholder
        ph = QPixmap(w, h)
        ph.fill(QColor(playlist_placeholder_color()))
        return round_playlist_pixmap(ph, 7)


# ── Icon provider ─────────────────────────────────────────────────────────────

PLAYLIST_ICON_SVGS = {
    "grip":           ICON_GRIP,
    "more":           ICON_MORE_VERT,
    "play_all":       ICON_PLAY_ALL,
    "play":           ICON_PLAY,
    "shuffle":        ICON_PLAY_SHUFFLE,
    "back":           ICON_ARROW_LEFT,
    "plus":           ICON_PLUS,
    "export":         ICON_EXPORT,
    "folder_link":    ICON_FOLDER_LINK,
    "save":           ICON_SAVE_PLAYLIST,
    "cloud":          ICON_CLOUD_DOWNLOAD,
    "section":        ICON_SECTION,
    "marker":         ICON_MARKER,
    "media_range":    ICON_MEDIA_RANGE,
    "chevron_down":   ICON_CHEVRON_DOWN,
    "chevron_up":     ICON_CHEVRON_UP,
    "edit":           ICON_EDIT,
    "trash":          ICON_TRASH,
    "palette":        ICON_PALETTE,
    "media_image":    ICON_IMAGE,
    "media_audio":    ICON_MUSIC,
    "media_video":    ICON_VIDEO,
}


class PlaylistIconProvider(SvgIconProvider):
    """Renders SVG icons for QML.

    QML requests: ``image://playlisticons/<name>/<size>/<color_hex>``
    Example: ``image://playlisticons/grip/14/484f58``
    """

    def __init__(self):
        super().__init__(PLAYLIST_ICON_SVGS, default_icon="grip")
