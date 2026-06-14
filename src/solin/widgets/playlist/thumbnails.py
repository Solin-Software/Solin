from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QPixmap

from ...core.foundation.constants import THUMB_JPEG_QUALITY
from ...core.foundation.exception_logging import log_ignored_exception
from ...core.playlists.thumbnails import playlist_thumb_path


def load_thumb_from_disk(
    item_id: str,
    thumb_cache_dir: str | Path,
) -> QPixmap | None:
    path = playlist_thumb_path(item_id, thumb_cache_dir=thumb_cache_dir)
    if path.exists():
        pixmap = QPixmap(str(path))
        if not pixmap.isNull():
            return pixmap
    return None


def save_thumb_to_disk(
    item_id: str,
    pixmap: QPixmap,
    thumb_cache_dir: str | Path,
) -> None:
    if pixmap is None or pixmap.isNull():
        return
    try:
        path = playlist_thumb_path(item_id, thumb_cache_dir=thumb_cache_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(path), "JPEG", THUMB_JPEG_QUALITY)
    except Exception:  # noqa: BLE001 - Qt image codec boundary
        log_ignored_exception(__name__, "Could not save playlist thumbnail")


def thumb_to_bytes(pixmap: QPixmap) -> bytes | None:
    if pixmap is None or pixmap.isNull():
        return None
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "JPEG", THUMB_JPEG_QUALITY)
    data = bytes(buffer.data())
    buffer.close()
    return data if data else None
