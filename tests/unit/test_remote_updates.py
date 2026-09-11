import subprocess
import sys
from threading import Event

from PySide6.QtCore import QCoreApplication
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


def test_service_cleans_up_thread_after_failure(tmp_path):
    # Qt owns process-global state. Exercise the real QThread lifecycle in a
    # clean process so unrelated native scene workers cannot corrupt this test.
    source = f"""
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from solin.core.releases.channel import UpdateChannel
from solin.core.remote import updates

app = QCoreApplication([])
updates.GitHubReleases.discover = lambda *args: (_ for _ in ()).throw(RuntimeError('failed'))
service = updates.UpdateService(lambda: 'en', lambda: UpdateChannel.STABLE,
    Path({str(tmp_path)!r}), installed_windows_provider=lambda: False)
loop = QEventLoop()
service.finished.connect(loop.quit)
service.check()
QTimer.singleShot(3000, loop.quit)
loop.exec()
app.processEvents()
assert service._thread is None
service.stop()
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
