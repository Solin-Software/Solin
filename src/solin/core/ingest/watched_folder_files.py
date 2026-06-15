"""Filesystem operations for watched-folder media workflows."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from solin.core.media.download_storage import safe_remove


class WatchedFolderFileStore:
    """Performs watched-folder copy, rename, delete, and contained removal."""

    def copy_file_into_folder(self, source: str | Path, folder: str | Path) -> str:
        source_path = Path(source)
        folder_path = Path(folder)
        destination = self._unique_child_path(folder_path, source_path.name)
        temp_path = destination.with_name(f"{destination.name}.solin_tmp")
        try:
            shutil.copy2(source_path, temp_path)
            os.replace(temp_path, destination)
        except OSError:
            safe_remove(temp_path)
            raise
        return os.fspath(destination)

    def rename_folder(self, folder: str | Path, new_name: str) -> str:
        if Path(new_name).name != new_name or not new_name.strip():
            raise ValueError(f"Invalid folder name: {new_name!r}")
        folder_path = Path(folder)
        destination = folder_path.parent / new_name
        folder_path.rename(destination)
        return os.fspath(destination)

    def delete_folder(self, folder: str | Path) -> None:
        shutil.rmtree(folder, ignore_errors=False)

    def remove_file_inside(self, file_path: str | Path, folder: str | Path) -> bool:
        if not file_path:
            return False
        path = Path(file_path)
        if not path.exists():
            return True
        if not path.is_file() or not self.is_inside(path, folder):
            return False
        path.unlink()
        return True

    @staticmethod
    def is_inside(path: str | Path, folder: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(Path(folder).resolve())
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _unique_child_path(folder: Path, filename: str) -> Path:
        destination = folder / filename
        if not destination.exists():
            return destination
        stem = destination.stem
        suffix = destination.suffix
        counter = 1
        while destination.exists():
            destination = folder / f"{stem} ({counter}){suffix}"
            counter += 1
        return destination
