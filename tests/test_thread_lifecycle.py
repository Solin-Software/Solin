from __future__ import annotations

import threading
import time

from PySide6.QtCore import QCoreApplication

from solin.core.ingest.wifi_server import WifiReceiveServer
from solin.core.integrations.automation.obs import OBSWebSocketService
from solin.core.integrations.automation.zoom import service as zoom_module
from solin.core.integrations.automation.zoom.service import ZoomService
from solin.core.integrations.ndi import NDIReceiverService
from solin.core.media.downloader import SongDownloader
from solin.core.media.cache import MediaCacheManager
from solin.core.jw.songs import JWSongsStore
from solin.core.jw.languages import JWLanguageService
from solin.core.profiles.settings import ProfileSettings
from solin.core.rendering.fonts import FontManager


def _app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _prefs():
    return ProfileSettings.for_profile_id("thread_lifecycle").prefs()


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def test_downloader_ignores_results_from_replaced_job(monkeypatch, tmp_path):
    _app()
    jobs = []
    job_started = threading.Event()

    def _capture_job(_self, job):
        jobs.append(job)
        job_started.set()

    monkeypatch.setattr(SongDownloader, "_worker", _capture_job)
    downloader = SongDownloader(tmp_path)
    finished: list[str] = []
    progress: list[tuple[int, int]] = []
    downloader.finished.connect(finished.append)
    downloader.progress.connect(lambda current, total: progress.append((current, total)))

    first_id = downloader.start("https://example.test/old.mp3", persist=False)
    assert job_started.wait(1)
    first_job = jobs[-1]
    job_started.clear()

    second_id = downloader.start("https://example.test/new.mp3", persist=True)
    assert job_started.wait(1)
    assert first_job.cancel_event.is_set()
    assert first_id != second_id

    stale_temp = tmp_path / "stale.tmp"
    stale_temp.write_bytes(b"old")
    downloader._deliver_progress(first_id, 10, 100)
    downloader._deliver_finished(first_id, str(stale_temp), False)

    assert progress == []
    assert finished == []
    assert not stale_temp.exists()

    current_path = str(tmp_path / "current.mp3")
    downloader._deliver_progress(second_id, 100, 100)
    downloader._deliver_finished(second_id, current_path, True)

    assert progress == [(100, 100)]
    assert finished == [current_path]


def test_media_cache_notification_from_python_thread_runs_on_qt_thread(tmp_path):
    app = _app()
    manager = MediaCacheManager(tmp_path)
    callback_threads = []
    manager.cache_changed.connect(
        lambda _url: callback_threads.append(QCoreApplication.instance().thread())
    )

    worker = threading.Thread(
        target=manager.notify_cached_threadsafe,
        args=("https://example.test/media.mp4",),
    )
    worker.start()
    worker.join()

    assert _wait_until(lambda: len(callback_threads) == 1)
    assert callback_threads == [app.thread()]


def test_font_manager_shutdown_cancels_and_joins_workers(tmp_path):
    manager = FontManager(tmp_path)
    events = []

    class _Worker:
        def cancel(self):
            events.append("cancel")

        def wait(self):
            events.append("wait")

    manager._workers["font"] = _Worker()

    manager.shutdown()

    assert events == ["cancel", "wait"]


def test_jw_songs_store_owns_and_drains_its_thread_pool(tmp_path):
    store = JWSongsStore(tmp_path)
    events = []

    class _Pool:
        def clear(self):
            events.append("clear")

        def waitForDone(self):
            events.append("wait")

    store._thread_pool = _Pool()
    store._workers["request"] = object()

    store.shutdown()

    assert events == ["clear", "wait"]
    assert store._workers == {}


def test_jw_language_service_owns_and_drains_its_thread_pool(tmp_path):
    service = JWLanguageService(cache_file=tmp_path / "languages.json")
    events = []

    class _Pool:
        def clear(self):
            events.append("clear")

        def waitForDone(self):
            events.append("wait")

    service._thread_pool = _Pool()
    service._worker = object()
    service._is_loading = True

    service.shutdown()

    assert events == ["clear", "wait"]
    assert service._worker is None
    assert service.is_loading is False


