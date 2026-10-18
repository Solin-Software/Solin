from queue import Queue
from threading import Event, Semaphore

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, Qt
from shiboken6 import isValid
from solin.core.releases.channel import UpdateChannel
from solin.core.releases.manifest import ReleaseAsset
from solin.core.releases.version import ReleaseVersion
from solin.core.remote import updates
from solin.core.remote.update_policy import UpdateAction, UpdateInfo

_APP = QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def update_info():
    return UpdateInfo(
        UpdateAction.REVEAL,
        ReleaseVersion.parse("26.32.0"),
        ReleaseAsset("linux", "x86_64", "appimage", "Solin.AppImage", 3, "a" * 64, "2.38"),
        "https://github.com/file",
    )


def _wait_until(predicate):
    loop = QEventLoop()
    poll = QTimer(loop)
    deadline = QTimer(loop)
    deadline.setSingleShot(True)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline.timeout.connect(loop.quit)
    try:
        if not predicate():
            poll.start(1)
            deadline.start(3000)
            loop.exec()
        assert predicate(), "Timed out waiting for update service"
    finally:
        poll.stop()
        deadline.stop()


@pytest.fixture
def blocked_service(monkeypatch, tmp_path):
    calls = Queue()
    release = Semaphore(0)
    preference = {"channel": UpdateChannel.STABLE}

    def discover(client, current, channel):
        calls.put((channel, client._cancelled))
        assert release.acquire(timeout=5), "Test did not release discovery"
        return []

    monkeypatch.setattr(updates.GitHubReleases, "discover", discover)
    service = updates.UpdateService(
        lambda: "en", lambda: preference["channel"], tmp_path,
        installed_windows_provider=lambda: False,
    )
    try:
        yield service, preference, calls, release
    finally:
        release.release()
        if isValid(service):
            service.stop()
            _wait_until(lambda: service._thread is None)
            if isValid(service):
                service.deleteLater()


def test_active_check_cancels_and_coalesces_latest_preference(blocked_service):
    service, preference, calls, release = blocked_service
    service.check()
    channel, cancelled = calls.get(timeout=3)
    assert channel == UpdateChannel.STABLE

    preference["channel"] = UpdateChannel.BETA
    service.check()
    service.check()
    assert cancelled.is_set()
    assert calls.empty()

    release.release()
    _wait_until(lambda: not calls.empty())
    channel, next_cancelled = calls.get_nowait()
    assert channel == UpdateChannel.BETA
    assert next_cancelled is not cancelled
    assert not next_cancelled.is_set()
    release.release()
    _wait_until(lambda: service._thread is None)
    assert calls.empty()


@pytest.mark.parametrize("initial", [UpdateChannel.STABLE, UpdateChannel.BETA])
def test_queued_result_is_stale_after_channel_round_trip(
    blocked_service, monkeypatch, update_info, initial,
):
    service, preference, calls, release = blocked_service
    preference["channel"] = initial
    monkeypatch.setattr(updates, "evaluate_update", lambda *args, **kwargs: update_info)
    received, finished = [], []
    service.update_available.connect(received.append)
    service.finished.connect(lambda: finished.append(True))
    service.check()
    channel, cancelled = calls.get(timeout=3)
    assert channel == initial

    emitted = Event()
    service._thread.update_available.connect(
        lambda *_: emitted.set(), Qt.ConnectionType.DirectConnection,
    )
    release.release()
    assert emitted.wait(3), "Worker did not queue its result"

    preference["channel"] = (
        UpdateChannel.BETA if initial == UpdateChannel.STABLE else UpdateChannel.STABLE
    )
    service.check()
    preference["channel"] = initial
    service.check()
    assert cancelled.is_set()
    _wait_until(lambda: not calls.empty())
    assert calls.get_nowait()[0] == initial
    assert received == []
    assert finished == []

    release.release()
    _wait_until(lambda: service._thread is None)
    assert received == [update_info]
    assert finished == [True]
    assert calls.empty()


