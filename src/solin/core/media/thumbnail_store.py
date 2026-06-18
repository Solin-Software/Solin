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

    def exists(self, item_id: str) -> bool:
        try:
            return self.path(item_id).is_file()
        except (OSError, ValueError):
            return False

    def save_bytes(self, item_id: str, data: bytes) -> Path:
        destination = self.path(item_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_path = make_persistent_temp_path(destination)
        try:
            with open(write_path, "wb") as handle:
                handle.write(data)
            os.replace(write_path, destination)
        except OSError:
            safe_remove(write_path)
            raise
        return destination

    def copy_from(self, item_id: str, source: str | Path) -> Path:
        destination = self.path(item_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_path = make_persistent_temp_path(destination)
        try:
            shutil.copyfile(source, write_path)
            os.replace(write_path, destination)
        except OSError:
            safe_remove(write_path)
            raise
        return destination
