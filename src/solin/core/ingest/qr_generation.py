"""Generation-aware Qt adapter for Wi-Fi QR rendering."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal

from solin.core.foundation.qt_threads import stop_owned_qthread
from solin.core.ingest.qr_codes import generate_qr_png

log = logging.getLogger(__name__)


class QrGenerationThread(QThread):
    completed = Signal(int, bytes)
    failed = Signal(int)

    def __init__(self, generation: int, url: str, parent=None) -> None:
        super().__init__(parent)
        self._generation = generation
        self._url = url

    def run(self) -> None:
        try:
            png_data = generate_qr_png(self._url)
        except Exception:  # noqa: BLE001 - qrcode/Pillow codec boundary
            log.exception("Could not generate Wi-Fi QR code")
            if not self.isInterruptionRequested():
                self.failed.emit(self._generation)
            return
        if not self.isInterruptionRequested():
            self.completed.emit(self._generation, png_data)


class QrGenerationSession(QObject):
    """Own QR workers and ignore results invalidated by cancellation."""

    ready = Signal(bytes)
    failed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._generation = 0
        self._threads: list[QrGenerationThread] = []
        self._closed = False

    def start(self, url: str) -> bool:
        if self._closed or not url:
            return False
        self.cancel()
        thread = QrGenerationThread(self._generation, url, self)
        self._threads.append(thread)
        thread.completed.connect(self._on_completed)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(lambda worker=thread: self._discard(worker))
        thread.finished.connect(thread.deleteLater)
        thread.start()
        return True

    def cancel(self) -> None:
        if self._closed:
            return
        self._generation += 1
        for thread in list(self._threads):
            thread.requestInterruption()

    def close(self) -> None:
        if self._closed:
            return
        self.cancel()
        self._closed = True
        for thread in list(self._threads):
            stopped = stop_owned_qthread(
                thread,
                wait_ms=2_000,
                logger=log,
                label="Wi-Fi QR generation",
            )
            if stopped:
                self._discard(thread)

    def _on_completed(self, generation: int, png_data: bytes) -> None:
        if not self._closed and generation == self._generation:
            self.ready.emit(png_data)

    def _on_failed(self, generation: int) -> None:
        if not self._closed and generation == self._generation:
            self.failed.emit()

    def _discard(self, thread: QrGenerationThread) -> None:
        if thread in self._threads:
            self._threads.remove(thread)


class QrGenerationSessionFactory:
    def create(self, *, parent: QObject | None = None) -> QrGenerationSession:
        return QrGenerationSession(parent)
