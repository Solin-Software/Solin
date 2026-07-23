from __future__ import annotations

from collections import deque
from collections.abc import Mapping
import logging
from time import perf_counter_ns
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from solin.ui.cooperative_dispatch import CooperativeDispatch


log = logging.getLogger(__name__)
_SLOW_DISPATCH_MS = 16.0


class UiPreparationCoordinator(QObject):
    """Starts cancelable UI preparation only when Qt is about to become idle."""

    completed = Signal()

    def __init__(
        self,
        tasks: Mapping[int, Any],
        parent: QObject,
        *,
        on_demand_tasks: Mapping[int, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self._tasks = dict(tasks)
        self._on_demand_tasks = dict(on_demand_tasks or {})
        self._pending = deque(self._tasks)
        self._active_index: int | None = None
        self._started = False
        self._cancelled = False
        self._completed = False
        self._dispatch = CooperativeDispatch(self._dispatch_next, self)
        self._dispatch_durations_ms: list[float] = []
        for index, task in self._tasks.items():
            task.completed.connect(lambda current=index: self._task_finished(current))
            task.failed.connect(lambda _message, current=index: self._task_finished(current))

    @property
    def dispatch_durations_ms(self) -> tuple[float, ...]:
        return tuple(self._dispatch_durations_ms)

    @property
    def unit_durations_ms(self) -> tuple[float, ...]:
        durations: list[float] = []
        for task in self._tasks.values():
            durations.extend(getattr(task, "unit_durations_ms", ()))
            durations.extend(getattr(task, "apply_durations_ms", ()))
        return tuple(durations)

    @property
    def unit_durations_by_task(self) -> dict[int, tuple[float, ...]]:
        result: dict[int, tuple[float, ...]] = {}
        for index, task in self._tasks.items():
            durations = tuple(getattr(task, "unit_durations_ms", ())) + tuple(
                getattr(task, "apply_durations_ms", ())
            )
            if durations:
                result[index] = durations
        return result

    def start(self) -> None:
        if self._started or self._cancelled:
            return
        self._started = True
        if not self._pending:
            self._complete()
            return
        self._dispatch.request()

    def prioritize(self, index: int) -> None:
        if self._cancelled:
            return
        on_demand = self._on_demand_tasks.get(index)
        if on_demand is not None:
            on_demand.start()
            return
        if index not in self._tasks or self._active_index == index:
            return
        try:
            self._pending.remove(index)
        except ValueError:
            return
        self._pending.appendleft(index)
        if self._started and self._active_index is None:
            self._dispatch.request()

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._dispatch.cancel()
        self._pending.clear()
        for task in self._tasks.values():
            task.cancel()
        for task in self._on_demand_tasks.values():
            task.cancel()
        self._active_index = None

    @Slot()
    def _dispatch_next(self) -> None:
        if self._cancelled or not self._started or self._active_index is not None:
            return
        while self._pending:
            index = self._pending.popleft()
            task = self._tasks[index]
            if task.is_terminal:
                continue
            self._active_index = index
            started_ns = perf_counter_ns()
            task.start()
            elapsed_ms = (perf_counter_ns() - started_ns) / 1_000_000
            self._dispatch_durations_ms.append(elapsed_ms)
            if elapsed_ms > _SLOW_DISPATCH_MS:
                log.warning(
                    "UI preparation dispatch exceeded one frame: %.1f ms (page %s)",
                    elapsed_ms,
                    index,
                )
            if task.is_terminal:
                self._active_index = None
                self._dispatch.request()
            return
        self._complete()

    def _task_finished(self, index: int) -> None:
        if self._active_index == index:
            self._active_index = None
        if not self._pending and self._active_index is None:
            self._complete()
        elif self._active_index is None:
            self._dispatch.request()

    def _complete(self) -> None:
        if self._completed or self._cancelled:
            return
        self._completed = True
        self._dispatch.cancel()
        self.completed.emit()


__all__ = ["UiPreparationCoordinator"]
