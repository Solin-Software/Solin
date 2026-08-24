from types import SimpleNamespace

from solin.controllers.live_integration_controller import (
    LiveIntegrationContext,
    LiveIntegrationController,
    LiveIntegrationHandlers,
)
from solin.core.projection.application import ObsSceneSession, ProjectionSession


class _ObsSettingsStub:
    def __init__(self):
        self.values = {
            "media_scene": "Media",
            "default_scene": "Idle",
            "ndi_enabled": False,
            "ndi_source": "",
        }

    def media_window_scene(self):
        return self.values["media_scene"]

    def default_scene(self):
        return self.values["default_scene"]

    def has_media_window_scene(self):
        scene = self.media_window_scene()
        return bool(scene and not scene.startswith("—"))

    def ndi_enabled(self):
        return self.values["ndi_enabled"]

    def ndi_source(self):
        return self.values["ndi_source"].strip()

    def ndi_stream_configured(self):
        return self.ndi_enabled() and bool(self.ndi_source())


class _ObsServiceStub:
    def __init__(self):
        self.is_connected = True
        self.current_scene = "Camera"
        self.requested_scenes = []

    def request_scene_change(self, scene):
        self.requested_scenes.append(scene)
        self.current_scene = scene


class _ProjectionBarStub:
    def __init__(self):
        self.obs_btn_available = None
        self.obs_scene_states = []
        self._obs_scene_is_media = False

    def set_obs_btn_available(self, available):
        self.obs_btn_available = available

    def set_obs_scene_is_media(self, active):
        self._obs_scene_is_media = active
        self.obs_scene_states.append(active)

    def is_obs_scene_media(self):
        return self._obs_scene_is_media


class _QuickToolbarStub:
    def __init__(self):
        self.obs_stream_available = None
        self.obs_stream_active = None
        self.obs_connected = None
        self.obs_current_scene = None
        self.obs_scenes = None
        self.zoom_participants = None
        self.zoom_sharing = None

    def set_obs_stream_available(self, available):
        self.obs_stream_available = available

    def set_obs_stream_active(self, active):
        self.obs_stream_active = active

    def set_obs_connected(self, connected):
        self.obs_connected = connected

    def set_obs_current_scene(self, scene):
        self.obs_current_scene = scene

    def set_obs_scenes(self, scenes):
        self.obs_scenes = scenes

    def set_zoom_participants(self, count, names):
        self.zoom_participants = (count, names)

    def set_zoom_sharing(self, sharing):
        self.zoom_sharing = sharing


class _NotificationsStub:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message, **kwargs):
        self.errors.append((message, kwargs))

    def warning(self, message):
        self.warnings.append(message)


class _ProtectionStub:
    locked = False

    def allow_manual_projection_change(self, *, notify=True):
        return not self.locked


class _WindowStub:
    def __init__(self):
        self._obs_settings = _ObsSettingsStub()
        self._obs_service = _ObsServiceStub()
        self._quick_toolbar = _QuickToolbarStub()
        self.proj_bar = _ProjectionBarStub()
        self.projection_session = ProjectionSession()
        self.obs_scene_session = ObsSceneSession()
        self._ndi_service = SimpleNamespace(is_running=False)
        self._zoom_service = SimpleNamespace()
        self.media_ctrl = SimpleNamespace()
        self.stopped_projection = False
        self.browser_projection_stops = 0
        self.projection_statuses = []
        self.notifications = _NotificationsStub()
        self.playback_protection = _ProtectionStub()
        self.content_frames = []

    def _stop_projection(self):
        self.stopped_projection = True

    def stop_browser_tab_projection(self):
        self.browser_projection_stops += 1

    def update_projection_status(self, *args, **kwargs):
        self.projection_statuses.append((args, kwargs))

    def tr(self, text):
        return text


def _controller(window):
    return LiveIntegrationController(
        LiveIntegrationContext(
            projection_session=window.projection_session,
            obs_scene_session=window.obs_scene_session,
            obs_settings=window._obs_settings,
            obs_service=window._obs_service,
            ndi_service=window._ndi_service,
            zoom_service=window._zoom_service,
            notifications=window.notifications,
            projection_bar=window.proj_bar,
            media_controller=window.media_ctrl,
            quick_toolbar=lambda: window._quick_toolbar,
            projection_windows=lambda: [],
            translate=window.tr,
            playback_protection=window.playback_protection,
            content_frame_sink=window.content_frames.append,
        ),
        LiveIntegrationHandlers(
            stop_projection=window._stop_projection,
            stop_browser_tab_projection=window.stop_browser_tab_projection,
            update_projection_status=window.update_projection_status,
        ),
    )


def test_refresh_obs_btn_availability_requires_connection_and_media_scene():
    window = _WindowStub()
    controller = _controller(window)

    controller.refresh_obs_btn_availability()

    assert window.proj_bar.obs_btn_available is True

    window._obs_service.is_connected = False
    controller.refresh_obs_btn_availability()

    assert window.proj_bar.obs_btn_available is False


def test_live_stream_replacements_are_rejected_before_side_effects_when_locked():
    window = _WindowStub()
    window.playback_protection.locked = True
    window._obs_settings.values["ndi_enabled"] = True
    window._obs_settings.values["ndi_source"] = "Program"
    controller = _controller(window)

    controller.project_obs_ndi_stream()

    assert window.browser_projection_stops == 0
    assert window.projection_session.state == {"type": "idle"}
    assert window.stopped_projection is False


def test_refresh_obs_stream_availability_updates_toolbar_state():
    window = _WindowStub()
    window._obs_settings.values["ndi_enabled"] = True
    window._obs_settings.values["ndi_source"] = "Program"
    window.projection_session.set_state({"type": "obs_stream"})
    controller = _controller(window)

    controller.refresh_obs_stream_availability()

    assert window._quick_toolbar.obs_stream_available is True
    assert window._quick_toolbar.obs_stream_active is True


def test_quick_obs_return_scene_change_only_records_valid_return_scene():
    window = _WindowStub()
    window._obs_service.current_scene = "Media"
    controller = _controller(window)

    controller.on_quick_obs_return_scene_change("Camera")

    assert window.obs_scene_session.pre_media_scene == "Camera"

    controller.on_quick_obs_return_scene_change("Media")

    assert window.obs_scene_session.pre_media_scene == "Camera"


def test_obs_scene_toggle_moves_between_media_and_return_scene():
    window = _WindowStub()
    controller = _controller(window)

    controller.on_obs_scene_toggle()

    assert window.obs_scene_session.pre_media_scene == "Camera"
    assert window._obs_service.requested_scenes == ["Media"]
    assert window.proj_bar.obs_scene_states == [True]

    controller.on_obs_scene_toggle()

    assert window._obs_service.requested_scenes == ["Media", "Camera"]
    assert window.proj_bar.obs_scene_states == [True, False]


def test_zoom_share_error_uses_central_notifications():
    window = _WindowStub()
    controller = _controller(window)

    controller.on_zoom_share_error("Zoom window not found")

    assert window.notifications.errors == [
        (
            "Zoom window not found",
            {
                "title": "Zoom sharing failed",
                "dedupe_key": "zoom-share:Zoom window not found",
            },
        )
    ]


def test_zoom_updates_use_quick_toolbar_public_api():
    window = _WindowStub()
    controller = _controller(window)

    controller.on_zoom_participants_updated(3, ["A", "B", "C"])
    controller.on_zoom_sharing_state_changed(True)

    assert window._quick_toolbar.zoom_participants == (3, ["A", "B", "C"])
    assert window._quick_toolbar.zoom_sharing is True


def test_live_integration_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
