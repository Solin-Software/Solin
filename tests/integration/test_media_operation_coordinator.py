from __future__ import annotations

import threading
import time

from PySide6.QtCore import QCoreApplication, QTimer

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.core.media.operations import (
    MediaOperationPresentation,
    MediaOperationProgress,
    MediaOperationSpec,
    MediaOperationState,
)


_APP = QCoreApplication.instance() or QCoreApplication([])


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        QCoreApplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for an asynchronous media operation")
        time.sleep(0.002)
    QCoreApplication.processEvents()


def _spec(
    operation_id: str,
    conflict_key: str,
    runner,
    *,
    commit=lambda _result: None,
    retryable: bool = False,
) -> MediaOperationSpec:
    return MediaOperationSpec(
        operation_id=operation_id,
        scope_id="playlist:test",
        operation_type="test",
        conflict_key=conflict_key,
        presentation=MediaOperationPresentation.TREE_LOCAL,
        runner=runner,
        commit=commit,
        initial_stage="Preparing media…",
        retryable=retryable,
    )


def test_coordinator_runs_independent_work_and_serializes_conflicts() -> None:
    coordinator = MediaOperationCoordinator(max_workers=2)
    started: list[str] = []
    completed: list[str] = []
    lock = threading.Lock()
    releases = {
        operation_id: threading.Event()
        for operation_id in ("same-1", "same-2", "independent")
    }

    def runner(operation_id: str):
        def run(_progress, cancellation):
            with lock:
                started.append(operation_id)
            while not releases[operation_id].wait(0.005):
                if cancellation.is_set():
                    return None
            return operation_id

        return run

    for operation_id, conflict in (
        ("same-1", "folder:a"),
        ("same-2", "folder:a"),
        ("independent", "folder:b"),
    ):
        assert coordinator.submit(
            _spec(
                operation_id,
                conflict,
                runner(operation_id),
                commit=lambda result: completed.append(str(result)),
            )
        )

    _wait_until(lambda: len(started) == 2)
    assert set(started) == {"same-1", "independent"}

    releases["independent"].set()
    _wait_until(lambda: "independent" in completed)
    assert "same-2" not in started

    releases["same-1"].set()
    _wait_until(lambda: "same-2" in started)
    releases["same-2"].set()
    _wait_until(lambda: coordinator.active_count == 0)

    assert completed == ["independent", "same-1", "same-2"]
    assert coordinator.shutdown() == ()


def test_blocking_runner_does_not_stop_the_qt_event_loop() -> None:
    coordinator = MediaOperationCoordinator()
    ticks: list[int] = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()

    def block(_progress, _cancellation):
        time.sleep(0.18)
        return None

    assert coordinator.submit(_spec("blocking", "tree:a", block))
    _wait_until(lambda: coordinator.active_count == 0)
    timer.stop()

    assert len(ticks) >= 10
    coordinator.shutdown()


def test_progress_signal_traffic_is_throttled_to_twenty_hertz() -> None:
    coordinator = MediaOperationCoordinator()
    progress_updates: list[object] = []
    coordinator.operationChanged.connect(progress_updates.append)

    def report_many(progress, _cancellation):
        for completed in range(200):
            progress(
                MediaOperationProgress(
                    MediaOperationState.COPYING,
                    completed=completed,
                    total=200,
                )
            )
        progress(
            MediaOperationProgress(
                MediaOperationState.COPYING,
                completed=200,
                total=200,
            )
        )
        return None

    coordinator.submit(_spec("progress", "tree:a", report_many))
    _wait_until(lambda: coordinator.active_count == 0)

    copying = [
        update
        for update in progress_updates
        if update.state == MediaOperationState.COPYING
    ]
    assert 1 <= len(copying) <= 3
    coordinator.shutdown()


def test_failed_operation_can_be_retried_idempotently() -> None:
    coordinator = MediaOperationCoordinator()
    attempts = 0
    committed: list[str] = []

    def flaky(_progress, _cancellation):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary cloud lock")
        return "ready"

    coordinator.submit(
        _spec(
            "retryable",
            "tree:a",
            flaky,
            commit=lambda result: committed.append(str(result)),
            retryable=True,
        )
    )
    _wait_until(lambda: coordinator.active_count == 0)

    failed = coordinator.record("retryable")
    assert failed is not None
    assert failed.state == MediaOperationState.FAILED
    assert failed.retryable is True
    assert coordinator.retry("retryable")
    _wait_until(lambda: coordinator.active_count == 0)

    assert attempts == 2
    assert committed == ["ready"]
    assert coordinator.record("retryable").state == MediaOperationState.READY
    coordinator.shutdown()


def test_cancelling_queued_work_never_starts_its_runner() -> None:
    coordinator = MediaOperationCoordinator(max_workers=1)
    release = threading.Event()
    queued_started: list[bool] = []

    def active(_progress, cancellation):
        while not release.wait(0.005):
            if cancellation.is_set():
                return None
        return None

    coordinator.submit(_spec("active", "tree:a", active))
    coordinator.submit(
        _spec(
            "queued",
            "tree:b",
            lambda _progress, _cancellation: queued_started.append(True),
        )
    )

    coordinator.cancel("queued")
    assert coordinator.record("queued").state == MediaOperationState.CANCELLED
    release.set()
    _wait_until(lambda: coordinator.active_count == 0)

    assert queued_started == []
    coordinator.shutdown()
