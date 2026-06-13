import inspect

from solin.controllers.main_window_ui_controller import MainWindowUiController


def test_nav_button_specs_keep_page_indices_and_sidebar_order_stable():
    specs = MainWindowUiController.nav_button_specs()
    attr_names = [attr_name for attr_name, _icon, _label, _index in specs]
    page_indices = [index for _attr_name, _icon, _label, index in specs]

    assert page_indices == list(range(10))
    assert attr_names == [
        "nav_songs_btn",
        "nav_meetings_btn",
        "nav_browser_btn",
        "nav_clips_btn",
        "nav_timer_btn",
        "nav_theme_btn",
        "nav_settings_btn",
        "nav_playlist_btn",
        "nav_cache_btn",
        "nav_wifi_btn",
    ]
    assert MainWindowUiController.sidebar_layout_order() == (
        "nav_meetings_btn",
        "nav_browser_btn",
        "nav_timer_btn",
        "nav_playlist_btn",
        "nav_songs_btn",
        "nav_clips_btn",
        "nav_theme_btn",
        "nav_cache_btn",
        "nav_wifi_btn",
    )


def test_ui_controller_primes_native_hosts_before_browser_materializes():
    build_source = inspect.getsource(MainWindowUiController.build_ui)
    prime_source = inspect.getsource(MainWindowUiController._prime_native_cursor_hosts)

    assert "_prime_native_cursor_hosts()" in build_source
    assert "WA_NativeWindow" in prime_source
    assert "WA_DontCreateNativeAncestors" in prime_source
    assert "winId()" in prime_source
