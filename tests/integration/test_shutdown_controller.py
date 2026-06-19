from types import SimpleNamespace

from solin.controllers.shutdown_controller import (
    ShutdownController,
    ShutdownDependencies,
    ShutdownServices,
)
from solin.core.foundation.qt_threads import OwnedQThreadRegistry


class _CleanupWidget:
    def __init__(self, fail=False):
        self.cleaned = False
        self.fail = fail

    def cleanup(self):
        self.cleaned = True
        if self.fail:
            raise RuntimeError("cleanup failed")


class _FinishedSignal:
    def __init__(self):
        self.connected = None

    def connect(self, callback):
        self.connected = callback


class _ThreadStub:
    def __init__(self, running=True, stop_after_quit=True):
        self._running = running
        self._stop_after_quit = stop_after_quit
        self.quit_called = False
        self.wait_ms = None
        self.parent = object()
        self.finished = _FinishedSignal()

    def isRunning(self):
        return self._running

    def quit(self):
        self.quit_called = True
        if self._stop_after_quit:
            self._running = False

    def wait(self, wait_ms):
        self.wait_ms = wait_ms

    def setParent(self, parent):
        self.parent = parent

    def deleteLater(self):
        pass


class _ProjectionSession:
    def __init__(self, events):
        self.projection_windows = [
            SimpleNamespace(close=lambda: events.append("projection"))
        ]
        self._events = events

    def close_floating_preview(self):
        self._events.append("floating")


class _Recorder:
    def __init__(self, events, name):
        self._events = events
        self._name = name

    def __getattr__(self, method_name):
        def _record(*_args, **_kwargs):
            self._events.append(f"{self._name}.{method_name}")

        return _record


def _dependencies(events=None):
    events = events if events is not None else []
    widget = _CleanupWidget()
    registry = OwnedQThreadRegistry()
    return ShutdownDependencies(
        projection_session=_ProjectionSession(events),
        timer_output=_Recorder(events, "timer"),
        services=ShutdownServices(
            remote_services=_Recorder(events, "remote"),
            download_notifications=_Recorder(events, "downloads"),
            playback_notifications=_Recorder(events, "playback-notifications"),
            notifications=_Recorder(events, "notifications"),
            projection_integrations=_Recorder(events, "projection-integrations"),
            background_song=_Recorder(events, "background-song"),
            media_controller=_Recorder(events, "media"),
            ndi=_Recorder(events, "ndi"),
            camera=_Recorder(events, "camera"),
            obs=_Recorder(events, "obs"),
            zoom=_Recorder(events, "zoom"),
            ipc=_Recorder(events, "ipc"),
        ),
        widget_providers=(lambda: widget,),
        conversion_threads=registry,
        jwl_temp_files=set(),
        queue_pending_deletion=lambda path: events.append(("pending", path)),
        save_window_state=lambda: events.append("window-state"),
        cleanup_lazy_pages=lambda: events.append("lazy-pages"),
    )


def test_cleanup_widget_calls_cleanup():
    widget = _CleanupWidget()

    ShutdownController._cleanup_widget(widget)

    assert widget.cleaned is True


def test_cleanup_widget_swallows_cleanup_errors():
    widget = _CleanupWidget(fail=True)

    ShutdownController._cleanup_widget(widget)

    assert widget.cleaned is True


def test_thread_registry_discards_finished_thread():
    registry = OwnedQThreadRegistry()
    thread = _ThreadStub()

    registry.track(thread)
    thread.finished.connected()

    assert registry.active_count == 0


def test_thread_registry_stops_and_removes_finished_thread():
    registry = OwnedQThreadRegistry()
    thread = _ThreadStub(running=True, stop_after_quit=True)
    registry.track(thread)

    registry.stop_all()

    assert thread.quit_called is True
    assert thread.wait_ms == 3000
    assert registry.active_count == 0


def test_thread_registry_defers_delete_when_thread_remains_running():
    registry = OwnedQThreadRegistry()
    thread = _ThreadStub(running=True, stop_after_quit=False)
    registry.track(thread)

    registry.stop_all()

    assert registry.active_count == 1
    assert thread.parent is None
    assert thread.finished.connected == thread.deleteLater


def test_shutdown_runs_owned_cleanup_boundaries_in_order():
    events = []
    dependencies = _dependencies(events)
    thread = _ThreadStub()
    dependencies.conversion_threads.track(thread)

    ShutdownController(dependencies).shutdown()

    assert events == [
        "projection",
        "floating",
        "timer.close_all",
        "remote.stop",
        "downloads.stop",
        "playback-notifications.stop",
        "notifications.shutdown",
        "projection-integrations.cleanup",
        "background-song.shutdown",
        "media.stop",
        "ndi.stop",
        "camera.stop",
        "obs.stop",
        "zoom.stop",
        "ipc.close",
        "window-state",
        "lazy-pages",
    ]
    assert dependencies.projection_session.projection_windows == []
    assert dependencies.conversion_threads.active_count == 0


def test_remove_or_queue_tmp_file_queues_when_remove_fails(monkeypatch):
    queued = []
    monkeypatch.setattr(
        "solin.controllers.shutdown_controller.os.path.isfile",
        lambda path: True,
    )
    monkeypatch.setattr(
        "solin.controllers.shutdown_controller.os.remove",
        lambda path: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(
        ShutdownController,
        "_queue_pending_deletion",
        lambda _self, path: queued.append(path),
    )
    controller = ShutdownController(_dependencies())

    controller._remove_or_queue_tmp_file("temp.jwlplaylist")

    assert queued == ["temp.jwlplaylist"]


def test_queue_pending_deletion_delegates_to_dependency():
    events = []
    controller = ShutdownController(_dependencies(events))

    controller._queue_pending_deletion("temp.jwlplaylist")

    assert events == [("pending", "temp.jwlplaylist")]


def test_shutdown_controller_uses_explicit_dependencies():
    controller = ShutdownController(_dependencies())

    assert not hasattr(controller, "_window")
