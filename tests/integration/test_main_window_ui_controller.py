import inspect

from solin.controllers.main_window_ui_controller import MainWindowUiController


def test_nav_button_specs_keep_page_indices_and_sidebar_order_stable():
    specs = MainWindowUiController.nav_button_specs(scenes_enabled=True)
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
    assert MainWindowUiController.sidebar_layout_order(scenes_enabled=True) == (
        "nav_meetings_btn",
        "nav_browser_btn",
        "nav_scenes_btn",
        "nav_timer_btn",
        "nav_playlist_btn",
        "nav_library_btn",
        "nav_theme_btn",
        "nav_wifi_btn",
    )


def test_native_scenes_navigation_is_absent_when_disabled():
    attr_names = [
        attr_name
        for attr_name, _icon, _label, _index in MainWindowUiController.nav_button_specs(scenes_enabled=False)
    ]

    assert "nav_scenes_btn" not in attr_names
    assert "nav_scenes_btn" not in MainWindowUiController.sidebar_layout_order(scenes_enabled=False)


def test_ui_controller_primes_native_hosts_before_browser_materializes():
    build_source = inspect.getsource(MainWindowUiController.build_ui)
    prime_source = inspect.getsource(MainWindowUiController._prime_native_cursor_hosts)

    assert "_prime_native_cursor_hosts(" in build_source
    assert "quick_toolbar.set_projection_overlay_active" in build_source
    assert "WA_NativeWindow" in prime_source
    assert "WA_DontCreateNativeAncestors" in prime_source
    assert "winId()" in prime_source
