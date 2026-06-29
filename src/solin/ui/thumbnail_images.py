"""Qt image conversion for thumbnail storage adapters."""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImageReader, QPixmap

from solin.core.foundation.constants import THUMB_JPEG_QUALITY
from solin.core.media.thumbnail_store import ThumbnailStore


def load_thumbnail(store: ThumbnailStore, item_id: str) -> QPixmap | None:
    if not store.exists(item_id):
        return None
    pixmap = QPixmap(str(store.path(item_id)))
    return pixmap if not pixmap.isNull() else None


def image_source_aspect_ratio(
    *,
    path: str = "",
    pixmap: QPixmap | None = None,
) -> float:
    """Return authoritative source aspect without decoding a full image."""

    if pixmap is not None and not pixmap.isNull() and pixmap.height() > 0:
        return pixmap.width() / pixmap.height()
    if not path:
        return 0.0
    reader = QImageReader(path)
    size = reader.size()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        return 0.0
    return size.width() / size.height()


def pixmap_to_jpeg_bytes(pixmap: QPixmap) -> bytes | None:
    if pixmap is None or pixmap.isNull():
        return None
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return None
    try:
        if not pixmap.save(buffer, "JPEG", THUMB_JPEG_QUALITY):
            return None
        data = bytes(buffer.data())
        return data or None
    finally:
        buffer.close()


def save_thumbnail(store: ThumbnailStore, item_id: str, pixmap: QPixmap) -> bool:
    data = pixmap_to_jpeg_bytes(pixmap)
    if not data:
        return False
    store.save_bytes(item_id, data)
    return True
