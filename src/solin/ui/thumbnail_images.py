"""Qt image conversion for thumbnail storage adapters."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage, QImageReader, QPixmap

from solin.core.foundation.constants import THUMB_JPEG_QUALITY
from solin.core.media.thumbnail_store import ThumbnailStore

if TYPE_CHECKING:
    from solin.core.media.media_extract import MediaExtractImage


def media_extract_image_to_qimage(image: MediaExtractImage) -> QImage:
    """Copy packed native BGRA pixels into an independently owned Qt image.

    Invalid geometry or incomplete buffers are rejected before Qt can read them.
    Format_ARGB32 maps to native BGRA bytes on the supported little-endian hosts.
    """
    width, height, stride = image.width, image.height, image.stride
    if (
        type(width) is not int
        or type(height) is not int
        or type(stride) is not int
        or not 0 < width < 2**31
        or not 0 < height < 2**31
        or not 0 < stride < 2**31
        or stride != width * 4
        or not isinstance(image.data, bytes)
        or len(image.data) != stride * height
    ):
        raise ValueError("Invalid extracted BGRA image geometry or buffer")
    result = QImage(image.data, width, height, stride, QImage.Format.Format_ARGB32).copy()
    if result.isNull():
        raise ValueError("Could not copy extracted BGRA pixels into a Qt image")
    return result


def load_thumbnail(store: ThumbnailStore, item_id: str) -> QPixmap | None:
    if not store.exists(item_id):
        return None
    return load_thumbnail_path(os.fspath(store.path(item_id)))


def load_thumbnail_path(path: str) -> QPixmap | None:
    """Load one existing local thumbnail at the Qt presentation boundary."""

    normalized_path = str(path or "").strip()
    if not normalized_path or not os.path.isfile(normalized_path):
        return None
    pixmap = QPixmap(normalized_path)
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


def image_to_jpeg_bytes(image: QImage) -> bytes | None:
    if image is None or image.isNull():
        return None
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return None
    try:
        if not image.save(buffer, "JPEG", THUMB_JPEG_QUALITY):
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
