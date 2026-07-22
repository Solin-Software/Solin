"""Latest-wins background persistence for immutable application snapshots."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from solin.core.foundation.resource_keys import ResourceClaim
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.foundation.thread_workers import (
    ThreadedWorkerPool,
    WorkerHandle,
)


log = logging.getLogger(__name__)
SnapshotWriter = Callable[[], None]
_RETRY_DELAYS_MS = (1_000, 2_000, 5_000, 10_000, 30_000)


@dataclass(frozen=True, slots=True)
class _WriteRequest:
    key: str
    generation: int
    writer: SnapshotWriter
    conflict_key: str | ResourceClaim


class SnapshotWriteCoordinator(QObject):
    """Coalesce writes by storage key and execute them serially off Qt."""

    writeCompleted = Signal(str, int)
    writeFailed = Signal(str, int, str)
    _workerCompleted = Signal(object, object)

    def __init__(
        self,
        *,
        debounce_ms: int = 75,
        resource_lanes: ResourceLaneRegistry | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._debounce_ms = max(0, debounce_ms)
        self._resource_lanes = resource_lanes or ResourceLaneRegistry()
        self._worker_pool = ThreadedWorkerPool()
        self._pending: dict[str, _WriteRequest] = {}
        self._generations: dict[str, int] = {}
        self._failed: dict[str, _WriteRequest] = {}
        self._retry_attempts: dict[str, int] = {}
        self._inflight: tuple[_WriteRequest, WorkerHandle] | None = None
        self._closing = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._drain)
        self._workerCompleted.connect(self._on_worker_completed)

    def request(
        self,
        key: str,
        writer: SnapshotWriter,
        *,
        conflict_key: str | ResourceClaim | None = None,
    ) -> int:
        if self._closing:
            return 0
        if not key or not callable(writer):
            raise ValueError("Snapshot writes require a key and writer")
        generation = self._generations.get(key, 0) + 1
        self._generations[key] = generation
        self._failed.pop(key, None)
        self._retry_attempts.pop(key, None)
        self._pending[key] = _WriteRequest(
            key,
            generation,
            writer,
            conflict_key or key,
        )
        if self._inflight is None:
            self._timer.start(self._debounce_ms)
        return generation

    def retry(self, key: str) -> bool:
        if self._closing or key in self._pending:
            return False
        request = self._failed.pop(key, None)
        if request is None or self._generations.get(key) != request.generation:
            return False
        self._pending[key] = request
        if self._inflight is None:
            self._timer.start(0)
        return True

    def shutdown(self, *, wait_ms: int = 1_500) -> tuple[str, ...]:
        if self._closing:
            return ()
        self._closing = True
        self._timer.stop()
        deadline = time.monotonic() + max(0, wait_ms) / 1_000
        inflight = self._inflight
        if inflight is not None:
            inflight[1].join(timeout=max(0.0, deadline - time.monotonic()))
            if inflight[1].is_alive():
                log.warning("Snapshot writer did not stop before shutdown: %s", inflight[0].key)
        pending = tuple(self._pending.values()) + tuple(self._failed.values())
        self._pending.clear()
        self._failed.clear()
        self._worker_pool.shutdown(timeout=max(0.0, deadline - time.monotonic()))
        unfinished = []
        if inflight is not None and inflight[1].is_alive():
            unfinished.append(inflight[0].key)
        unfinished.extend(request.key for request in pending)
        return tuple(dict.fromkeys(unfinished))

    @Slot()
    def _drain(self) -> None:
        if self._closing or self._inflight is not None or not self._pending:
            return
        key = next(iter(self._pending))
        request = self._pending.pop(key)

        def run() -> None:
            try:
                self._run_request(request)
            except BaseException as exc:  # noqa: BLE001 - worker-to-Qt boundary
                error: object = exc
            else:
                error = None
            try:
                self._workerCompleted.emit(request, error)
            except RuntimeError:
                pass

        worker = self._worker_pool.submit("snapshot-writer", run)
        if worker is None:
            self._pending[request.key] = request
            return
        self._inflight = (request, worker)

    @Slot(object, object)
    def _on_worker_completed(
        self,
        request: object,
        error: object,
    ) -> None:
        if not isinstance(request, _WriteRequest):
            return
        self._inflight = None
        if self._closing:
            return
        if self._generations.get(request.key) == request.generation:
            if error is None:
                self._failed.pop(request.key, None)
                self._retry_attempts.pop(request.key, None)
                self.writeCompleted.emit(request.key, request.generation)
            else:
                self._failed[request.key] = request
                attempt = self._retry_attempts.get(request.key, 0)
                self._retry_attempts[request.key] = attempt + 1
                self.writeFailed.emit(request.key, request.generation, str(error))
                delay = _RETRY_DELAYS_MS[min(attempt, len(_RETRY_DELAYS_MS) - 1)]
                QTimer.singleShot(
                    delay,
                    lambda key=request.key, generation=request.generation: (
                        self.retry(key)
                        if self._generations.get(key) == generation
                        else False
                    ),
                )
        if not self._closing and self._pending:
            self._timer.start(0)

    def _run_request(self, request: _WriteRequest) -> None:
        self._resource_lanes.run(request.conflict_key, request.writer)


__all__ = ["SnapshotWriteCoordinator"]
