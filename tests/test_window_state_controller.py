import uuid

from solin.controllers.window_state_controller import WindowStateController
from solin.core.profiles.settings import ProfileSettings
from solin.core.ui.window_settings import WindowGeometrySettingsStore


def test_clamped_size_uses_minimums_for_invalid_saved_values():
    assert WindowStateController._clamped_size("100", "200", 900, 600) == (900, 600)


def test_clamped_size_preserves_values_above_minimums():
    assert WindowStateController._clamped_size("1280", "720", 900, 600) == (1280, 720)


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
