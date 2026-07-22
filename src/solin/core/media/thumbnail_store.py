"""Atomic filesystem storage for generated thumbnails."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from solin.core.media.download_storage import make_persistent_temp_path, safe_remove


class ThumbnailStore:
    """Stores JPEG thumbnails under one explicit cache root."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path(self, item_id: str) -> Path:
        if not item_id or Path(item_id).name != item_id:
            raise ValueError(f"Invalid thumbnail item id: {item_id!r}")
        return self._root / f"{item_id}.jpg"

    def source_signature_path(self, item_id: str) -> Path:
        return thumbnail_source_signature_path(self.path(item_id))

    @staticmethod
    def storage_id_from_filename(filename: str) -> str | None:
        """Parse image and provenance filenames owned by this store."""

        if filename.endswith(".jpg.source"):
            return filename.removesuffix(".jpg.source") or None
        if filename.endswith(".jpg"):
            return filename.removesuffix(".jpg") or None
        return None

    def exists(self, item_id: str) -> bool:
        try:
            return self.path(item_id).is_file()
        except (OSError, ValueError):
            return False

    def save_bytes(
        self,
        item_id: str,
        data: bytes,
        *,
        source_signature: str = "",
    ) -> Path:
        destination = self.path(item_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_path = make_persistent_temp_path(destination)
        try:
            with open(write_path, "wb") as handle:
                handle.write(data)
            os.replace(write_path, destination)
            _publish_source_signature(destination, source_signature)
        except OSError:
            safe_remove(write_path)
            raise
        return destination

    def copy_from(
        self,
        item_id: str,
        source: str | Path,
        *,
        source_signature: str = "",
    ) -> Path:
        destination = self.path(item_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_path = make_persistent_temp_path(destination)
        try:
            shutil.copyfile(source, write_path)
            os.replace(write_path, destination)
            _publish_source_signature(destination, source_signature)
        except OSError:
            safe_remove(write_path)
            raise
        return destination

    def bind_source_signature(self, item_id: str, source_signature: str) -> Path:
        """Atomically bind an existing thumbnail to one local source revision."""

        destination = self.path(item_id)
        if not destination.is_file():
            raise FileNotFoundError(destination)
        _publish_source_signature(destination, source_signature)
        return self.source_signature_path(item_id)


def thumbnail_source_signature_path(thumbnail_path: str | Path) -> Path:
    """Return the sidecar that binds a thumbnail to one source revision."""

    path = Path(thumbnail_path)
    return path.with_suffix(f"{path.suffix}.source")


def read_thumbnail_source_signature(thumbnail_path: str | Path | None) -> str:
    """Read thumbnail provenance; call only from a filesystem worker."""

    if thumbnail_path is None:
        return ""
    try:
        return thumbnail_source_signature_path(thumbnail_path).read_text(
            encoding="utf-8"
        ).strip()
    except (OSError, UnicodeError):
        return ""


def _publish_source_signature(
    thumbnail_path: Path,
    source_signature: str,
) -> None:
    signature_path = thumbnail_source_signature_path(thumbnail_path)
    if not source_signature:
        try:
            signature_path.unlink()
        except FileNotFoundError:
            pass
        return
    write_path = make_persistent_temp_path(signature_path)
    try:
        with open(write_path, "w", encoding="utf-8") as handle:
            handle.write(source_signature)
        os.replace(write_path, signature_path)
    except OSError:
        safe_remove(write_path)
        # Never leave an old signature beside newly published image bytes.
        safe_remove(signature_path)
        raise


__all__ = [
    "ThumbnailStore",
    "read_thumbnail_source_signature",
    "thumbnail_source_signature_path",
]
