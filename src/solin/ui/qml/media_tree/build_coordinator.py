"""Latest-wins background construction for immutable media-tree snapshots."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty, SimpleQueue

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from solin.core.foundation.thread_workers import WorkerPool
from solin.ui.qml.media_tree.snapshot import MediaTreeSnapshot


@dataclass(frozen=True, slots=True)
class SnapshotBuildRequest:
    generation: int
    revision: int
    tree_id: str
    build: Callable[[], MediaTreeSnapshot]
    structure_revision: int = 0


@dataclass(frozen=True, slots=True)
class SnapshotBuildResult:
    request: SnapshotBuildRequest
    snapshot: MediaTreeSnapshot | None = None
    error: str = ""


class SnapshotBuildCoordinator(QObject):
    """Run one build at a time while coalescing queued work to the latest input."""

    snapshotReady = Signal(object)
    buildFailed = Signal(object)
    idle = Signal()

    def __init__(
        self,
        worker_pool: WorkerPool,
        *,
        worker_name: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._worker_pool = worker_pool
        self._worker_name = worker_name
        self._active = False
        self._pending: SnapshotBuildRequest | None = None
        self._closed = False
        self._results: SimpleQueue[SnapshotBuildResult] = SimpleQueue()
        self._result_timer = QTimer(self)
        self._result_timer.setInterval(4)
        self._result_timer.timeout.connect(self._drain_results)

    @property
    def active(self) -> bool:
        return self._active

    def request(self, request: SnapshotBuildRequest) -> bool:
        if self._closed:
            return False
        if self._active:
            self._pending = request
            return True
        return self._start(request)

    def close(self) -> None:
        self._closed = True
        self._pending = None
        self._result_timer.stop()

    def _start(self, request: SnapshotBuildRequest) -> bool:
        self._active = True
        results = self._results

        def run() -> None:
            try:
                result = SnapshotBuildResult(request, snapshot=request.build())
            except Exception as exc:  # noqa: BLE001 - worker boundary reports failures
                result = SnapshotBuildResult(request, error=str(exc))
            # Never touch a QObject from this stdlib worker. The GUI timer owns
            # delivery, so deletion of a view cannot race a cross-thread emit.
            results.put(result)

        self._result_timer.start()
        worker = self._worker_pool.submit(self._worker_name, run)
        if worker is not None:
            self._drain_results()
            return True
        self._active = False
        self._result_timer.stop()
        self.buildFailed.emit(
            SnapshotBuildResult(request, error="Worker pool is stopped")
        )
        self.idle.emit()
        return False

    @Slot()
    def _drain_results(self) -> None:
        if self._closed:
            self._result_timer.stop()
            return
        try:
            value = self._results.get_nowait()
        except Empty:
            return
        self._on_worker_finished(value)

    def _on_worker_finished(self, value: SnapshotBuildResult) -> None:
        if value.snapshot is None:
            self.buildFailed.emit(value)
        else:
            self.snapshotReady.emit(value)
        self._active = False
        if self._closed:
            self._pending = None
            return
        pending = self._pending
        self._pending = None
        if pending is not None:
            self._start(pending)
        else:
            self._result_timer.stop()
            self.idle.emit()


__all__ = [
    "SnapshotBuildCoordinator",
    "SnapshotBuildRequest",
    "SnapshotBuildResult",
]
