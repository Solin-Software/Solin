"""Shared Qt coordinator for bounded, conflict-aware media I/O."""

from __future__ import annotations

from collections import deque
from concurrent.futures import CancelledError
import logging
from time import monotonic

from PySide6.QtCore import QObject, QThread, Signal, Slot

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.media.operations import (
    MediaOperationCancelled,
    MediaOperationProgress,
    MediaOperationRecord,
    MediaOperationSpec,
    MediaOperationState,
)


log = logging.getLogger(__name__)
_PROGRESS_INTERVAL_SECONDS = 0.05


class _MediaOperationThread(QThread):
    progress = Signal(str, object)
    succeeded = Signal(str, object)
    failed = Signal(str, str)
    cancelled = Signal(str)

    def __init__(
        self,
        spec: MediaOperationSpec,
        cancellation: CancellationFlag,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._spec = spec
        self._cancellation = cancellation
        self._last_progress_at = 0.0
        self._last_state: MediaOperationState | None = None
        self._last_stage = ""

    def run(self) -> None:
        operation_id = self._spec.operation_id
        try:
            result = self._spec.runner(self._report_progress, self._cancellation)
            if self._cancellation.is_set():
                self.cancelled.emit(operation_id)
            else:
                self.succeeded.emit(operation_id, result)
        except (CancelledError, MediaOperationCancelled):
            self.cancelled.emit(operation_id)
        except Exception as exc:  # noqa: BLE001 - worker-to-GUI exception boundary
            log.exception("Media operation %s failed", operation_id)
            self.failed.emit(operation_id, str(exc) or type(exc).__name__)

    def _report_progress(self, progress: MediaOperationProgress) -> None:
        if not isinstance(progress, MediaOperationProgress):
            raise TypeError("Media operation runners must report MediaOperationProgress")
        now = monotonic()
        terminal = progress.total > 0 and progress.completed >= progress.total
        if (
            progress.state != self._last_state
            or progress.stage != self._last_stage
            or terminal
            or now - self._last_progress_at >= _PROGRESS_INTERVAL_SECONDS
        ):
            self._last_state = progress.state
            self._last_stage = progress.stage
            self._last_progress_at = now
            self.progress.emit(self._spec.operation_id, progress)


class MediaOperationCoordinator(QObject):
    """Run independent operations concurrently and serialize conflicting work."""

    operationChanged = Signal(object)
    operationFinished = Signal(str, str)

    def __init__(self, *, max_workers: int = 2, parent=None) -> None:
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        super().__init__(parent)
        self._max_workers = max_workers
        self._queue: deque[str] = deque()
        self._specs: dict[str, MediaOperationSpec] = {}
        self._records: dict[str, MediaOperationRecord] = {}
        self._threads: dict[str, _MediaOperationThread] = {}
        self._thread_ids: dict[_MediaOperationThread, str] = {}
        self._cancellations: dict[str, CancellationFlag] = {}
        self._active_conflicts: set[str] = set()
        self._closing = False

    @property
    def active_count(self) -> int:
        return len(self._threads)

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    def submit(self, spec: MediaOperationSpec) -> bool:
        self._assert_gui_thread()
        if self._closing or spec.operation_id in self._specs:
            return False
        self._specs[spec.operation_id] = spec
        self._queue.append(spec.operation_id)
        self._publish(
            MediaOperationRecord(
                operation_id=spec.operation_id,
                scope_id=spec.scope_id,
                operation_type=spec.operation_type,
                presentation=spec.presentation,
                state=MediaOperationState.QUEUED,
                stage=spec.initial_stage,
                cancellable=True,
                retryable=False,
            )
        )
        self._drain()
        return True

    @Slot(str)
    def cancel(self, operation_id: str) -> None:
        self._assert_gui_thread()
        if operation_id in self._queue:
            self._queue.remove(operation_id)
            self._finish_cancelled(operation_id)
            self._drain()
            return
        cancellation = self._cancellations.get(operation_id)
        if cancellation is None or cancellation.is_set():
            return
        cancellation.set()
        record = self._records[operation_id]
        self._publish(
            MediaOperationRecord(
                operation_id=record.operation_id,
                scope_id=record.scope_id,
                operation_type=record.operation_type,
                presentation=record.presentation,
                state=record.state,
                stage=record.stage,
                detail=record.detail,
                completed=record.completed,
                total=record.total,
                cancellable=False,
                retryable=False,
            )
        )

    @Slot(str, result=bool)
    def retry(self, operation_id: str) -> bool:
        self._assert_gui_thread()
        if self._closing or operation_id in self._threads or operation_id in self._queue:
            return False
        spec = self._specs.get(operation_id)
        record = self._records.get(operation_id)
        if (
            spec is None
            or record is None
            or record.state != MediaOperationState.FAILED
            or not spec.retryable
        ):
            return False
        self._queue.append(operation_id)
        self._publish(
            MediaOperationRecord(
                operation_id=operation_id,
                scope_id=spec.scope_id,
                operation_type=spec.operation_type,
                presentation=spec.presentation,
                state=MediaOperationState.QUEUED,
                stage=spec.initial_stage,
                cancellable=True,
            )
        )
        self._drain()
        return True

    def record(self, operation_id: str) -> MediaOperationRecord | None:
        return self._records.get(operation_id)

    def forget(self, operation_id: str) -> bool:
        self._assert_gui_thread()
        if operation_id in self._threads or operation_id in self._queue:
            return False
        record = self._records.get(operation_id)
        if record is None or record.state not in {
            MediaOperationState.READY,
            MediaOperationState.FAILED,
            MediaOperationState.CANCELLED,
        }:
            return False
        self._records.pop(operation_id, None)
        self._specs.pop(operation_id, None)
        return True

    def shutdown(self, wait_ms: int = 8_000) -> tuple[str, ...]:
        self._assert_gui_thread()
        self._closing = True
        for operation_id in tuple(self._queue):
            self._queue.remove(operation_id)
            self._finish_cancelled(operation_id)
        for cancellation in self._cancellations.values():
            cancellation.set()
        deadline = monotonic() + max(0, wait_ms) / 1_000
        for operation_id, thread in tuple(self._threads.items()):
            remaining_ms = max(0, int((deadline - monotonic()) * 1_000))
            if remaining_ms:
                thread.wait(remaining_ms)
            if thread.isRunning():
                log.warning("Media operation %s did not stop before shutdown", operation_id)
                thread.setParent(None)
                thread.finished.connect(thread.deleteLater)
        return tuple(
            operation_id
            for operation_id, thread in self._threads.items()
            if thread.isRunning()
        )

    def _drain(self) -> None:
        while len(self._threads) < self._max_workers:
            operation_id = next(
                (
                    queued_id
                    for queued_id in self._queue
                    if self._specs[queued_id].conflict_key not in self._active_conflicts
                ),
                None,
            )
            if operation_id is None:
                return
            self._queue.remove(operation_id)
            self._start(operation_id)

    def _start(self, operation_id: str) -> None:
        spec = self._specs[operation_id]
        cancellation = CancellationFlag()
        thread = _MediaOperationThread(spec, cancellation, self)
        self._threads[operation_id] = thread
        self._thread_ids[thread] = operation_id
        self._cancellations[operation_id] = cancellation
        self._active_conflicts.add(spec.conflict_key)
        thread.progress.connect(self._on_progress)
        thread.succeeded.connect(self._on_succeeded)
        thread.failed.connect(self._on_failed)
        thread.cancelled.connect(self._on_cancelled)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._publish(
            MediaOperationRecord(
                operation_id=operation_id,
                scope_id=spec.scope_id,
                operation_type=spec.operation_type,
                presentation=spec.presentation,
                state=MediaOperationState.PREPARING,
                stage=spec.initial_stage,
                cancellable=True,
            )
        )
        thread.start()

    @Slot(str, object)
    def _on_progress(self, operation_id: str, value: object) -> None:
        if not isinstance(value, MediaOperationProgress):
            return
        spec = self._specs.get(operation_id)
        if spec is None:
            return
        self._publish(
            MediaOperationRecord(
                operation_id=operation_id,
                scope_id=spec.scope_id,
                operation_type=spec.operation_type,
                presentation=spec.presentation,
                state=value.state,
                stage=value.stage,
                detail=value.detail,
                completed=value.completed,
                total=value.total,
                cancellable=value.cancellable,
            )
        )

    @Slot(str, object)
    def _on_succeeded(self, operation_id: str, result: object) -> None:
        spec = self._specs.get(operation_id)
        if spec is None:
            return
        try:
            spec.commit(result)
        except Exception as exc:  # noqa: BLE001 - GUI commit exception boundary
            log.exception("Media operation %s commit failed", operation_id)
            self._finish_failed(operation_id, str(exc) or type(exc).__name__)
            return
        previous = self._records[operation_id]
        self._publish(
            MediaOperationRecord(
                operation_id=operation_id,
                scope_id=spec.scope_id,
                operation_type=spec.operation_type,
                presentation=spec.presentation,
                state=MediaOperationState.READY,
                stage=previous.stage,
                detail=previous.detail,
                completed=previous.total or previous.completed,
                total=previous.total,
            )
        )
        self.operationFinished.emit(operation_id, MediaOperationState.READY.value)

    @Slot(str, str)
    def _on_failed(self, operation_id: str, message: str) -> None:
        self._finish_failed(operation_id, message)

    @Slot(str)
    def _on_cancelled(self, operation_id: str) -> None:
        self._finish_cancelled(operation_id)

    @Slot()
    def _on_thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, _MediaOperationThread):
            return
        operation_id = self._thread_ids.pop(thread, "")
        if not operation_id:
            return
        spec = self._specs.get(operation_id)
        self._threads.pop(operation_id, None)
        self._cancellations.pop(operation_id, None)
        if spec is not None:
            self._active_conflicts.discard(spec.conflict_key)
        if not self._closing:
            self._drain()

    def _finish_failed(self, operation_id: str, message: str) -> None:
        spec = self._specs.get(operation_id)
        if spec is None:
            return
        self._publish(
            MediaOperationRecord(
                operation_id=operation_id,
                scope_id=spec.scope_id,
                operation_type=spec.operation_type,
                presentation=spec.presentation,
                state=MediaOperationState.FAILED,
                stage=self._records[operation_id].stage,
                retryable=spec.retryable,
                error=message,
            )
        )
        if spec.failed is not None:
            spec.failed(message, spec.retryable)
        self.operationFinished.emit(operation_id, MediaOperationState.FAILED.value)

    def _finish_cancelled(self, operation_id: str) -> None:
        spec = self._specs.get(operation_id)
        if spec is None:
            return
        self._publish(
            MediaOperationRecord(
                operation_id=operation_id,
                scope_id=spec.scope_id,
                operation_type=spec.operation_type,
                presentation=spec.presentation,
                state=MediaOperationState.CANCELLED,
            )
        )
        if spec.cancelled is not None:
            spec.cancelled()
        self.operationFinished.emit(operation_id, MediaOperationState.CANCELLED.value)

    def _publish(self, record: MediaOperationRecord) -> None:
        self._records[record.operation_id] = record
        self.operationChanged.emit(record)

    def _assert_gui_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("MediaOperationCoordinator must be used from its Qt thread")
