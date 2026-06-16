from PySide6.QtCore import QDateTime

from solin.controllers.timer_theme_controller import (
    TimerThemeContext,
    TimerThemeController,
    TimerThemeHandlers,
)
from solin.core.projection.application import ProjectionSession


class _NavigationStub:
    def __init__(self):
        self.stopped = 0

    def stop_browser_tab_projection(self):
        self.stopped += 1


class _ServiceStub:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class _ProjectionBarStub:
    def __init__(self):
        self.playlists = []
        self.timers = []
        self.images = []

    def set_playlist(self, playlist):
        self.playlists.append(playlist)

    def activate_timer(self, target_dt):
        self.timers.append(target_dt)

    def activate_image(self, title, image_data=None):
        self.images.append((title, image_data))


class _ProjectionIntegrationsStub:
    def __init__(self):
        self.statuses = []

    def update_status(self, *args, **kwargs):
        self.statuses.append((args, kwargs))


class _ProjectionWindowStub:
    def __init__(self):
        self.timers = []
        self.timer_updates = []
        self.blinks = []
        self.themes = []

    def show_timer(self, remaining, total):
        self.timers.append((remaining, total))

    def update_timer(self, remaining, total):
        self.timer_updates.append((remaining, total))

    def set_timer_blink(self, on):
        self.blinks.append(on)

    def show_sermon_theme(self, text, subtitle):
        self.themes.append((text, subtitle))


class _WindowStub:
    def __init__(self):
        self.projection_session = ProjectionSession()
        self.projection_session.set_tab_projection_active(True)
        self._navigation = _NavigationStub()
        self.media_ctrl = _ServiceStub()
        self._ndi_service = _ServiceStub()
        self._camera_service = _ServiceStub()
        self.proj_bar = _ProjectionBarStub()
        self._projection_integrations = _ProjectionIntegrationsStub()
        self.projection_session.set_state({"type": "video"})
        self.windows = [_ProjectionWindowStub(), _ProjectionWindowStub()]

    def _all_windows(self):
        return self.windows

    def tr(self, text):
        return text


def _controller(window):
    return TimerThemeController(
        TimerThemeContext(
            projection_session=window.projection_session,
            projection_bar=window.proj_bar,
            media_controller=window.media_ctrl,
            ndi_service=window._ndi_service,
            camera_service=window._camera_service,
            projection_windows=window._all_windows,
            translate=window.tr,
        ),
        TimerThemeHandlers(
            stop_browser_tab_projection=(
                window._navigation.stop_browser_tab_projection
            ),
            update_projection_status=(
                window._projection_integrations.update_status
            ),
        ),
    )


def test_start_timer_stops_active_sources_and_broadcasts_timer():
    window = _WindowStub()
    controller = _controller(window)
    controller._remaining_seconds = lambda _target_dt: 42
    target_dt = QDateTime.currentDateTime().addSecs(60)

    controller.start_timer(target_dt)

    assert window.projection_session.tab_projection_active is False
    assert window._navigation.stopped == 1
    assert window.media_ctrl.stopped == 1
    assert window._ndi_service.stopped == 1
    assert window._camera_service.stopped == 1
    assert window.proj_bar.playlists == [[]]
    assert window.proj_bar.timers == [target_dt]
    assert [projection_window.timers for projection_window in window.windows] == [
        [(42, 42)],
        [(42, 42)],
    ]
    assert window.projection_session.state == {
        "type": "timer",
        "target_dt": target_dt,
        "total": 42,
    }
    args, kwargs = window._projection_integrations.statuses[0]
    assert args[0] is True
    assert args[1].startswith("Cronômetro → ")
    assert kwargs == {"auto_keys_media": False}


def test_timer_update_and_blink_are_broadcast_to_all_projection_windows():
    window = _WindowStub()
    controller = _controller(window)

    controller.on_timer_update_proj(7, 30)
    controller.on_timer_blink_proj(True)

    assert [projection_window.timer_updates for projection_window in window.windows] == [
        [(7, 30)],
        [(7, 30)],
    ]
    assert [projection_window.blinks for projection_window in window.windows] == [
        [True],
        [True],
    ]


def test_project_sermon_theme_renders_preview_and_updates_projection_state():
    window = _WindowStub()
    controller = _controller(window)
    controller._render_sermon_theme_preview = lambda text, subtitle: b"preview"
    text = "A theme long enough to be shortened in the projection bar"

    controller.project_sermon_theme(text)

    assert window.projection_session.tab_projection_active is False
    assert window.proj_bar.playlists == [[]]
    assert [projection_window.themes for projection_window in window.windows] == [
        [(text, "PUBLIC TALK")],
        [(text, "PUBLIC TALK")],
    ]
    assert window.proj_bar.images == [(text[:28] + "…", b"preview")]
    assert window.projection_session.state == {
        "type": "sermon_theme",
        "text": text,
        "subtitle": "PUBLIC TALK",
        "transform": (1.0, 0.0, 0.0),
    }
    args, kwargs = window._projection_integrations.statuses[0]
    assert args == (True, text[:28] + "…")
    assert kwargs == {"auto_keys_media": False}


def test_timer_theme_controller_uses_explicit_dependencies():
    controller = _controller(_WindowStub())

    assert not hasattr(controller, "_window")
