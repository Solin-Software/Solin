from solin.controllers.window_state_controller import WindowStateController


def test_clamped_size_uses_minimums_for_invalid_saved_values():
    assert WindowStateController._clamped_size("100", "200", 900, 600) == (900, 600)


def test_clamped_size_preserves_values_above_minimums():
    assert WindowStateController._clamped_size("1280", "720", 900, 600) == (1280, 720)
