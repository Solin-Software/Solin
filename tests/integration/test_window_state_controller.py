import uuid

from PySide6.QtCore import QByteArray

from solin.controllers.window_state_controller import (
    WindowStateContext,
    WindowStateController,
)
from solin.core.foundation.settings_keys import SettingsKey
from solin.core.profiles.settings import ProfileSettings
from solin.core.windowing.settings import WindowGeometrySettingsStore


class _WindowPortStub:
    def __init__(self):
        self.resized = None
        self.moved = None
        self.normal_shown = 0
        self.shown = 0
        self.raised = 0
        self.activated = 0
        self.minimized = False
        self.visible = True

    def width(self):
        return 1280

    def height(self):
        return 720

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


def _controller(window_port):
    return WindowStateController(
        WindowStateContext(
            width=window_port.width,
            height=window_port.height,
            move=window_port.move,
            is_minimized=window_port.is_minimized,
            show_normal=window_port.show_normal,
            is_visible=window_port.is_visible,
            show=window_port.show,
            raise_window=window_port.raise_window,
            activate_window=window_port.activate_window,
            win_id=window_port.win_id,
            titlebar_window=window_port,
        )
    )


def test_bring_to_front_uses_explicit_window_port():
    window = _WindowPortStub()
    window.visible = False
    controller = _controller(window)

    controller.bring_to_front()

    assert window.shown == 1
    assert window.raised == 1
    assert window.activated == 1


def test_window_state_controller_uses_explicit_dependencies():
    controller = _controller(_WindowPortStub())

    assert not hasattr(controller, "_window")


def test_window_geometry_settings_store_roundtrips_geometry():
    store = WindowGeometrySettingsStore.for_profile_settings(
        ProfileSettings.for_profile_id(f"window_geometry_{uuid.uuid4().hex}")
    )
    store.settings.clear()
    try:
        assert store.geometry().isEmpty()

        store.save_geometry(QByteArray(b"window-geometry"))

        assert store.geometry() == QByteArray(b"window-geometry")
    finally:
        store.settings.clear()


def test_window_geometry_settings_store_migrates_legacy_size() -> None:
    store = WindowGeometrySettingsStore.for_profile_settings(
        ProfileSettings.for_profile_id(f"window_geometry_{uuid.uuid4().hex}")
    )
    store.settings.clear()
    try:
        store.settings.set_value(SettingsKey.LEGACY_WINDOW_WIDTH, 1440, sync=False)
        store.settings.set_value(SettingsKey.LEGACY_WINDOW_HEIGHT, 900)

        assert store.initial_size(1200, 760) == (1440, 900)

        store.save_geometry(QByteArray(b"window-geometry"))

        assert SettingsKey.LEGACY_WINDOW_WIDTH not in store.settings.all_keys()
        assert SettingsKey.LEGACY_WINDOW_HEIGHT not in store.settings.all_keys()
    finally:
        store.settings.clear()


def test_window_geometry_settings_store_roundtrips_sidebar_collapsed():
    store = WindowGeometrySettingsStore.for_profile_settings(
        ProfileSettings.for_profile_id(f"window_geometry_{uuid.uuid4().hex}")
    )
    store.settings.clear()
    try:
        assert store.sidebar_collapsed() is False

        store.save_sidebar_collapsed(True)
        assert store.sidebar_collapsed() is True

        store.save_sidebar_collapsed(False)
        assert store.sidebar_collapsed() is False
    finally:
        store.settings.clear()
