import threading

from solin.controllers.projection_integration_controller import (
    ProjectionIntegrationContext,
    ProjectionIntegrationController,
)
from solin.core.foundation.thread_workers import ThreadedWorkerPool
from solin.core.projection.application import ObsSceneSession, ProjectionSession


class _ObsSettingsStub:
    def __init__(self, *, media_scene="Media", default_scene="Idle"):
        self._media_scene = media_scene
        self._default_scene = default_scene

    def media_window_scene(self):
        return self._media_scene

    def default_scene(self):
        return self._default_scene


class _AutoShareSettingsStub:
    def __init__(self, *, enabled=False, hotkey="", click_position=(300, 250)):
        self._enabled = enabled
        self._hotkey = hotkey
        self._click_position = click_position

    def is_enabled(self):
        return self._enabled

    def hotkey(self):
        return self._hotkey

    def is_configured(self):
        x, y = self._click_position
        return (
            self._enabled
            and bool(self._hotkey)
            and x >= 0
            and y >= 0
        )

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
        self.auto_share_waits = 0
        self.auto_share_resolutions = []
        self.auto_share_cancellations = 0

    def set_obs_scene_is_media(self, active):
        self.obs_scene_states.append(active)

    def begin_auto_share_playback_wait(self):
        self.auto_share_waits += 1

    def resolve_auto_share_playback_wait(self, success):
        self.auto_share_resolutions.append(success)

    def cancel_auto_share_playback_wait(self):
        self.auto_share_cancellations += 1


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
        self.obs_scene_session = ObsSceneSession()
        self.proj_bar = _ProjectionBarStub()
        self._auto_key_projection = _AutoKeyProjectionStub()
        self._visible = visible
        self.started_shares = []
        self.stopped_shares = []
        self.mouse_interference_warnings = 0
        self.raised_projection_windows = []
        self.share_started = threading.Event()
        self.share_stopped = threading.Event()
        self.auto_share_workers = ThreadedWorkerPool()

    def _all_windows(self):
        return [_ProjectionWindowStub(self._visible)]

    def start_auto_share(self, hotkey, click_x, click_y, *, movement_warning=None):
        self.started_shares.append((hotkey, click_x, click_y))
        if movement_warning is not None:
            movement_warning()
        self.share_started.set()
        return True

    def stop_auto_share(self, hotkey):
        self.stopped_shares.append(hotkey)
        self.share_stopped.set()
        return True

    def raise_projection_window(self, window):
        self.raised_projection_windows.append(window)


def _controller(window):
    return ProjectionIntegrationController(
        ProjectionIntegrationContext(
            projection_session=window.projection_session,
            obs_scene_session=window.obs_scene_session,
            auto_key_projection=window._auto_key_projection,
            projection_windows=window._all_windows,
            obs_service=window._obs_service,
            obs_settings=window._obs_settings,
            auto_share_settings=window._auto_share_settings,
            projection_bar=window.proj_bar,
            auto_share_finished=lambda *_args: None,
            start_auto_share=window.start_auto_share,
            stop_auto_share=window.stop_auto_share,
            auto_share_mouse_interference_warning=(
                lambda: setattr(
                    window,
                    "mouse_interference_warnings",
                    window.mouse_interference_warnings + 1,
                )
            ),
            raise_projection_window=window.raise_projection_window,
            auto_share_workers=window.auto_share_workers,
        )
    )


class _ProjectionWindowStub:
    def __init__(self, visible):
        self._visible = visible

    def isVisible(self):
        return self._visible


class _ProjectionWindowRuntimeErrorStub:
    def isVisible(self):
        raise RuntimeError("deleted")


def test_current_projection_activity_distinguishes_idle_video_audio_and_visual():
    window = _WindowStub()
    controller = _controller(window)

    assert controller.current_projection_activity() == (False, True)

    window.projection_session.set_state({"type": "video", "is_audio": True})
    assert controller.current_projection_activity() == (True, False)

    window.projection_session.set_state({"type": "image"})
    assert controller.current_projection_activity() == (True, True)


def test_update_status_drives_auto_key_edges():
    window = _WindowStub(obs_connected=False)
    controller = _controller(window)

    controller.update_status(True, auto_keys_media=True, sync_obs=False)
    controller.update_status(False, sync_obs=False)

    assert window._auto_key_projection.visual_states == [True, False]


def test_sync_obs_scene_moves_to_media_and_remembers_previous_scene():
    window = _WindowStub(visible=True)
    controller = _controller(window)

    controller.sync_obs_scene(active=True, visual=True)

    assert window.obs_scene_session.pre_media_scene == "Camera"
    assert window._obs_service.requested_scenes == ["Media"]
    assert window.proj_bar.obs_scene_states == [True]


def test_sync_obs_scene_returns_to_previous_scene_from_media_scene():
    window = _WindowStub(visible=True)
    window._obs_service.current_scene = "Media"
    window.obs_scene_session.remember("Camera")
    controller = _controller(window)

    controller.sync_obs_scene(active=False, visual=True)

    assert window.obs_scene_session.pre_media_scene == ""
    assert window._obs_service.requested_scenes == ["Camera"]
    assert window.proj_bar.obs_scene_states == [False]


