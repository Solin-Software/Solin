import inspect

import pytest

import solin.controllers.main_window_ui_controller as ui_controller_module
from solin.controllers.main_window_ui_controller import MainWindowUiController


def test_nav_button_specs_keep_page_indices_and_sidebar_order_stable(monkeypatch):
    monkeypatch.setattr(ui_controller_module, "NATIVE_SCENES_SUPPORTED", True)

    specs = MainWindowUiController.nav_button_specs()
    attr_names = [attr_name for attr_name, _icon, _label, _index in specs]
    page_indices = [index for _attr_name, _icon, _label, index in specs]

    assert page_indices == list(range(9))
    assert attr_names == [
        "nav_library_btn",
        "nav_meetings_btn",
        "nav_browser_btn",
        "nav_timer_btn",
        "nav_theme_btn",
        "nav_settings_btn",
        "nav_playlist_btn",
        "nav_wifi_btn",
        "nav_scenes_btn",
    ]
    assert MainWindowUiController.sidebar_layout_order() == (
        "nav_meetings_btn",
        "nav_browser_btn",
        "nav_scenes_btn",
        "nav_timer_btn",
        "nav_playlist_btn",
        "nav_library_btn",
        "nav_theme_btn",
        "nav_wifi_btn",
    )


def test_native_scenes_navigation_is_absent_on_unsupported_platforms(monkeypatch):
    monkeypatch.setattr(ui_controller_module, "NATIVE_SCENES_SUPPORTED", False)
    monkeypatch.setenv("SOLIN_SCENE_ENGINE", "native")

    attr_names = [
        attr_name
        for attr_name, _icon, _label, _index in MainWindowUiController.nav_button_specs()
    ]

    assert "nav_scenes_btn" not in attr_names
    assert "nav_scenes_btn" not in MainWindowUiController.sidebar_layout_order()


@pytest.mark.parametrize("selection", [None, "libobs"])
def test_libobs_scenes_navigation_is_available_on_all_platforms(monkeypatch, selection):
    monkeypatch.setattr(ui_controller_module, "NATIVE_SCENES_SUPPORTED", False)
    if selection is None:
        monkeypatch.delenv("SOLIN_SCENE_ENGINE", raising=False)
    else:
        monkeypatch.setenv("SOLIN_SCENE_ENGINE", selection)

    specs = MainWindowUiController.nav_button_specs()

    assert specs[-1][0] == "nav_scenes_btn"
    assert specs[-1][3] == 8
    assert "nav_scenes_btn" in MainWindowUiController.sidebar_layout_order()


def test_ui_controller_primes_native_hosts_before_browser_materializes():
    build_source = inspect.getsource(MainWindowUiController.build_ui)
    prime_source = inspect.getsource(MainWindowUiController._prime_native_cursor_hosts)

    assert "_prime_native_cursor_hosts(" in build_source
    assert "quick_toolbar.set_projection_overlay_active" in build_source
    assert "WA_NativeWindow" in prime_source
    assert "WA_DontCreateNativeAncestors" in prime_source
    assert "winId()" in prime_source
