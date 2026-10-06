from threading import Event

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from solin.core.releases.channel import UpdateChannel
from solin.core.remote import updates

_APP = QCoreApplication.instance() or QCoreApplication([])


def test_worker_always_finishes_when_discovery_fails(monkeypatch, tmp_path):
    def fail(*args):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(updates.GitHubReleases, "discover", fail)
    worker = updates.UpdateWorker("en", UpdateChannel.STABLE, tmp_path, False, Event())
    finished, failed = [], []
    worker.finished.connect(lambda: finished.append(True))
    worker.fetch_failed.connect(failed.append)
    worker.run()
    assert finished == [True]
    assert failed == ["network unavailable"]


def test_worker_does_not_expose_identity(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        updates.GitHubReleases, "discover", lambda self, *args: calls.append(args) or []
    )
    worker = updates.UpdateWorker("pt_BR", UpdateChannel.BETA, tmp_path, False, Event())
    worker.run()
    assert calls == [(updates.APP_VERSION, UpdateChannel.BETA)]


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
        assert service._worker is None
    finally:
        timer.stop()
        service.stop(wait_ms=3000, delete_when_stopped=True)
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_stopping_suppresses_pending_result(monkeypatch, tmp_path):
    from solin.core.releases.manifest import ReleaseAsset
    from solin.core.releases.version import ReleaseVersion
    from solin.core.remote.update_policy import UpdateAction, UpdateInfo

    service = updates.UpdateService(
        lambda: "en",
        lambda: UpdateChannel.STABLE,
        tmp_path,
        installed_windows_provider=lambda: False,
    )
    received = []
    service.update_available.connect(received.append)
    service.stop()
    service._on_result(
        UpdateInfo(
            UpdateAction.REVEAL,
            ReleaseVersion.parse("26.32.0"),
            ReleaseAsset("linux", "x86_64", "appimage", "Solin.AppImage", 3, "a" * 64, "2.35"),
            "https://github.com/file",
        )
    )
    assert received == []
