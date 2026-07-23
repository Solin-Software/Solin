from __future__ import annotations

import time
from concurrent.futures import Future

from PySide6.QtCore import QObject, Qt, QThread
from PySide6.QtWidgets import QApplication, QWidget

from solin.controllers.ui_preparation_coordinator import UiPreparationCoordinator
from solin.ui.async_load import AsyncLoadHandle
from solin.ui.incremental_load import IncrementalLoadHandle, IncrementalLoadState
from solin.ui.loading_placeholder import DeferredLoadingPlaceholder


_APP = QApplication.instance() or QApplication([])


def _spin_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.005)
    assert predicate()


def test_incremental_handle_runs_exactly_one_unit_per_cooperative_dispatch() -> None:
    parent = QObject()
    calls: list[int] = []
    handle = IncrementalLoadHandle(
        (lambda: calls.append(1), lambda: calls.append(2)),
        parent,
    )

    handle.start()
    handle._run_next_unit()

    assert calls == [1]
    assert handle.state is IncrementalLoadState.LOADING

    handle._run_next_unit()

    assert calls == [1, 2]
    assert handle.state is IncrementalLoadState.READY
    assert len(handle.unit_durations_ms) == 2


def test_incremental_handle_can_complete_on_demand_or_cancel() -> None:
    parent = QObject()
    completed_calls: list[str] = []
    completed = IncrementalLoadHandle(
        (lambda: completed_calls.append("first"), lambda: completed_calls.append("second")),
        parent,
    )
    cancelled = IncrementalLoadHandle(
        (lambda: completed_calls.append("unexpected"),),
        parent,
    )

    completed.complete_now()
    cancelled.cancel()
    cancelled._run_next_unit()

    assert completed_calls == ["first", "second"]
    assert completed.state is IncrementalLoadState.READY
    assert cancelled.state is IncrementalLoadState.CANCELLED


def test_async_handle_applies_worker_result_on_application_thread() -> None:
    parent = QObject()
    results: list[tuple[int, bool]] = []
    handle = AsyncLoadHandle(
        lambda: 42,
        lambda value: results.append(
            (value, QThread.currentThread() is parent.thread())
        ),
        parent,
        thread_name_prefix="test-async-load",
    )

    handle.start()
    assert results == []
    _spin_until(lambda: handle.is_terminal)

    assert results == [(42, True)]
    assert handle.state is IncrementalLoadState.READY
    assert len(handle.apply_durations_ms) == 1


def test_async_handle_ignores_a_late_result_after_cancellation() -> None:
    parent = QObject()
    release = False
    applied: list[int] = []

    def load() -> int:
        deadline = time.monotonic() + 1.0
        while not release and time.monotonic() < deadline:
            time.sleep(0.005)
        return 7

    handle = AsyncLoadHandle(
        load,
        applied.append,
        parent,
        thread_name_prefix="test-cancelled-load",
    )
    handle.start()
    handle.cancel()
    release = True
    _spin_until(lambda: handle.state is IncrementalLoadState.CANCELLED)
    _APP.processEvents()

    assert applied == []


def test_async_handle_propagates_cancellation_to_running_loader() -> None:
    parent = QObject()
    cancellations: list[bool] = []
    handle = AsyncLoadHandle(
        lambda: 1,
        lambda _value: None,
        parent,
        thread_name_prefix="test-cancel-propagation",
        cancel_load=lambda: cancellations.append(True),
    )

    handle.start()
    handle.cancel()

    assert cancellations == [True]


def test_async_handle_closes_delivery_gate_before_cancelling_loader() -> None:
    parent = QObject()
    worker_result: Future[int] = Future()
    worker_result.set_result(7)
    deliveries: list[int] = []
    handle: AsyncLoadHandle

    def complete_during_cancellation() -> None:
        handle._worker_completed(worker_result)

    handle = AsyncLoadHandle(
        lambda: 1,
        lambda _value: None,
        parent,
        thread_name_prefix="test-cancel-delivery-gate",
        cancel_load=complete_during_cancellation,
    )
    handle._result_ready.connect(
        deliveries.append,
        Qt.ConnectionType.DirectConnection,
    )
    handle._set_state(IncrementalLoadState.LOADING)

    handle.cancel()

    assert deliveries == []
    assert handle.state is IncrementalLoadState.CANCELLED


def test_coordinator_keeps_on_demand_tasks_out_of_eager_completion() -> None:
    parent = QObject()
    eager_calls: list[str] = []
    on_demand_calls: list[str] = []
    eager = IncrementalLoadHandle((lambda: eager_calls.append("eager"),), parent)
    on_demand = IncrementalLoadHandle(
        (lambda: on_demand_calls.append("on-demand"),),
        parent,
    )
    coordinator = UiPreparationCoordinator(
        {1: eager},
        parent,
        on_demand_tasks={2: on_demand},
    )
    completions: list[bool] = []
    coordinator.completed.connect(lambda: completions.append(True))

    coordinator.start()
    coordinator._dispatch_next()
    eager.complete_now()

    assert completions == [True]
    assert on_demand.state is IncrementalLoadState.PENDING
    assert on_demand_calls == []

    coordinator.prioritize(2)
    on_demand.complete_now()

    assert on_demand_calls == ["on-demand"]


def test_coordinator_cancels_eager_and_on_demand_tasks() -> None:
    parent = QObject()
    eager = IncrementalLoadHandle((lambda: None,), parent)
    on_demand = IncrementalLoadHandle((lambda: None,), parent)
    coordinator = UiPreparationCoordinator(
        {1: eager},
        parent,
        on_demand_tasks={2: on_demand},
    )

    coordinator.start()
    coordinator.cancel()

    assert eager.state is IncrementalLoadState.CANCELLED
    assert on_demand.state is IncrementalLoadState.CANCELLED


def test_deferred_loading_placeholder_moves_smoothly_in_both_directions() -> None:
    travel = 148.0

    assert DeferredLoadingPlaceholder._chunk_offset(0, travel) == 0.0
    assert DeferredLoadingPlaceholder._chunk_offset(400, travel) == 74.0
    assert DeferredLoadingPlaceholder._chunk_offset(800, travel) == 148.0
    assert DeferredLoadingPlaceholder._chunk_offset(1_200, travel) == 74.0
    assert DeferredLoadingPlaceholder._chunk_offset(1_600, travel) == 0.0


def test_deferred_loading_placeholder_tracks_parent_and_stops_when_finished() -> None:
    host = QWidget()
    host.resize(640, 480)
    placeholder = DeferredLoadingPlaceholder("Loading…", host)

    host.show()
    _APP.processEvents()

    assert placeholder.isVisible()
    assert placeholder.geometry() == host.rect()
    assert placeholder._frame_timer.isActive()

    host.resize(800, 600)
    _APP.processEvents()

    assert placeholder.geometry() == host.rect()

    placeholder.finish()

    assert placeholder.active is False
    assert placeholder.isVisible() is False
    assert placeholder._frame_timer.isActive() is False
    host.close()


def test_visible_loading_animation_does_not_starve_incremental_work() -> None:
    host = QWidget()
    placeholder = DeferredLoadingPlaceholder("Loading…", host)
    calls: list[int] = []
    handle = IncrementalLoadHandle(
        (lambda: calls.append(1), lambda: calls.append(2), lambda: calls.append(3)),
        host,
    )

    host.show()
    handle.start()
    _spin_until(lambda: handle.is_terminal)

    assert placeholder._frame_timer.isActive()
    assert handle.state is IncrementalLoadState.READY
    assert calls == [1, 2, 3]
    host.close()
