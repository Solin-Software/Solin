import threading

from solin.controllers.projection_integration_controller import ProjectionIntegrationController
from solin.core.projection.application import ProjectionSession


class _ObsSettingsStub:
    def __init__(self, *, media_scene="Media", default_scene="Idle"):
        self._media_scene = media_scene
        self._default_scene = default_scene

    def media_window_scene(self):
        return self._media_scene

    def default_scene(self):
        return self._default_scene


class _AutoShareSettingsStub:
    def __init__(self, *, enabled=False, hotkey="", click_position=(-1, -1)):
        self._enabled = enabled
        self._hotkey = hotkey
        self._click_position = click_position

    def is_enabled(self):
        return self._enabled

    def hotkey(self):
        return self._hotkey

    def is_configured(self):
        return self._enabled and bool(self._hotkey)

    def click_position(self):
        return self._click_position


class _ObsServiceStub:
    def __init__(self, connected=True, current_scene=""):
        self.is_connected = connected
        self.current_scene = current_scene
        self.requested_scenes = []

    def request_scene_change(self, scene):
        self.requested_scenes.append(scene)
        self.current_scene = scene


class _ProjectionBarStub:
    def __init__(self):
        self.obs_scene_states = []

    def set_obs_scene_is_media(self, active):
        self.obs_scene_states.append(active)


class _AutoKeyProjectionStub:
    def __init__(self):
        self.visual_states = []

    def set_visual_active(self, active):
        self.visual_states.append(active)


class _WindowStub:
    def __init__(self, *, visible=True, obs_connected=True):
        self.projection_session = ProjectionSession()
        self._obs_settings = _ObsSettingsStub()
        self._auto_share_settings = _AutoShareSettingsStub()
        self._obs_service = _ObsServiceStub(obs_connected, "Camera")
        self._obs_pre_media_scene = ""
        self.proj_bar = _ProjectionBarStub()
        self._auto_key_projection = _AutoKeyProjectionStub()
        self._visible = visible

    def _all_windows(self):
        return [_ProjectionWindowStub(self._visible)]


class _ProjectionWindowStub:
    def __init__(self, visible):
        self._visible = visible

    def isVisible(self):
        return self._visible


def test_current_projection_activity_distinguishes_idle_video_audio_and_visual():
    window = _WindowStub()
    controller = ProjectionIntegrationController(window)

    assert controller.current_projection_activity() == (False, True)

    window.projection_session.set_state({"type": "video", "is_audio": True})
    assert controller.current_projection_activity() == (True, False)

    window.projection_session.set_state({"type": "image"})
    assert controller.current_projection_activity() == (True, True)


def test_update_status_drives_auto_key_edges():
    window = _WindowStub(obs_connected=False)
    controller = ProjectionIntegrationController(window)

    controller.update_status(True, auto_keys_media=True, sync_obs=False)
    controller.update_status(False, sync_obs=False)

    assert window._auto_key_projection.visual_states == [True, False]


def test_sync_obs_scene_moves_to_media_and_remembers_previous_scene():
    window = _WindowStub(visible=True)
    controller = ProjectionIntegrationController(window)

    controller.sync_obs_scene(active=True, visual=True)

    assert window._obs_pre_media_scene == "Camera"
    assert window._obs_service.requested_scenes == ["Media"]
    assert window.proj_bar.obs_scene_states == [True]


def test_sync_obs_scene_returns_to_previous_scene_from_media_scene():
    window = _WindowStub(visible=True)
    window._obs_service.current_scene = "Media"
    window._obs_pre_media_scene = "Camera"
    controller = ProjectionIntegrationController(window)

    controller.sync_obs_scene(active=False, visual=True)

    assert window._obs_pre_media_scene == ""
    assert window._obs_service.requested_scenes == ["Camera"]
    assert window.proj_bar.obs_scene_states == [False]


def test_auto_share_hotkey_uses_settings_store_value():
    window = _WindowStub()
    window._auto_share_settings = _AutoShareSettingsStub(
        enabled=True,
        hotkey="Ctrl+Shift+S",
    )
    controller = ProjectionIntegrationController(window)

    assert controller.auto_share_hotkey() == "Ctrl+Shift+S"
    assert controller.auto_share_configured() is True


def test_auto_share_configured_rejects_missing_hotkey():
    window = _WindowStub()
    window._auto_share_settings = _AutoShareSettingsStub(enabled=True, hotkey="")
    controller = ProjectionIntegrationController(window)

    assert controller.auto_share_hotkey() == ""
    assert controller.auto_share_configured() is False


def test_auto_share_ignores_stale_worker_result():
    window = _WindowStub()
    controller = ProjectionIntegrationController(window)
    controller._auto_share_generation = 2
    controller._auto_share_active = False

    controller.on_auto_share_finished(1, True, True)

    assert controller._auto_share_active is False


def test_auto_share_failed_stop_restores_active_state():
    window = _WindowStub()
    controller = ProjectionIntegrationController(window)
    controller._auto_share_generation = 3
    controller._auto_share_active = False

    controller.on_auto_share_finished(3, False, False)

    assert controller._auto_share_active is True


def test_cleanup_joins_owned_auto_share_workers():
    window = _WindowStub()
    controller = ProjectionIntegrationController(window)
    release = threading.Event()
    started = threading.Event()

    def worker():
        started.set()
        release.wait(1)

    controller._launch_auto_share_worker("share-test", worker)
    assert started.wait(1)
    release.set()

    controller.cleanup()

    assert controller._auto_share_stop.is_set()
    assert controller._auto_share_threads == set()
