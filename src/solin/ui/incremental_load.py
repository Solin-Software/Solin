from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Callable
from enum import StrEnum
import logging
from time import perf_counter_ns

from PySide6.QtCore import QObject, Signal, Slot

from .cooperative_dispatch import CooperativeDispatch


log = logging.getLogger(__name__)
_SLOW_UNIT_MS = 16.0


class IncrementalLoadState(StrEnum):
    PENDING = "pending"
    LOADING = "loading"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IncrementalLoadHandle(QObject):
    """Runs one small QWidget construction unit whenever Qt becomes idle."""

    completed = Signal()
    failed = Signal(str)
    state_changed = Signal(str)

    def __init__(
        self,
        units: Iterable[Callable[[], None]],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._units = deque(units)
        self._state = IncrementalLoadState.PENDING
        self._unit_durations_ms: list[float] = []
        self._dispatch = CooperativeDispatch(self._run_next_unit, self)

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
    def unit_durations_ms(self) -> tuple[float, ...]:
        return tuple(self._unit_durations_ms)

    def start(self) -> None:
        if self._state is not IncrementalLoadState.PENDING:
            return
        self._set_state(IncrementalLoadState.LOADING)
        self._dispatch.request()

    def cancel(self) -> None:
        if self.is_terminal:
            return
        self._dispatch.cancel()
        self._units.clear()
        self._set_state(IncrementalLoadState.CANCELLED)

    def complete_now(self) -> None:
        """Finish pending UI work when the user requests the dependent surface."""
        if self.is_terminal:
            return
        if self._state is IncrementalLoadState.PENDING:
            self.start()
        self._dispatch.cancel()
        while self._state is IncrementalLoadState.LOADING:
            self._run_next_unit()

    @Slot()
    def _run_next_unit(self) -> None:
        if self._state is not IncrementalLoadState.LOADING:
            return
        if not self._units:
            self._complete()
            return
        unit = self._units.popleft()
        started_ns = perf_counter_ns()
        try:
            unit()
        except Exception as error:  # noqa: BLE001 - deferred QWidget boundary
            log.exception("Incremental UI preparation failed")
            self._fail(str(error))
            return
        elapsed_ms = (perf_counter_ns() - started_ns) / 1_000_000
        self._unit_durations_ms.append(elapsed_ms)
        if elapsed_ms > _SLOW_UNIT_MS:
            log.warning("Incremental UI unit exceeded one frame: %.1f ms", elapsed_ms)
        if not self._units:
            self._complete()
        else:
            self._dispatch.request()

    def _complete(self) -> None:
        self._dispatch.cancel()
        self._set_state(IncrementalLoadState.READY)
        self.completed.emit()

    def _fail(self, message: str) -> None:
        self._dispatch.cancel()
        self._units.clear()
        self._set_state(IncrementalLoadState.FAILED)
        self.failed.emit(message)

    def _set_state(self, state: IncrementalLoadState) -> None:
        if self._state is state:
            return
        self._state = state
        self.state_changed.emit(state.value)


__all__ = ["IncrementalLoadHandle", "IncrementalLoadState"]
