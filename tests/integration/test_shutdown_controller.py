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
        self.projection_windows = [SimpleNamespace(close=lambda: events.append("projection"))]
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
            remote_control=lambda: _Recorder(events, "remote-control"),
            remote_services=lambda: _Recorder(events, "remote"),
            download_notifications=_Recorder(events, "downloads"),
            playback_notifications=_Recorder(events, "playback-notifications"),
            notifications=_Recorder(events, "notifications"),
            projection_integrations=_Recorder(events, "projection-integrations"),
            background_song=_Recorder(events, "background-song"),
            media_countdown_automation=_Recorder(events, "media-countdown"),
            media_tree_runtime=_Recorder(events, "media-tree"),
            media_controller=_Recorder(events, "media"),
            ndi=_Recorder(events, "ndi"),
            obs=_Recorder(events, "obs"),
            zoom=_Recorder(events, "zoom"),
            ipc=lambda: _Recorder(events, "ipc"),
        ),
        widget_providers=(lambda: widget,),
        conversion_threads=registry,
        jwl_temp_files=set(),
        queue_pending_deletion=lambda path: events.append(("pending", path)),
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
        "remote-control.stop",
        "media-countdown.shutdown",
        "projection",
        "floating",
        "timer.close_all",
        "remote.stop",
        "downloads.stop",
        "playback-notifications.stop",
        "notifications.shutdown",
        "media-tree.shutdown",
        "projection-integrations.cleanup",
        "background-song.shutdown",
        "media.stop",
        "ndi.stop",
        "obs.stop",
        "zoom.stop",
        "ipc.close",
        "lazy-pages",
    ]
    assert dependencies.projection_session.projection_windows == []
    assert dependencies.conversion_threads.active_count == 0


def test_scene_data_plane_stops_in_reverse_dependency_order():
    events = []
    dependencies = _dependencies(events)
    services = dependencies.services
    dependencies = ShutdownDependencies(
        projection_session=dependencies.projection_session,
        timer_output=dependencies.timer_output,
        services=ShutdownServices(
            remote_control=services.remote_control,
            remote_services=services.remote_services,
            download_notifications=services.download_notifications,
            playback_notifications=services.playback_notifications,
            notifications=services.notifications,
            projection_integrations=services.projection_integrations,
            background_song=services.background_song,
            media_countdown_automation=services.media_countdown_automation,
            media_tree_runtime=services.media_tree_runtime,
            media_controller=services.media_controller,
            ndi=services.ndi,
            obs=services.obs,
            zoom=services.zoom,
            ipc=services.ipc,
            program_content=_Recorder(events, "program-content"),
            content_frame_ingress=_Recorder(events, "content-ingress"),
            scene_frame_egresses=(_Recorder(events, "scene-egress"),),
            scenes=_Recorder(events, "scenes"),
        ),
        widget_providers=dependencies.widget_providers,
        conversion_threads=dependencies.conversion_threads,
        jwl_temp_files=dependencies.jwl_temp_files,
        queue_pending_deletion=dependencies.queue_pending_deletion,
        cleanup_lazy_pages=dependencies.cleanup_lazy_pages,
    )

    ShutdownController(dependencies).shutdown()

    scene_events = [event for event in events if event.startswith((
        "program-content.",
        "content-ingress.",
        "scene-egress.",
        "scenes.",
    ))]
    assert scene_events == [
        "program-content.close",
        "content-ingress.close",
        "scene-egress.close",
        "scenes.close",
    ]


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


def test_shutdown_allows_deferred_resources_that_were_never_created():
    dependencies = _dependencies()
    services = dependencies.services
    dependencies = ShutdownDependencies(
        projection_session=dependencies.projection_session,
        timer_output=dependencies.timer_output,
        services=ShutdownServices(
            remote_control=services.remote_control,
            remote_services=lambda: None,
            download_notifications=services.download_notifications,
            playback_notifications=services.playback_notifications,
            notifications=services.notifications,
            projection_integrations=services.projection_integrations,
            background_song=services.background_song,
            media_countdown_automation=services.media_countdown_automation,
            media_tree_runtime=services.media_tree_runtime,
            media_controller=services.media_controller,
            ndi=services.ndi,
            obs=services.obs,
            zoom=services.zoom,
            ipc=lambda: None,
        ),
        widget_providers=dependencies.widget_providers,
        conversion_threads=dependencies.conversion_threads,
        jwl_temp_files=dependencies.jwl_temp_files,
        queue_pending_deletion=dependencies.queue_pending_deletion,
        cleanup_lazy_pages=dependencies.cleanup_lazy_pages,
    )

    ShutdownController(dependencies).shutdown()


def test_shutdown_is_idempotent_and_one_failed_stage_does_not_skip_later_cleanup(
    monkeypatch,
):
    events = []
    controller = ShutdownController(_dependencies(events))

    def fail_remote_control():
        events.append("remote-control.failed")
        raise RuntimeError("simulated shutdown failure")

    monkeypatch.setattr(controller, "stop_remote_control", fail_remote_control)

    controller.shutdown()
    controller.shutdown()

    assert events.count("remote-control.failed") == 1
    assert events.count("lazy-pages") == 1
    assert "ipc.close" in events
