from __future__ import annotations

from PySide6.QtGui import QPixmap

from ...core.foundation.constants import THUMB_JPEG_QUALITY
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.playlists.thumbnails import playlist_thumb_path


def _load_thumb_from_disk(item_id: str) -> QPixmap | None:
    path = playlist_thumb_path(item_id)
    if path.exists():
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            return pixmap
    return None


def _save_thumb_to_disk(item_id: str, pixmap: QPixmap) -> None:
    if pixmap is None or pixmap.isNull():
        return
    try:
        path = playlist_thumb_path(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(path), "JPEG", THUMB_JPEG_QUALITY)
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
