import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget

from solin.bootstrap.application_window import ApplicationWindow
from solin.controllers.main_window_ui_controller import MainWindowUiController
from solin.widgets.common.collapsible_sidebar import SIDEBAR_COLLAPSED_WIDTH, SIDEBAR_EXPANDED_WIDTH


def test_nav_button_specs_keep_page_indices_and_sidebar_order_stable():
    specs = MainWindowUiController.nav_button_specs()
    attr_names = [attr_name for attr_name, _icon, _label, _index in specs]
    page_indices = [index for _attr_name, _icon, _label, index in specs]

    assert page_indices == list(range(10))
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
        "nav_meeting_console_btn",
    ]
    assert MainWindowUiController.sidebar_layout_order() == (
        "nav_meetings_btn",
        "nav_browser_btn",
        "nav_scenes_btn",
        "nav_meeting_console_btn",
        "nav_timer_btn",
        "nav_playlist_btn",
        "nav_library_btn",
        "nav_theme_btn",
        "nav_wifi_btn",
    )


@pytest.mark.parametrize("selection", [None, "libobs", "native", "invalid"])
def test_scenes_navigation_ignores_retired_engine_selection(monkeypatch, selection):
    if selection is None:
        monkeypatch.delenv("SOLIN_SCENE_ENGINE", raising=False)
    else:
        monkeypatch.setenv("SOLIN_SCENE_ENGINE", selection)

    specs = MainWindowUiController.nav_button_specs()

    scenes = next(spec for spec in specs if spec[0] == "nav_scenes_btn")
    assert scenes[3] == 8
    assert "nav_scenes_btn" in MainWindowUiController.sidebar_layout_order()


def test_ui_controller_primes_native_hosts_before_browser_materializes():
    build_source = inspect.getsource(MainWindowUiController.build_ui)
    prime_source = inspect.getsource(MainWindowUiController._prime_native_cursor_hosts)

    assert "_prime_native_cursor_hosts(" in build_source
    assert "quick_toolbar.set_projection_overlay_active" in build_source
    assert "WA_NativeWindow" in prime_source
    assert "WA_DontCreateNativeAncestors" in prime_source
    assert "winId()" in prime_source


@pytest.mark.parametrize("collapsed", [False, True])
@pytest.mark.parametrize("width", [460, 559, 560, 700])
@pytest.mark.parametrize("geometry_state", ["fresh", "normal", "maximized"])
def test_sidebar_uses_host_width_before_and_after_content_installation(
    request, collapsed, width, geometry_state,
):
    def create_window(initial_width, geometry):
        window = ApplicationWindow(
            width=initial_width, height=700, geometry=geometry,
            save_geometry=lambda _geometry: None, pending_files=[],
        )
        request.addfinalizer(window.deleteLater)
        request.addfinalizer(window.close)
        return window

    geometry = QByteArray()
    if geometry_state != "fresh":
        source = create_window(width, geometry)
        if geometry_state == "maximized":
            source.showMaximized()
            QApplication.processEvents()
        geometry = source.saveGeometry()
    window = create_window(width if geometry_state == "fresh" else 700, geometry)
    runtime = QWidget(window.content_parent())
    settings = Mock()
    settings.sidebar_collapsed.return_value = collapsed
    context = SimpleNamespace(
        parent=runtime, translate=lambda text: text,
        active_profile_id="test", active_profile_name="Test",
        window_geometry_settings=settings,
    )
    builder = MainWindowUiController(
        context, SimpleNamespace(profile_switch_requested=lambda: None),
        media_info_queue_factory=Mock(), media_info_service_factory=Mock(),
        browser_download_service_factory=Mock(), browser_image_fetch_service_factory=Mock(),
    )
    sidebar = builder._build_sidebar(Mock(), [])
    runtime_layout = QHBoxLayout(runtime)
    runtime_layout.setContentsMargins(0, 0, 0, 0)
    runtime_layout.setSpacing(0)
    runtime_layout.addWidget(sidebar.frame)
    runtime_layout.addWidget(QWidget(runtime), stretch=1)
    request.addfinalizer(sidebar.sidebar_chrome._animation.stop)

    if geometry_state != "maximized":
        assert window.width() == width
    assert sidebar.sidebar_chrome.is_collapsed() is (collapsed or window.width() < 560)
    window._content_stack.addWidget(runtime)
    window._content_stack.setCurrentWidget(runtime)
    window.show()
    QApplication.processEvents()
    assert sidebar.sidebar_chrome.is_collapsed() is (collapsed or window.width() < 560)
    if geometry_state == "maximized":
        assert window.isMaximized()
        window.showNormal()
        QApplication.processEvents()

    for target_width in (460, 559, 560, 700):
        window.resize(target_width, 700)
        QApplication.processEvents()
        expected_collapsed = collapsed or target_width < 560
        assert window.width() == target_width
        assert sidebar.sidebar_chrome.is_collapsed() is expected_collapsed
        assert sidebar.frame.width() == (
            SIDEBAR_COLLAPSED_WIDTH if expected_collapsed else SIDEBAR_EXPANDED_WIDTH
        )
    settings.save_sidebar_collapsed.assert_not_called()
