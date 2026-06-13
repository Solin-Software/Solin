from solin.controllers.live_integration_controller import LiveIntegrationController


class _PrefsStub:
    def __init__(self, values=None):
        self.values = values or {}

    def value(self, key, default=None, _type=None):
        return self.values.get(key, default)


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
    def __init__(self, camera_option=None):
        self.camera_option = camera_option
        self.obs_stream_available = None
        self.obs_stream_active = None
        self.obs_camera_stream_available = None
        self.camera_enabled = None
        self.obs_connected = None
        self.obs_current_scene = None
        self.obs_scenes = None

    def current_camera_option(self):
        return self.camera_option

    def set_obs_stream_available(self, available):
        self.obs_stream_available = available

    def set_obs_stream_active(self, active):
        self.obs_stream_active = active

    def set_obs_camera_stream_available(self, available):
        self.obs_camera_stream_available = available

    def set_camera_enabled(self, enabled):
        self.camera_enabled = enabled

    def set_obs_connected(self, connected):
        self.obs_connected = connected

    def set_obs_current_scene(self, scene):
        self.obs_current_scene = scene

    def set_obs_scenes(self, scenes):
        self.obs_scenes = scenes


class _CameraServiceStub:
    def __init__(self, saved_option=None):
        self.saved_option = saved_option
        self.find_saved_args = None

    def find_saved(self, backend, name):
        self.find_saved_args = (backend, name)
        return self.saved_option


class _CameraOptionStub:
    def __init__(self, is_virtual):
        self.is_virtual = is_virtual


class _NotificationsStub:
    def __init__(self):
        self.errors = []

    def error(self, message, **kwargs):
        self.errors.append((message, kwargs))


class _WindowStub:
    def __init__(self):
        self._obs_prefs = _PrefsStub(
            {
                "obs/media_window_scene": "Media",
                "obs/default_scene": "Idle",
                "obs/ndi_enabled": False,
                "obs/ndi_source": "",
                "camera/enabled": True,
                "camera/backend": "dshow",
                "camera/device_name": "Cam",
            }
        )
        self._obs_service = _ObsServiceStub()
        self._camera_service = _CameraServiceStub()
        self._quick_toolbar = _QuickToolbarStub()
        self.proj_bar = _ProjectionBarStub()
        self._proj_state = {"type": "idle"}
        self._obs_pre_media_scene = ""
        self.stopped_projection = False
        self.notifications = _NotificationsStub()

    def _stop_projection(self):
        self.stopped_projection = True

    def tr(self, text):
        return text


def test_refresh_obs_btn_availability_requires_connection_and_media_scene():
    window = _WindowStub()
    controller = LiveIntegrationController(window)

    controller.refresh_obs_btn_availability()

    assert window.proj_bar.obs_btn_available is True

    window._obs_service.is_connected = False
    controller.refresh_obs_btn_availability()

    assert window.proj_bar.obs_btn_available is False


def test_refresh_obs_stream_availability_updates_stream_and_camera_availability():
    window = _WindowStub()
    window._obs_prefs.values["obs/ndi_enabled"] = True
    window._obs_prefs.values["obs/ndi_source"] = "Program"
    window._proj_state = {"type": "obs_stream"}
    controller = LiveIntegrationController(window)

    controller.refresh_obs_stream_availability()

    assert window._quick_toolbar.obs_stream_available is True
    assert window._quick_toolbar.obs_stream_active is True
    assert window._quick_toolbar.obs_camera_stream_available is False


def test_selected_camera_option_prefers_toolbar_option_then_saved_option():
    toolbar_option = _CameraOptionStub(is_virtual=True)
    saved_option = _CameraOptionStub(is_virtual=False)
    window = _WindowStub()
    window._quick_toolbar.camera_option = toolbar_option
    window._camera_service.saved_option = saved_option
    controller = LiveIntegrationController(window)

    assert controller.selected_camera_option() is toolbar_option

    window._quick_toolbar.camera_option = None
    assert controller.selected_camera_option() is saved_option
    assert window._camera_service.find_saved_args == ("dshow", "Cam")


def test_quick_obs_return_scene_change_only_records_valid_return_scene():
    window = _WindowStub()
    window._obs_service.current_scene = "Media"
    controller = LiveIntegrationController(window)

    controller.on_quick_obs_return_scene_change("Camera")

    assert window._obs_pre_media_scene == "Camera"

    controller.on_quick_obs_return_scene_change("Media")

    assert window._obs_pre_media_scene == "Camera"


def test_obs_scene_toggle_moves_between_media_and_return_scene():
    window = _WindowStub()
    controller = LiveIntegrationController(window)

    controller.on_obs_scene_toggle()

    assert window._obs_pre_media_scene == "Camera"
    assert window._obs_service.requested_scenes == ["Media"]
    assert window.proj_bar.obs_scene_states == [True]

    controller.on_obs_scene_toggle()

    assert window._obs_service.requested_scenes == ["Media", "Camera"]
    assert window.proj_bar.obs_scene_states == [True, False]


def test_camera_disabled_stops_active_camera_stream():
    window = _WindowStub()
    window._proj_state = {"type": "camera_stream"}
    controller = LiveIntegrationController(window)

    controller.on_camera_settings_enabled_toggled(False)

    assert window._quick_toolbar.camera_enabled is False
    assert window.stopped_projection is True


def test_zoom_share_error_uses_central_notifications():
    window = _WindowStub()
    controller = LiveIntegrationController(window)

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
