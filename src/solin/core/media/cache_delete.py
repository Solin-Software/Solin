"""Cancellable worker session for deleting cached media outside the GUI thread."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .cache import remove_cached_entry


class _CacheDeletionWorker(QObject):
    completed = Signal(list, list)
    finished = Signal()

    def __init__(self, media_cache_dir: str | Path, paths: list[str]) -> None:
        super().__init__()
        self._media_cache_dir = media_cache_dir
        self._paths = list(dict.fromkeys(paths))
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        deleted: list[str] = []
        failed: list[tuple[str, str]] = []
        try:
            for path in self._paths:
                if self._cancelled:
                    break
                try:
                    if remove_cached_entry(self._media_cache_dir, path):
                        deleted.append(path)
                except (OSError, ValueError) as exc:
                    failed.append((path, str(exc)))
            self.completed.emit(deleted, failed)
        finally:
            self.finished.emit()


class CacheDeletionSession(QObject):
    completed = Signal(list, list)
    finished = Signal()

    def __init__(
        self,
        media_cache_dir: str | Path,
        paths: list[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._worker = _CacheDeletionWorker(media_cache_dir, paths)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._worker.completed.connect(self.completed.emit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.finished.connect(self._thread.quit)
        self._thread.started.connect(self._worker.run)
        self._thread.finished.connect(self.finished.emit)
        self._thread.finished.connect(self._thread.deleteLater)

    def start(self) -> None:
        self._thread.start()

    def cancel(self, *, wait_ms: int = 2_000) -> None:
        try:
            self._worker.cancel()
        except RuntimeError:
            pass
        try:
            self._thread.quit()
            self._thread.wait(wait_ms)
            if self._thread.isRunning():
                self._thread.setParent(None)
                self._thread.finished.connect(self._thread.deleteLater)
        except RuntimeError:
            pass


class CacheDeletionSessionFactory:
    def create(
        self,
        media_cache_dir: str | Path,
        paths: list[str],
        *,
        parent=None,
    ) -> CacheDeletionSession:
        return CacheDeletionSession(media_cache_dir, paths, parent)