def test_zoom_workers_are_coalesced_and_serialized(monkeypatch):
    _app()
    monkeypatch.setattr(zoom_module, "_HAS_ZOOM", True)
    service = ZoomService(_prefs())
    service._active = True
    service._stop_evt.clear()
    service._generation = 1

    gate = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    calls: list[str] = []

    def _worker(name):
        def _run(_generation):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                calls.append(name)
            gate.wait(2)
            with lock:
                active -= 1

        return _run

    service._launch_worker("connection", _worker("connection"))
    service._launch_worker("connection", _worker("duplicate"))
    service._launch_worker("participants", _worker("participants"))

    assert _wait_until(lambda: calls == ["connection"])
    gate.set()
    assert _wait_until(lambda: len(calls) == 2)
    service.stop(wait=True)

    assert calls == ["connection", "participants"]
    assert max_active == 1


def test_zoom_rejects_callback_from_stopped_generation(monkeypatch):
    _app()
    monkeypatch.setattr(zoom_module, "_HAS_ZOOM", True)
    service = ZoomService(_prefs())
    service.start()
    generation = service._generation

    service.stop()
    service._on_connected_main(generation, True)
    service._on_sharing_main(generation, True)

    assert service.is_connected is False
    assert service.is_sharing is False
    assert not service._part_timer.isActive()
    assert not service._share_timer.isActive()


def test_ndi_restart_waits_for_previous_worker():
    _app()
    service = NDIReceiverService()
    lock = threading.Lock()
    sources: list[str] = []
    active = 0
    max_active = 0

    def _worker(generation, stop_event, source_name, _max_fps):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            sources.append(source_name)
        stop_event.wait(2)
        with lock:
            active -= 1
        service._worker_stopped.emit(generation)

    service._worker = _worker
    service.start("Source A")
    assert _wait_until(lambda: sources == ["Source A"])

    service.start("Source B")
    assert _wait_until(lambda: sources == ["Source A", "Source B"])
    service.stop(wait=True)

    assert max_active == 1


def test_obs_restart_waits_for_previous_worker(monkeypatch):
    _app()
    service = OBSWebSocketService(_prefs)
    monkeypatch.setattr(service, "_config_ok", lambda: True)
    lock = threading.Lock()
    generations: list[int] = []
    active = 0
    max_active = 0

    def _worker(generation, stop_event):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            generations.append(generation)
        stop_event.wait(2)
        with lock:
            active -= 1

    service._worker_connect = _worker
    service.start()
    assert _wait_until(lambda: len(generations) == 1)

    service.stop()
    service.start()
    assert _wait_until(lambda: len(generations) == 2)
    service.stop(wait=True)

    assert max_active == 1
    assert generations[1] > generations[0]


def test_wifi_server_reports_stopped_after_threads_exit(tmp_path):
    _app()
    service = WifiReceiveServer(embedded_dir=tmp_path)
    stopped: list[bool] = []
    service.server_stopped.connect(lambda: stopped.append(True))

    assert service.start({})
    server_thread = service._thread
    assert server_thread is not None and server_thread.is_alive()

    service.stop(wait=True)

    assert stopped == [True]
    assert service._thread is None
    assert not server_thread.is_alive()


def test_wifi_explicit_stop_cancels_queued_restart(monkeypatch, tmp_path):
    _app()
    service = WifiReceiveServer(embedded_dir=tmp_path)
    service._generation = 1
    service._stopping = True
    service._pending_start = {"title": "restart"}
    restarted: list[dict[str, str]] = []
    monkeypatch.setattr(
        service,
        "start",
        lambda labels: restarted.append(labels) or True,
    )

    service.stop()
    service._finish_shutdown(1, inactivity=False)

    assert service._pending_start is None
    assert restarted == []
    assert service._stopping is False
