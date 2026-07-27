import threading

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QThread, QTimer
from shiboken6 import isValid

from solin.core.foundation.thread_workers import ThreadedWorkerPool

from solin.ui.qml.media_tree.build_coordinator import (
    SnapshotBuildCoordinator,
    SnapshotBuildRequest,
    SnapshotBuildResult,
)
from solin.ui.qml.media_tree.snapshot import MediaTreeSnapshot


_APP = QCoreApplication.instance() or QCoreApplication([])


class _ControlledWorkers:
    def __init__(self) -> None:
        self.tasks = []

    def submit(self, _name: str, target):
        self.tasks.append(target)
        return object()


def _request(revision: int) -> SnapshotBuildRequest:
    return SnapshotBuildRequest(
        generation=1,
        revision=revision,
        tree_id="playlist:test",
        build=lambda: MediaTreeSnapshot.create("playlist:test", revision),
    )


def test_snapshot_build_coordinator_coalesces_pending_work_without_starvation() -> None:
    workers = _ControlledWorkers()
    coordinator = SnapshotBuildCoordinator(workers, worker_name="test")
    published: list[SnapshotBuildResult] = []
    idle: list[bool] = []
    coordinator.snapshotReady.connect(published.append)
    coordinator.idle.connect(lambda: idle.append(True))

    assert coordinator.request(_request(1))
    assert coordinator.request(_request(2))
    assert coordinator.request(_request(3))
    assert len(workers.tasks) == 1

    workers.tasks.pop(0)()
    coordinator._drain_results()
    assert [result.snapshot.revision for result in published if result.snapshot] == [1]
    assert len(workers.tasks) == 1
    assert idle == []

    workers.tasks.pop(0)()
    coordinator._drain_results()
    assert [result.snapshot.revision for result in published if result.snapshot] == [1, 3]
    assert idle == [True]


def test_snapshot_build_coordinator_reports_worker_boundary_failures() -> None:
    workers = _ControlledWorkers()
    coordinator = SnapshotBuildCoordinator(workers, worker_name="test")
    failures: list[SnapshotBuildResult] = []
    coordinator.buildFailed.connect(failures.append)
    request = SnapshotBuildRequest(
        generation=1,
        revision=3,
        tree_id="playlist:test",
        build=lambda: (_ for _ in ()).throw(RuntimeError("broken snapshot")),
    )

    assert coordinator.request(request)
    workers.tasks.pop(0)()
    coordinator._drain_results()

    assert len(failures) == 1
    assert failures[0].request is request
    assert failures[0].error == "broken snapshot"


def test_closed_coordinator_drops_an_inflight_worker_completion() -> None:
    workers = _ControlledWorkers()
    coordinator = SnapshotBuildCoordinator(workers, worker_name="test")
    published: list[object] = []
    coordinator.snapshotReady.connect(published.append)
    assert coordinator.request(_request(1))

    coordinator.close()
    workers.tasks.pop(0)()

    assert published == []


def test_threaded_build_returns_to_the_coordinators_qt_thread() -> None:
    workers = ThreadedWorkerPool()
    coordinator = SnapshotBuildCoordinator(workers, worker_name="test")
    loop = QEventLoop()
    build_threads: list[int] = []
    callback_threads: list[QThread] = []

    def build() -> MediaTreeSnapshot:
        build_threads.append(threading.get_ident())
        return MediaTreeSnapshot.create("playlist:test", 1)

    def ready(_result: object) -> None:
        callback_threads.append(QThread.currentThread())
        loop.quit()

    coordinator.snapshotReady.connect(ready)
    assert coordinator.request(
        SnapshotBuildRequest(1, 1, "playlist:test", build)
    )
    timeout = QTimer(loop)
    timeout.setSingleShot(True)
    timeout.setInterval(2_000)
    timeout.timeout.connect(loop.quit)
    timeout.start()
    try:
        if not callback_threads:
            loop.exec()
    finally:
        timeout.stop()
        workers.shutdown()

    assert build_threads and build_threads[0] != threading.get_ident()
    assert callback_threads == [coordinator.thread()]


def test_deleting_a_closed_coordinator_cannot_race_an_inflight_worker() -> None:
    workers = ThreadedWorkerPool()
    coordinator = SnapshotBuildCoordinator(workers, worker_name="test")
    started = threading.Event()
    release = threading.Event()

    def build() -> MediaTreeSnapshot:
        started.set()
        release.wait(2)
        return MediaTreeSnapshot.create("playlist:test", 1)

    assert coordinator.request(
        SnapshotBuildRequest(1, 1, "playlist:test", build)
    )
    assert started.wait(1)
    coordinator.close()
    coordinator.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(coordinator)

    release.set()
    assert workers.shutdown() == ()
