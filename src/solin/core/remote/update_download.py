"""Streaming downloads verified before a package becomes available to the UI."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QThread, Signal, Slot

from solin.core.network.http import HttpError, stream_get
from solin.core.remote.update_policy import UpdateInfo


class _Transfer(QObject):
    progress = Signal(int)
    succeeded = Signal(str)
    failed = Signal(str)
    done = Signal()

    def __init__(self, info: UpdateInfo, directory: Path, cancelled: Event) -> None:
        super().__init__()
        self.info = info
        self.directory = directory
        self.cancelled = cancelled

    @Slot()
    def run(self) -> None:
        partial: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix="download-", suffix=".part", dir=self.directory
            )
            partial = Path(name)
            digest = hashlib.sha256()
            received = 0
            deadline = time.monotonic() + 3600
            with os.fdopen(descriptor, "wb") as output:
                with stream_get(
                    self.info.url, timeout=(5.0, 5.0), headers={"Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code != 200:
                        raise HttpError("Unexpected package response")
                    if response.content_length and response.content_length != self.info.asset.size:
                        raise HttpError("Package size does not match release manifest")
                    for chunk in response.iter_bytes():
                        if self.cancelled.is_set() or time.monotonic() >= deadline:
                            raise HttpError("Download cancelled or timed out")
                        received += len(chunk)
                        if received > self.info.asset.size:
                            raise HttpError("Package exceeds manifest size")
                        output.write(chunk)
                        digest.update(chunk)
                        self.progress.emit(min(99, received * 100 // self.info.asset.size))
                output.flush()
                os.fsync(output.fileno())
            if self.cancelled.is_set():
                raise HttpError("Download cancelled")
            if received != self.info.asset.size or digest.hexdigest() != self.info.asset.sha256:
                raise HttpError("Package integrity verification failed")
            destination = self.directory / self.info.asset.filename
            partial.replace(destination)
            partial = None
            if self.info.asset.kind == "appimage":
                destination.chmod(destination.stat().st_mode | 0o100)
            self.succeeded.emit(str(destination))
        except Exception as exc:  # noqa: BLE001 - Thread boundary also reports disk and permission failures.
            self.failed.emit(str(exc))
        finally:
            if partial is not None:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass
            self.done.emit()


class UpdateDownloadWorker(QObject):
    progress = Signal(int)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, info: UpdateInfo, parent: QObject | None = None, *, directory: Path) -> None:
        super().__init__(parent)
        self._cancelled = Event()
        self._thread: QThread | None = None
        self._info = info
        self._directory = directory
        self._transfer: _Transfer | None = None
        self._aborted = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._transfer = _Transfer(self._info, self._directory, self._cancelled)
        self._thread = QThread(self)
        self._transfer.moveToThread(self._thread)
        self._thread.started.connect(self._transfer.run)
        self._transfer.progress.connect(self.progress)
        self._transfer.succeeded.connect(self._success)
        self._transfer.failed.connect(self._failure)
        self._transfer.done.connect(self._transfer.deleteLater)
        self._transfer.done.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)
        self._thread.start()

    def abort(self) -> None:
        self._aborted = True
        self._cancelled.set()
        # Parent dialogs may be destroyed while a socket reaches its short timeout.
        self.setParent(None)

    def _success(self, path: str) -> None:
        if not self._aborted:
            self.finished.emit(path)

    def _failure(self, message: str) -> None:
        if not self._aborted:
            self.failed.emit(message)

    @Slot()
    def _cleanup(self) -> None:
        if self._thread:
            self._thread.deleteLater()
        self._thread = None
        self._transfer = None
        self.deleteLater()
