from __future__ import annotations

import os

from PySide6.QtGui import QPixmap

from ...core.foundation import paths as _paths
from ...core.foundation.constants import THUMB_JPEG_QUALITY
from ...core.foundation.exception_logging import log_ignored_exception


def _thumb_cache_path(item_id: str) -> str:
    return os.path.join(_paths.THUMB_CACHE_DIR, f"{item_id}.jpg")


def _load_thumb_from_disk(item_id: str) -> QPixmap | None:
    path = _thumb_cache_path(item_id)
    if os.path.exists(path):
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            return pixmap
    return None


def _save_thumb_to_disk(item_id: str, pixmap: QPixmap) -> None:
    if pixmap is None or pixmap.isNull():
        return
    try:
        os.makedirs(_paths.THUMB_CACHE_DIR, exist_ok=True)
        pixmap.save(_thumb_cache_path(item_id), "JPEG", THUMB_JPEG_QUALITY)
    except Exception:  # noqa: BLE001 - Qt image codec boundary
        log_ignored_exception(__name__, "Could not save playlist thumbnail")


def _thumb_to_bytes(pixmap: QPixmap) -> bytes | None:
    if pixmap is None or pixmap.isNull():
        return None
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "JPEG", THUMB_JPEG_QUALITY)
    data = bytes(buffer.data())
    buffer.close()
    return data if data else None
