import uuid

from solin.controllers.window_state_controller import (
    WindowStateContext,
    WindowStateController,
)
from solin.core.profiles.settings import ProfileSettings
from solin.ui.window_settings import WindowGeometrySettingsStore


class _GeometrySettingsStub:
    def __init__(self):
        self.saved = None

    def size(self, _default_width, _default_height):
        return 100, 200

    def save_size(self, width, height):
        self.saved = (width, height)


class _WindowPortStub:
    def __init__(self):
        self.resized = None
        self.moved = None
        self.icon = None
        self.normal_shown = 0
        self.shown = 0
        self.raised = 0
        self.activated = 0
        self.minimized = False
        self.visible = True

    def minimum_width(self):
        return 900

    def minimum_height(self):
        return 600

    def resize(self, width, height):
        self.resized = (width, height)

    def width(self):
        return 1280

    def height(self):
        return 720

    def set_window_icon(self, icon):
        self.icon = icon

    def move(self, x, y):
        self.moved = (x, y)

    def is_minimized(self):
        return self.minimized

    def show_normal(self):
        self.normal_shown += 1

    def is_visible(self):
        return self.visible

    def show(self):
        self.shown += 1

    def raise_window(self):
        self.raised += 1

    def activate_window(self):
        self.activated += 1

    def win_id(self):
        return 1


def _controller(window_port, geometry_settings):
    return WindowStateController(
        WindowStateContext(
            minimum_width=window_port.minimum_width,
            minimum_height=window_port.minimum_height,
            resize=window_port.resize,
            width=window_port.width,
            height=window_port.height,
            set_window_icon=window_port.set_window_icon,
            move=window_port.move,
            is_minimized=window_port.is_minimized,
            show_normal=window_port.show_normal,
            is_visible=window_port.is_visible,
            show=window_port.show,
            raise_window=window_port.raise_window,
            activate_window=window_port.activate_window,
            win_id=window_port.win_id,
            titlebar_window=window_port,
        ),
        geometry_settings,
    )


def test_clamped_size_uses_minimums_for_invalid_saved_values():
    assert WindowStateController._clamped_size("100", "200", 900, 600) == (900, 600)


def test_clamped_size_preserves_values_above_minimums():
    assert WindowStateController._clamped_size("1280", "720", 900, 600) == (1280, 720)


def test_restore_and_save_size_use_explicit_window_port():
    window = _WindowPortStub()
    settings = _GeometrySettingsStub()
    controller = _controller(window, settings)

    controller.restore_size()
    controller.save_size()

    assert window.resized == (900, 600)
    assert settings.saved == (1280, 720)


def test_bring_to_front_uses_explicit_window_port():
    window = _WindowPortStub()
    window.visible = False
    controller = _controller(window, _GeometrySettingsStub())

    controller.bring_to_front()

    assert window.shown == 1
    assert window.raised == 1
    assert window.activated == 1


def test_window_state_controller_uses_explicit_dependencies():
    controller = _controller(_WindowPortStub(), _GeometrySettingsStub())

    assert not hasattr(controller, "_window")


def test_window_geometry_settings_store_roundtrips_size():
    store = WindowGeometrySettingsStore.for_profile_settings(
        ProfileSettings.for_profile_id(f"window_geometry_{uuid.uuid4().hex}")
    )
    store.settings.clear()
    try:
        assert store.size(1200, 760) == (1200, 760)

        store.save_size(1440, 900)

        assert store.size(1200, 760) == (1440, 900)
    finally:
        store.settings.clear()
