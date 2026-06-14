import pytest

from solin.controllers.projection_stop_controller import ProjectionStopController
from solin.core.projection.application import ProjectionSession


class _NavigationStub:
    def __init__(self, raises=False):
        self.raises = raises
        self.stopped = 0

    def stop_browser_tab_projection(self):
        self.stopped += 1
        if self.raises:
            raise RuntimeError("browser already gone")


class _ServiceStub:
    def __init__(self):
        self.stopped = 0
        self.stopped_later = 0

    def stop(self):
        self.stopped += 1

    def stop_later(self):
        self.stopped_later += 1


class _ProjectionBarStub:
    def __init__(self, visual_active=False):
        self.visual_active = visual_active
        self.deactivated = 0

    def is_visual_media_active(self):
        return self.visual_active

    def deactivate(self):
        self.deactivated += 1


class _ProjectionIntegrationsStub:
    def __init__(self, auto_share=False):
        self.auto_share = auto_share
        self.statuses = []

    def update_status(self, *args, **kwargs):
        self.statuses.append((args, kwargs))

    def auto_share_configured(self):
        return self.auto_share


class _LiveIntegrationsStub:
    def __init__(self):
        self.obs_active = []
        self.camera_active = []

    def set_obs_stream_active(self, active):
        self.obs_active.append(active)

    def set_camera_stream_active(self, active):
        self.camera_active.append(active)


class _ProjectionWindowStub:
    def __init__(self):
        self.cleared = 0

    def clear(self):
        self.cleared += 1


class _FloatingPreviewStub:
    def __init__(self):
        self.zoom_breaks = 0

    def trigger_zoom_break(self):
        self.zoom_breaks += 1


class _WindowStub:
    def __init__(
        self,
        *,
        state_type="image",
        tab_active=False,
        visual_active=False,
        nav_raises=False,
        auto_share=False,
        floating_preview=True,
    ):
        self.projection_session = ProjectionSession()
        self.projection_session.set_state({"type": state_type})
        self.projection_session.set_tab_projection_active(tab_active)
        self._navigation = _NavigationStub(raises=nav_raises)
        self.media_ctrl = _ServiceStub()
        self._ndi_service = _ServiceStub()
        self._camera_service = _ServiceStub()
        self.proj_bar = _ProjectionBarStub(visual_active=visual_active)
        self._projection_integrations = _ProjectionIntegrationsStub(auto_share=auto_share)
        self._live_integrations = _LiveIntegrationsStub()
        self.windows = [_ProjectionWindowStub(), _ProjectionWindowStub()]
        self.projection_session.floating_preview_window = (
            _FloatingPreviewStub() if floating_preview else None
        )

    def _all_windows(self):
        return self.windows


def test_stop_any_clears_projection_and_triggers_zoom_break_for_visual_state():
    window = _WindowStub(state_type="image", tab_active=True)
    controller = ProjectionStopController(window)

    controller.stop_any()

    assert window.projection_session.tab_projection_active is False
    assert window._navigation.stopped == 1
    assert window.media_ctrl.stopped == 1
    assert window._ndi_service.stopped == 1
    assert window._ndi_service.stopped_later == 0
    assert window._camera_service.stopped == 1
    assert [projection_window.cleared for projection_window in window.windows] == [1, 1]
    assert window.proj_bar.deactivated == 1
    assert window.projection_session.state == {"type": "idle"}
    assert window._live_integrations.obs_active == [False]
    assert window._live_integrations.camera_active == [False]
    assert window.projection_session.floating_preview_window.zoom_breaks == 1
    assert window._projection_integrations.statuses == [
        ((False,), {"sync_obs": True})
    ]


def test_stop_projection_uses_delayed_ndi_stop_for_obs_stream():
    window = _WindowStub(state_type="obs_stream", auto_share=True)
    controller = ProjectionStopController(window)

    controller.stop_projection()

    assert window._ndi_service.stopped == 0
    assert window._ndi_service.stopped_later == 1
    assert window.projection_session.floating_preview_window.zoom_breaks == 0
    assert window._projection_integrations.statuses == [
        ((False,), {"sync_obs": False})
    ]


def test_stop_projection_swallows_navigation_errors():
    window = _WindowStub(state_type="video", nav_raises=True)
    controller = ProjectionStopController(window)

    controller.stop_projection()

    assert window._navigation.stopped == 1
    assert window.projection_session.state == {"type": "idle"}


def test_stop_any_preserves_navigation_errors():
    window = _WindowStub(state_type="video", nav_raises=True)
    controller = ProjectionStopController(window)

    with pytest.raises(RuntimeError):
        controller.stop_any()
