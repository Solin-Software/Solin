from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
import logging
from threading import Lock
from time import perf_counter_ns
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .incremental_load import IncrementalLoadState


log = logging.getLogger(__name__)


class AsyncLoadHandle(QObject):
    """Runs non-Qt preparation in one owned worker and applies it on the UI thread."""

    completed = Signal()
    failed = Signal(str)
    state_changed = Signal(str)
    _result_ready = Signal(object)
    _worker_failed = Signal(str)

    def __init__(
        self,
        load: Callable[[], Any],
        apply: Callable[[Any], None],
        parent: QObject,
        *,
        thread_name_prefix: str,
        cancel_load: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._load = load
        self._apply = apply
        self._cancel_load = cancel_load
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=thread_name_prefix,
        )
        self._future: Future[Any] | None = None
        self._state = IncrementalLoadState.PENDING
        self._delivery_lock = Lock()
        self._accept_worker_delivery = True
        self._apply_durations_ms: list[float] = []
        self._result_ready.connect(
            self._apply_result,
            Qt.ConnectionType.QueuedConnection,
        )
        self._worker_failed.connect(
            self._fail,
            Qt.ConnectionType.QueuedConnection,
        )

    @property
    def state(self) -> IncrementalLoadState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in {
            IncrementalLoadState.READY,
            IncrementalLoadState.FAILED,
            IncrementalLoadState.CANCELLED,
        }

    @property
    def apply_durations_ms(self) -> tuple[float, ...]:
        return tuple(self._apply_durations_ms)

    def start(self) -> None:
        if self._state is not IncrementalLoadState.PENDING:
            return
        self._set_state(IncrementalLoadState.LOADING)
        future = self._executor.submit(self._load)
        self._future = future
        future.add_done_callback(self._worker_completed)

    def cancel(self) -> None:
        if self.is_terminal:
            return
        with self._delivery_lock:
            self._accept_worker_delivery = False
        if self._cancel_load is not None:
            self._cancel_load()
        self._set_state(IncrementalLoadState.CANCELLED)
        if self._future is not None:
            self._future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _worker_completed(self, future: Future[Any]) -> None:
        try:
            result = future.result()
        except Exception as error:  # noqa: BLE001 - worker result boundary
            log.exception("Asynchronous UI preparation failed")
            with self._delivery_lock:
                if not self._accept_worker_delivery:
                    return
                self._worker_failed.emit(str(error))
            return
        with self._delivery_lock:
            if not self._accept_worker_delivery:
                return
            self._result_ready.emit(result)

    @Slot(object)
    def _apply_result(self, result: Any) -> None:
        if self._state is not IncrementalLoadState.LOADING:
            return
        started_ns = perf_counter_ns()
        try:
            self._apply(result)
        except Exception as error:  # noqa: BLE001 - UI apply boundary
            log.exception("Could not apply asynchronous UI preparation")
            self._fail(str(error))
            return
        self._apply_durations_ms.append((perf_counter_ns() - started_ns) / 1_000_000)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._set_state(IncrementalLoadState.READY)
        self.completed.emit()

    @Slot(str)
    def _fail(self, message: str) -> None:
        if self.is_terminal:
            return
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._set_state(IncrementalLoadState.FAILED)
        self.failed.emit(message)

    def _set_state(self, state: IncrementalLoadState) -> None:
        if self._state is state:
            return
        self._state = state
        self.state_changed.emit(state.value)


__all__ = ["AsyncLoadHandle"]
