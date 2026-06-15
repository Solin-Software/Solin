"""Qt worker session for scanning cached media outside presentation widgets."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .cache_listing import scan_cached_media_items


class _CacheScanWorker(QObject):
    results_ready = Signal(list)

    def __init__(self, media_cache_dir: str | Path) -> None:
        super().__init__()
        self._media_cache_dir = media_cache_dir
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        self.results_ready.emit(
            scan_cached_media_items(
                self._media_cache_dir,
                is_cancelled=lambda: self._cancelled,
            )
        )


class CacheScanSession(QObject):
    """Own one cancellable cache scan thread."""

    results_ready = Signal(list)
    finished = Signal()

    def __init__(self, media_cache_dir: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._worker = _CacheScanWorker(media_cache_dir)
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._worker.results_ready.connect(self.results_ready.emit)
        self._worker.results_ready.connect(self._worker.deleteLater)
        self._worker.results_ready.connect(self._thread.quit)
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