def test_stop_is_nonblocking_and_drops_pending_restart(blocked_service, monkeypatch):
    service, preference, calls, release = blocked_service
    service.check()
    _, cancelled = calls.get(timeout=3)
    preference["channel"] = UpdateChannel.BETA
    service.check()

    def fail_wait(*args):
        pytest.fail("Stopping must not wait on the GUI thread")

    with monkeypatch.context() as patch:
        patch.setattr(service._thread, "wait", fail_wait)
        service.stop()
    assert cancelled.is_set()
    service.check()
    release.release()
    _wait_until(lambda: service._thread is None)
    assert calls.empty()
    service.check()
    assert service._thread is None


def test_service_accepts_another_check_after_becoming_idle(blocked_service):
    service, preference, calls, release = blocked_service
    for channel in (UpdateChannel.STABLE, UpdateChannel.BETA):
        preference["channel"] = channel
        service.check()
        assert calls.get(timeout=3)[0] == channel
        release.release()
        _wait_until(lambda: service._thread is None)


def test_shutdown_detaches_until_worker_finishes_without_waiting_or_restart(
    blocked_service, monkeypatch, qt_object_owner,
):
    service, preference, calls, release = blocked_service
    service.setParent(qt_object_owner)
    service.check()
    _, cancelled = calls.get(timeout=3)
    preference["channel"] = UpdateChannel.BETA
    service.check()
    destroyed = []
    service.destroyed.connect(lambda *_: destroyed.append(True))

    def fail_wait(*args):
        pytest.fail("Shutdown must not wait for the update thread")

    with monkeypatch.context() as patch:
        patch.setattr(service._thread, "wait", fail_wait)
        service.shutdown()
    assert service.parent() is None
    assert cancelled.is_set()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert isValid(service)
    assert destroyed == []

    service.check()
    release.release()
    _wait_until(lambda: service._thread is None)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed == [True]
    assert calls.empty()


def test_worker_always_finishes_when_discovery_fails(monkeypatch, tmp_path):
    def fail(*args):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(updates.GitHubReleases, "discover", fail)
    worker = updates.UpdateWorker("en", UpdateChannel.STABLE, tmp_path, False, Event())
    finished, failed = [], []
    worker.finished.connect(lambda: finished.append(True))
    worker.fetch_failed.connect(failed.append)
    try:
        worker.start()
        _wait_until(lambda: bool(finished))
        assert finished == [True]
        assert failed == ["network unavailable"]
    finally:
        assert worker.wait(3000)
        worker.deleteLater()


def test_worker_does_not_expose_identity(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        updates.GitHubReleases, "discover", lambda self, *args: calls.append(args) or []
    )
    worker = updates.UpdateWorker("pt_BR", UpdateChannel.BETA, tmp_path, False, Event())
    finished = []
    worker.finished.connect(lambda: finished.append(True))
    try:
        worker.start()
        _wait_until(lambda: bool(finished))
        assert calls == [(updates.APP_VERSION, UpdateChannel.BETA)]
    finally:
        assert worker.wait(3000)
        worker.deleteLater()


def test_service_cleans_up_thread_after_failure(monkeypatch, tmp_path):
    def fail(*args):
        raise RuntimeError("failed")

    monkeypatch.setattr(updates.GitHubReleases, "discover", fail)
    service = updates.UpdateService(
        lambda: "en", lambda: UpdateChannel.STABLE,
        tmp_path, installed_windows_provider=lambda: False,
    )
    loop = QEventLoop()
    timer = QTimer(loop)
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    service.finished.connect(loop.quit)
    try:
        service.check()
        timer.start(3000)
        loop.exec()
        assert service._thread is None
    finally:
        timer.stop()
        service.stop(wait_ms=3000, delete_when_stopped=True)
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_stopping_suppresses_pending_result(tmp_path, update_info):
    service = updates.UpdateService(
        lambda: "en",
        lambda: UpdateChannel.STABLE,
        tmp_path,
        installed_windows_provider=lambda: False,
    )
    received = []
    service.update_available.connect(received.append)
    service.stop()
    service._on_result(service._generation, update_info)
    assert received == []