def test_auto_share_hotkey_uses_settings_store_value():
    window = _WindowStub()
    window._auto_share_settings = _AutoShareSettingsStub(
        enabled=True,
        hotkey="Ctrl+Shift+S",
    )
    controller = _controller(window)

    assert controller.auto_share_hotkey() == "Ctrl+Shift+S"
    assert controller.auto_share_configured() is True


def test_auto_share_configured_rejects_missing_hotkey():
    window = _WindowStub()
    window._auto_share_settings = _AutoShareSettingsStub(enabled=True, hotkey="")
    controller = _controller(window)

    assert controller.auto_share_hotkey() == ""
    assert controller.auto_share_configured() is False


def test_auto_share_configured_rejects_missing_target():
    window = _WindowStub()
    window._auto_share_settings = _AutoShareSettingsStub(
        enabled=True,
        hotkey="Alt+S",
        click_position=(-1, -1),
    )
    controller = _controller(window)

    assert controller.auto_share_configured() is False


def test_auto_share_uses_injected_share_actions():
    window = _WindowStub(visible=True)
    window._auto_share_settings = _AutoShareSettingsStub(
        enabled=True,
        hotkey="Alt+S",
        click_position=(300, 250),
    )
    controller = _controller(window)

    controller.sync_zoom_share(active=True, visual=True)
    assert window.share_started.wait(1)
    assert window.started_shares == [("Alt+S", 300, 250)]
    assert window.mouse_interference_warnings == 1

    controller.sync_zoom_share(active=False, visual=True)
    assert window.share_stopped.wait(1)
    assert window.stopped_shares == ["Alt+S"]
    controller.cleanup()


def test_visual_video_waits_for_pending_auto_share_and_resumes_on_success():
    window = _WindowStub(visible=True)
    window._auto_share_settings = _AutoShareSettingsStub(
        enabled=True,
        hotkey="Alt+S",
    )
    controller = _controller(window)

    assert controller.prepare_video_playback_for_auto_share() is True
    assert window.proj_bar.auto_share_waits == 1

    controller.sync_zoom_share(active=True, visual=True)
    generation = controller._auto_share_generation
    controller.on_auto_share_finished(generation, True, True)

    assert window.proj_bar.auto_share_resolutions == [True]
    assert controller._auto_share_start_pending is False
    controller.cleanup()


def test_failed_auto_share_releases_video_without_successful_resume():
    window = _WindowStub(visible=True)
    window._auto_share_settings = _AutoShareSettingsStub(
        enabled=True,
        hotkey="Alt+S",
    )
    controller = _controller(window)

    assert controller.prepare_video_playback_for_auto_share() is True
    controller.sync_zoom_share(active=True, visual=True)
    generation = controller._auto_share_generation
    controller.on_auto_share_finished(generation, True, False)

    assert window.proj_bar.auto_share_resolutions == [False]
    assert controller._auto_share_active is False
    controller.cleanup()


def test_video_does_not_wait_when_auto_share_is_already_confirmed():
    window = _WindowStub(visible=True)
    window._auto_share_settings = _AutoShareSettingsStub(
        enabled=True,
        hotkey="Alt+S",
    )
    controller = _controller(window)
    controller._auto_share_active = True
    controller._auto_share_start_pending = False

    assert controller.prepare_video_playback_for_auto_share() is False
    assert window.proj_bar.auto_share_waits == 0
    controller.cleanup()


def test_new_video_waits_while_existing_auto_share_start_is_pending():
    window = _WindowStub(visible=True)
    window._auto_share_settings = _AutoShareSettingsStub(
        enabled=True,
        hotkey="Alt+S",
    )
    controller = _controller(window)
    controller._auto_share_active = True
    controller._auto_share_start_pending = True

    assert controller.prepare_video_playback_for_auto_share() is True
    assert window.proj_bar.auto_share_waits == 1
    controller.cleanup()


def test_auto_share_ignores_stale_worker_result():
    window = _WindowStub()
    controller = _controller(window)
    controller._auto_share_generation = 2
    controller._auto_share_active = False

    controller.on_auto_share_finished(1, True, True)

    assert controller._auto_share_active is False


def test_auto_share_failed_stop_restores_active_state():
    window = _WindowStub()
    controller = _controller(window)
    controller._auto_share_generation = 3
    controller._auto_share_active = False

    controller.on_auto_share_finished(3, False, False)

    assert controller._auto_share_active is True


def test_cleanup_joins_owned_auto_share_workers():
    window = _WindowStub()
    controller = _controller(window)
    release = threading.Event()
    started = threading.Event()

    def worker():
        started.set()
        release.wait(1)

    controller._launch_auto_share_worker("share-test", worker)
    assert started.wait(1)
    release.set()

    controller.cleanup()

    assert window.auto_share_workers.is_stopped
    assert window.auto_share_workers.active_count == 0


def test_raise_visible_projection_windows_uses_injected_focus_action():
    window = _WindowStub()
    visible = _ProjectionWindowStub(True)
    hidden = _ProjectionWindowStub(False)
    window.projection_session.projection_windows[:] = [
        visible,
        hidden,
        _ProjectionWindowRuntimeErrorStub(),
    ]
    controller = _controller(window)

    controller.raise_visible_projection_windows()

    assert window.raised_projection_windows == [visible]


def test_projection_integration_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
