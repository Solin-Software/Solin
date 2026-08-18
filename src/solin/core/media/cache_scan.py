"""Qt worker session for scanning cached media outside presentation widgets."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .cache_listing import scan_cached_media_items


class _CacheScanWorker(QObject):
    batch_ready = Signal(list)
    results_ready = Signal(list)
    failed = Signal(str)
    completed = Signal()

    def __init__(self, media_cache_dir: str | Path) -> None:
        super().__init__()
        self._media_cache_dir = media_cache_dir
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            items = scan_cached_media_items(
                self._media_cache_dir,
                is_cancelled=lambda: self._cancelled,
                on_batch=self.batch_ready.emit,
            )
        except OSError as exc:
            self.failed.emit(str(exc))
        else:
            self.results_ready.emit(items)
        finally:
            self.completed.emit()


class CacheScanSession(QObject):
    """Own one cancellable cache scan thread."""

    batch_ready = Signal(list)
    results_ready = Signal(list)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, media_cache_dir: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._worker = _CacheScanWorker(media_cache_dir)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._worker.batch_ready.connect(self.batch_ready.emit)
        self._worker.results_ready.connect(self.results_ready.emit)
        self._worker.failed.connect(self.failed.emit)
        self._worker.completed.connect(self._worker.deleteLater)
        self._worker.completed.connect(self._thread.quit)
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


class CacheScanSessionFactory:
    """Create cache scan sessions with explicit Qt ownership."""

    def create(
        self,
        media_cache_dir: str | Path,
        *,
        parent=None,
    ) -> CacheScanSession:
        return CacheScanSession(media_cache_dir, parent)
