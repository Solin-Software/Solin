from solin.controllers.main_window_bootstrap_controller import MainWindowBootstrapController
from solin.core.projection.application import ProjectionSession


class _Signal:
    def __init__(self, name, events=None):
        self.name = name
        self.events = events
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)
        if self.events is not None:
            self.events.append(f"connect:{self.name}")

    @property
    def connected_names(self):
        return [callback.__name__ for callback in self.callbacks]


class _EnabledSettings:
    def __init__(self, enabled):
        self._enabled = enabled

    def is_enabled(self):
        return self._enabled


class _Startable:
    def __init__(self, window, event_name):
        self._window = window
        self._event_name = event_name
        self.started = False

    def start(self):
        self.started = True
        self._window.events.append(self._event_name)


class _LiveIntegrations:
    def __init__(self, window):
        self._window = window

    def refresh_obs_btn_availability(self):
        self._window.events.append("obs-btn")

    def refresh_obs_stream_availability(self):
        self._window.events.append("obs-stream")

    def on_zoom_connection_changed(self):
        pass

    def on_zoom_participants_updated(self):
        pass

    def on_zoom_sharing_state_changed(self):
        pass

    def on_zoom_share_error(self):
        pass


class _ZoomService(_Startable):
    def __init__(self, window):
        super().__init__(window, "zoom")
        self.connection_changed = _Signal("zoom.connection_changed", window.events)
        self.participants_updated = _Signal("zoom.participants_updated", window.events)
        self.sharing_state_changed = _Signal("zoom.sharing_state_changed", window.events)
        self.share_error = _Signal("zoom.share_error", window.events)


class _ProjectionTargets:
    def __init__(self, window):
        self._window = window

    def open_projection_windows(self):
        self._window.events.append("projection")

    def on_monitor_toggle(self):
        pass

    def on_monitor_all(self):
        pass

    def on_floating_toggle(self):
        pass

    def on_idle_media_changed(self):
        pass


class _MonitorPopup:
    def __init__(self, window):
        window.events.append("monitor")
        self.projection_toggle_requested = _Signal("projection_toggle_requested")
        self.projection_all_requested = _Signal("projection_all_requested")
        self.floating_toggle_requested = _Signal("floating_toggle_requested")
        self.idle_media_changed = _Signal("idle_media_changed")


class _IpcController:
    def __init__(self, window):
        self._window = window
        self.started = False
        self.obs_scene_memory_at_start = None

    def start(self):
        self.started = True
        self.obs_scene_memory_at_start = getattr(
            self._window,
            "_obs_pre_media_scene",
            None,
        )
        self._window.events.append("ipc")


class _RemoteServices:
    def __init__(self, window, lang):
        self._window = window
        self.lang = lang
        self.started = False

    def start(self):
        self.started = True
        self._window.events.append("remote")


class _App:
    def __init__(self, ipc_active):
        self._solin_app_ipc_server_active = ipc_active


class _Window:
    def __init__(self, *, obs_enabled=True, zoom_enabled=True):
        self.events = []
        self.lang = object()
        self._projection_targets = _ProjectionTargets(self)
        self._obs_service = _Startable(self, "obs")
        self._obs_settings = _EnabledSettings(obs_enabled)
        self._live_integrations = _LiveIntegrations(self)
        self._zoom_service = _ZoomService(self)
        self._zoom_settings = _EnabledSettings(zoom_enabled)
        self._background_song_service = _Startable(self, "background-song")
        self.projection_session = ProjectionSession()
        self.stylesheets = []

    def setStyleSheet(self, stylesheet):
        self.stylesheets.append(stylesheet)
        self.events.append("style")


def _make_controller(window, *, ipc_active=False, platform="win32"):
    return MainWindowBootstrapController(
        window,
        app_getter=lambda: _App(ipc_active),
        platform=platform,
        monitor_popup_factory=_MonitorPopup,
        ipc_controller_factory=_IpcController,
        remote_services_factory=_RemoteServices,
        stylesheet="test-stylesheet",
    )


def test_finish_startup_preserves_startup_order_and_initializes_state():
    window = _Window(obs_enabled=True, zoom_enabled=True)

    _make_controller(window).finish_startup()

    assert window.events == [
        "projection",
        "obs",
        "obs-btn",
        "obs-stream",
        "connect:zoom.connection_changed",
        "connect:zoom.participants_updated",
        "connect:zoom.sharing_state_changed",
        "connect:zoom.share_error",
        "zoom",
        "background-song",
        "monitor",
        "ipc",
        "remote",
        "style",
    ]
    assert window.projection_session.idle_media_path == ""
    assert window.projection_session.floating_preview_window is None
    assert window.projection_session.state == {"type": "idle"}
    assert window.projection_session.tab_projection_active is False
    assert window._jwl_tmp_files == set()
    assert window._next_is_sjjm is False
    assert window._obs_pre_media_scene == ""
    assert window._ipc_controller.obs_scene_memory_at_start == ""
    assert window.stylesheets == ["test-stylesheet"]


def test_finish_startup_skips_disabled_services_and_app_owned_ipc():
    window = _Window(obs_enabled=False, zoom_enabled=True)

    _make_controller(window, ipc_active=True, platform="linux").finish_startup()

    assert "obs" not in window.events
    assert "zoom" not in window.events
    assert "ipc" not in window.events
    assert window._ipc_controller.started is False
    assert window._remote_services.started is True


def test_finish_startup_wires_zoom_and_monitor_signals():
    window = _Window()

    _make_controller(window).finish_startup()

    assert window._zoom_service.connection_changed.connected_names == [
        "on_zoom_connection_changed"
    ]
    assert window._zoom_service.participants_updated.connected_names == [
        "on_zoom_participants_updated"
    ]
    assert window._zoom_service.sharing_state_changed.connected_names == [
        "on_zoom_sharing_state_changed"
    ]
    assert window._zoom_service.share_error.connected_names == ["on_zoom_share_error"]
    assert window._monitor_popup.projection_toggle_requested.connected_names == [
        "on_monitor_toggle"
    ]
    assert window._monitor_popup.projection_all_requested.connected_names == [
        "on_monitor_all"
    ]
    assert window._monitor_popup.floating_toggle_requested.connected_names == [
        "on_floating_toggle"
    ]
    assert window._monitor_popup.idle_media_changed.connected_names == [
        "on_idle_media_changed"
    ]
