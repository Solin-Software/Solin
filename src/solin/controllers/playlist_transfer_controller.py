"""Qt orchestration for responsive, serialized playlist transfers."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from concurrent.futures import CancelledError
from dataclasses import dataclass
from time import monotonic
from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QT_TRANSLATE_NOOP,
    QThread,
    Signal,
    Slot,
)

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.playlists.native_types import NativePlaylistTransferCancelled
from solin.ui.dialogs.playlist_transfer import PlaylistTransferDialog

log = logging.getLogger(__name__)

_TR_CONTEXT = "PlaylistTransfer"
_CANCELLED_SOURCE = QT_TRANSLATE_NOOP("PlaylistTransfer", "Operation cancelled")
_PROCESSING_SOURCE = QT_TRANSLATE_NOOP("PlaylistTransfer", "Processing…")


def _tr(text: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, text)


@dataclass(frozen=True, slots=True)
class PlaylistTransferProgress:
    stage: str
    detail: str = ""
    completed: int = 0
    total: int = 0
    can_cancel: bool = True


@dataclass(frozen=True, slots=True)
class PlaylistTransferJob:
    key: str
    title: str
    initial_stage: str
    runner: Callable[[Callable[[object], None], CancellationFlag], object]
    completed: Callable[[object], None]
    succeeded_message: str = ""
    queue_if_busy: bool = False


class PlaylistTransferCancelled(RuntimeError):
    """Raised by transfer services after cooperative cancellation."""


class _PlaylistTransferThread(QThread):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, job: PlaylistTransferJob, cancellation: CancellationFlag, parent=None):
        super().__init__(parent)
        self._job = job
        self._cancellation = cancellation
        self._last_progress_at = 0.0
        self._last_stage = ""

    def run(self) -> None:
        try:
            result = self._job.runner(self._report_progress, self._cancellation)
            if self._cancellation.is_set():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
        except (CancelledError, PlaylistTransferCancelled, NativePlaylistTransferCancelled):
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - worker-to-UI error boundary
            log.exception("Playlist transfer failed")
            self.failed.emit(str(exc) or type(exc).__name__)

    def _report_progress(self, value: object) -> None:
        """Bound queued-signal traffic while preserving stage and terminal updates."""
        now = monotonic()
        stage = str(getattr(value, "stage", "") or "")
        completed = int(getattr(value, "completed", 0) or 0)
        total = int(getattr(value, "total", 0) or 0)
        terminal = total > 0 and completed >= total
        if (
            stage != self._last_stage
            or terminal
            or now - self._last_progress_at >= 0.05
        ):
            self._last_stage = stage
            self._last_progress_at = now
            self.progress.emit(value)


class PlaylistTransferController(QObject):
    """Runs exactly one transfer at a time and owns its modal presentation."""

    def __init__(self, *, parent, notifications) -> None:
        super().__init__(parent)
        self._parent = parent
        self._notifications = notifications
        self._dialog = PlaylistTransferDialog(parent)
        self._dialog.cancel_requested.connect(self.cancel_active)
        self._active_job: PlaylistTransferJob | None = None
        self._active_thread: _PlaylistTransferThread | None = None
        self._cancellation: CancellationFlag | None = None
        self._queue: deque[PlaylistTransferJob] = deque()
        self._queued_keys: set[str] = set()
        self._start_next_after_finish = False
        self._closing = False

    @property
    def is_busy(self) -> bool:
        return self._active_thread is not None

    def submit(self, job: PlaylistTransferJob) -> bool:
        if self._closing:
            return False
        if self.is_busy:
            if job.queue_if_busy and job.key not in self._queued_keys:
                self._queue.append(job)
                self._queued_keys.add(job.key)
                return True
            return False
        self._start(job)
        return True

    def cancel_active(self) -> None:
        if self._cancellation is None or self._cancellation.is_set():
            return
        self._cancellation.set()
        self._dialog.show_cancelling()

    def shutdown(self, wait_ms: int = 10_000) -> None:
        self._closing = True
        self._queue.clear()
        self._queued_keys.clear()
        self.cancel_active()
        thread = self._active_thread
        if thread is not None and thread.isRunning():
            thread.wait(wait_ms)
        if thread is not None and thread.isRunning():
            log.warning("Playlist transfer did not stop within %d ms", wait_ms)
            thread.setParent(None)
            thread.finished.connect(thread.deleteLater)
        self._active_thread = None
        self._active_job = None
        self._cancellation = None

    def _start(self, job: PlaylistTransferJob) -> None:
        cancellation = CancellationFlag()
        thread = _PlaylistTransferThread(job, cancellation, self)
        self._active_job = job
        self._active_thread = thread
        self._cancellation = cancellation
        thread.progress.connect(self._on_progress)
        thread.succeeded.connect(self._on_succeeded)
        thread.failed.connect(self._on_failed)
        thread.cancelled.connect(self._on_cancelled)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._dialog.begin(job.title, job.initial_stage)
        thread.start()

    @Slot(object)
    def _on_progress(self, value: object) -> None:
        progress = self._coerce_progress(value)
        self._dialog.set_progress(
            stage=progress.stage,
            detail=progress.detail,
            completed=progress.completed,
            total=progress.total,
            can_cancel=progress.can_cancel,
        )

    @Slot(object)
    def _on_succeeded(self, result: object) -> None:
        job = self._active_job
        if job is None:
            return
        try:
            job.completed(result)
        except Exception as exc:  # noqa: BLE001 - UI commit boundary
            log.exception("Playlist transfer commit failed")
            self._dialog.show_error(str(exc) or type(exc).__name__)
            self._start_next_after_finish = False
            self._queue.clear()
            self._queued_keys.clear()
            return
        self._dialog.finish()
        if job.succeeded_message:
            self._notifications.success(job.succeeded_message)
        self._start_next_after_finish = True

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self._dialog.show_error(message)
        self._start_next_after_finish = False
        self._queue.clear()
        self._queued_keys.clear()

    @Slot()
    def _on_cancelled(self) -> None:
        self._dialog.finish()
        self._notifications.information(_tr(_CANCELLED_SOURCE))
        self._start_next_after_finish = True

    @Slot()
    def _on_thread_finished(self) -> None:
        start_next = self._start_next_after_finish
        self._start_next_after_finish = False
        job = self._active_job
        if job is not None:
            self._queued_keys.discard(job.key)
        self._active_thread = None
        self._active_job = None
        self._cancellation = None
        if start_next and self._queue and not self._closing:
            next_job = self._queue.popleft()
            self._queued_keys.discard(next_job.key)
            self._start(next_job)

    @staticmethod
    def _coerce_progress(value: object) -> PlaylistTransferProgress:
        if isinstance(value, PlaylistTransferProgress):
            return value
        return PlaylistTransferProgress(
            stage=str(getattr(value, "stage", "") or _tr(_PROCESSING_SOURCE)),
            detail=str(getattr(value, "detail", "") or ""),
            completed=int(getattr(value, "completed", 0) or 0),
            total=int(getattr(value, "total", 0) or 0),
            can_cancel=bool(getattr(value, "can_cancel", True)),
        )


__all__ = [
    "PlaylistTransferCancelled",
    "PlaylistTransferController",
    "PlaylistTransferJob",
    "PlaylistTransferProgress",
]
