from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from PySide6.QtCore import QCoreApplication, QThread

from solin.core.ingest import wifi_server
from solin.core.ingest.wifi_server import WifiReceiveServer
from solin.core.foundation.thread_workers import ThreadedWorkerPool
from solin.core.integrations.automation.obs import OBSWebSocketService
from solin.core.integrations.ndi import NDIReceiverService
from solin.core.media.downloader import SongDownloader
from solin.core.media.cache import MediaCacheManager
from solin.core.jw.songs import JWSongsStore
from solin.core.jw.languages import JWLanguageService
from solin.core.rendering.fonts import FontManager
from tests._http import LoopbackHTTPServer


def _app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])




class _OBSSettings:
    def is_configured(self) -> bool:
        return True

    def websocket_port(self, default: int = 4455) -> int:
        return default

    def password(self) -> str:
        return ""




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


_WIFI_UPLOAD_THEME = {
    "bg": "#000000",
    "surface": "#111111",
    "surface2": "#222222",
    "border": "#333333",
    "border2": "#444444",
    "accent": "#555555",
    "accent_hover": "#666666",
    "accent_soft": "rgba(85,85,85,.12)",
    "accent_subtle": "rgba(85,85,85,.03)",
    "accent_subtle_hover": "rgba(85,85,85,.09)",
    "text": "#eeeeee",
    "text_on_accent": "#ffffff",
    "muted": "#999999",
    "ok": "#00aa00",
    "err": "#cc0000",
}


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
    _app()
    manager = MediaCacheManager(
        tmp_path,
        downloader_factory=lambda _parent: None,
    )
    callback_threads = []
    manager.cache_changed.connect(
        lambda _url: callback_threads.append(QThread.isMainThread())
    )

    worker = threading.Thread(
        target=manager.notify_cached_threadsafe,
        args=("https://example.test/media.mp4",),
    )
    worker.start()
    worker.join()

    assert _wait_until(lambda: len(callback_threads) == 1)
    assert callback_threads == [True]


def test_threaded_worker_pool_drains_workers_on_shutdown():
    pool = ThreadedWorkerPool()
    started = threading.Event()
    release = threading.Event()

    def _worker() -> None:
        started.set()
        release.wait(1)

    handle = pool.submit("test-worker", _worker)
    assert handle is not None
    assert started.wait(1)
    assert handle.name == "test-worker"
    assert handle.is_alive()
    assert pool.active_count == 1

    release.set()
    assert pool.shutdown(timeout=1) == ()

    assert pool.is_stopped
    assert pool.active_count == 0
    assert not handle.is_alive()
    assert pool.submit("after-shutdown", _worker) is None


def test_threaded_worker_pool_shutdown_serializes_with_pending_start(monkeypatch):
    pool = ThreadedWorkerPool()
    start_pending = threading.Event()
    allow_start = threading.Event()
    shutdown_acquiring = threading.Event()
    worker_ran = threading.Event()
    shutdown_thread = None
    real_lock = pool._lock
    real_start = threading.Thread.start

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is shutdown_thread:
                shutdown_acquiring.set()
            return real_lock.__enter__()

        def __exit__(self, *args):
            return real_lock.__exit__(*args)

    def gated_start(thread):
        if thread.name == "pending-pool-worker":
            start_pending.set()
            assert allow_start.wait(5), "Test did not release the pending worker start"
        return real_start(thread)

    def shutdown():
        nonlocal shutdown_thread
        shutdown_thread = threading.current_thread()
        return pool.shutdown(timeout=2)

    monkeypatch.setattr(pool, "_lock", ObservedLock())
    monkeypatch.setattr(threading.Thread, "start", gated_start)
    with ThreadPoolExecutor(max_workers=2) as controllers:
        submitting = controllers.submit(pool.submit, "pending-pool-worker", worker_ran.set)
        try:
            assert start_pending.wait(2)
            stopping = controllers.submit(shutdown)
            assert shutdown_acquiring.wait(2)
            stopped_before_start = pool.is_stopped
        finally:
            allow_start.set()
        try:
            handle = submitting.result(timeout=5)
            assert stopping.result(timeout=5) == ()
            assert not stopped_before_start
            assert handle is not None and not handle.is_alive()
            assert worker_ran.is_set()
            assert pool.is_stopped and pool.active_count == 0
            assert pool.submit("after-shutdown", worker_ran.set) is None
        finally:
            assert pool.shutdown(timeout=2) == ()


def test_threaded_worker_pool_failed_start_rolls_back_registration(monkeypatch):
    pool = ThreadedWorkerPool()
    worker_ran = threading.Event()
    failure = RuntimeError("cannot start a new thread")
    real_start = threading.Thread.start

    def fail_start(thread):
        if thread.name == "failed-pool-worker":
            raise failure
        return real_start(thread)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(RuntimeError) as caught:
            pool.submit("failed-pool-worker", worker_ran.set)
    assert caught.value is failure
    assert pool.active_count == 0
    assert not worker_ran.is_set() and not pool.is_stopped
    try:
        handle = pool.submit("replacement-pool-worker", worker_ran.set)
        assert handle is not None
        handle.join(timeout=2)
        assert not handle.is_alive() and worker_ran.is_set()
    finally:
        assert pool.shutdown(timeout=2) == ()


def test_threaded_worker_pool_worker_can_shutdown_without_joining_itself():
    pool = ThreadedWorkerPool()
    allow_shutdown = threading.Event()
    result = []
    worker_completed = threading.Event()

    def worker():
        try:
            assert allow_shutdown.wait(5), "Test did not release worker shutdown"
            result.append(pool.shutdown(timeout=2))
        finally:
            worker_completed.set()

    handle = pool.submit("self-shutdown-worker", worker)
    try:
        assert handle is not None
        allow_shutdown.set()
        assert worker_completed.wait(2)
        handle.join(timeout=2)
        assert not handle.is_alive()
        assert result == [("self-shutdown-worker",)]
        assert pool.active_count == 0 and pool.is_stopped
        assert pool.submit("after-self-shutdown", worker) is None
    finally:
        allow_shutdown.set()
        assert pool.shutdown(timeout=2) == ()


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
    service = OBSWebSocketService(_OBSSettings())
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


def test_wifi_server_reports_stopped_after_threads_exit(monkeypatch, tmp_path):
    _app()
    monkeypatch.setattr(wifi_server, "get_local_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(
        wifi_server, "HTTPServer",
        lambda address, handler: LoopbackHTTPServer(handler, port=address[1]),
    )
    service = WifiReceiveServer(embedded_dir=tmp_path)
    stopped: list[bool] = []
    service.server_stopped.connect(lambda: stopped.append(True))

    assert service.start({}, _WIFI_UPLOAD_THEME)
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
    service._pending_start = ({"title": "restart"}, _WIFI_UPLOAD_THEME)
    restarted: list[tuple[dict[str, str], dict[str, str]]] = []
    monkeypatch.setattr(
        service,
        "start",
        lambda labels, theme: restarted.append((labels, theme)) or True,
    )

    service.stop()
    service._finish_shutdown(1, inactivity=False)

    assert service._pending_start is None
    assert restarted == []
    assert service._stopping is False
