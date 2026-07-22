from __future__ import annotations

import threading
import time

from PySide6.QtCore import QCoreApplication

from solin.controllers.snapshot_write_coordinator import SnapshotWriteCoordinator
from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.core.foundation.resource_keys import (
    child_folder_resource_claim,
    folder_resource_key,
)
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.media.operations import MediaOperationPresentation, MediaOperationSpec


_APP = QCoreApplication.instance() or QCoreApplication([])


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        QCoreApplication.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for snapshot write")
        time.sleep(0.002)
    QCoreApplication.processEvents()


def test_latest_pending_snapshot_wins_and_write_runs_off_gui_thread() -> None:
    coordinator = SnapshotWriteCoordinator(debounce_ms=1)
    writes: list[tuple[int, int]] = []
    gui_thread = threading.get_ident()

    coordinator.request("playlists", lambda: writes.append((1, threading.get_ident())))
    coordinator.request("playlists", lambda: writes.append((2, threading.get_ident())))

    _wait_until(lambda: len(writes) == 1)

    assert writes[0][0] == 2
    assert writes[0][1] != gui_thread
    coordinator.shutdown()


def test_new_snapshot_during_inflight_write_is_persisted_after_it() -> None:
    coordinator = SnapshotWriteCoordinator(debounce_ms=0)
    release = threading.Event()
    writes: list[int] = []
    coordinator.request("meeting", lambda: (release.wait(1), writes.append(1)))
    QCoreApplication.processEvents()
    coordinator.request("meeting", lambda: writes.append(2))
    release.set()

    _wait_until(lambda: writes == [1, 2])

    coordinator.shutdown()


def test_superseded_write_failure_does_not_publish_a_stale_error() -> None:
    coordinator = SnapshotWriteCoordinator(debounce_ms=0)
    started = threading.Event()
    release = threading.Event()
    failures: list[tuple[str, int, str]] = []
    completed: list[tuple[str, int]] = []
    coordinator.writeFailed.connect(
        lambda key, generation, message: failures.append(
            (key, generation, message)
        )
    )
    coordinator.writeCompleted.connect(
        lambda key, generation: completed.append((key, generation))
    )

    def stale_write() -> None:
        started.set()
        release.wait(1)
        raise OSError("stale failure")

    coordinator.request("meeting", stale_write)
    _wait_until(started.is_set)
    coordinator.request("meeting", lambda: None)
    release.set()

    _wait_until(lambda: completed == [("meeting", 2)])

    assert failures == []
    coordinator.shutdown()


def test_latest_failed_snapshot_can_be_retried_without_rebuilding_it() -> None:
    coordinator = SnapshotWriteCoordinator(debounce_ms=0)
    attempts = 0
    completed: list[tuple[str, int]] = []

    def writer() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk busy")

    coordinator.writeCompleted.connect(
        lambda key, generation: completed.append((key, generation))
    )
    coordinator.request("playlists", writer)
    _wait_until(lambda: attempts == 1 and coordinator.retry("playlists"))
    _wait_until(lambda: completed == [("playlists", 1)])

    assert attempts == 2
    coordinator.shutdown()


def test_child_work_and_root_mutation_share_the_same_resource_lane(tmp_path) -> None:
    lanes = ResourceLaneRegistry()
    snapshots = SnapshotWriteCoordinator(
        debounce_ms=0,
        resource_lanes=lanes,
    )
    operations = MediaOperationCoordinator(resource_lanes=lanes)
    snapshot_started = threading.Event()
    release_snapshot = threading.Event()
    operation_started = threading.Event()
    watched_root = tmp_path / "watched"
    playlist_folder = watched_root / "playlist"

    def write_snapshot() -> None:
        snapshot_started.set()
        release_snapshot.wait(1)

    snapshots.request(
        "playlist",
        write_snapshot,
        conflict_key=child_folder_resource_claim(playlist_folder),
    )
    _wait_until(snapshot_started.is_set)
    operations.submit(
        MediaOperationSpec(
            operation_id="copy",
            scope_id="playlist:test",
            operation_type="copy",
            conflict_key=folder_resource_key(watched_root),
            presentation=MediaOperationPresentation.BACKGROUND,
            runner=lambda _progress, _cancellation: operation_started.set(),
            commit=lambda _result: None,
        )
    )

    time.sleep(0.03)
    assert not operation_started.is_set()
    release_snapshot.set()
    _wait_until(operation_started.is_set)

    snapshots.shutdown()
    operations.shutdown()


def test_snapshot_shutdown_wait_is_bounded_for_a_stuck_writer() -> None:
    coordinator = SnapshotWriteCoordinator(debounce_ms=0)
    started = threading.Event()
    release = threading.Event()

    def stuck_writer() -> None:
        started.set()
        release.wait(1)

    coordinator.request("meeting", stuck_writer)
    _wait_until(started.is_set)
    before = time.monotonic()
    unfinished = coordinator.shutdown(wait_ms=20)
    elapsed = time.monotonic() - before
    release.set()

    assert unfinished == ("meeting",)
    assert elapsed < 0.2
