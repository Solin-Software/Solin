from __future__ import annotations

import inspect

from solin.controllers.lazy_page_controller import LazyPageController
from solin.widgets.browser.ui import BrowserUiMixin
from solin.widgets.browser.widget import BrowserWidget
from solin.widgets.projection.bar import ProjectionBar
from solin.widgets.quick_access_toolbar import QuickAccessToolbar


def test_linux_browser_reserves_a_non_overlapping_toolbar_band():
    setup_source = inspect.getsource(QuickAccessToolbar.__init__)
    browser_ui_source = inspect.getsource(BrowserUiMixin._build_ui)

    assert "self.setParent(parent)" in setup_source
    assert "WindowType.Tool" not in setup_source
    assert "browser_toolbar_reserve = 56" in browser_ui_source
    assert "root.setContentsMargins(0, 0, 0, browser_toolbar_reserve)" in browser_ui_source


def test_projection_overlay_occludes_only_the_foreign_browser_surface():
    setup_source = inspect.getsource(ProjectionBar._build_overlay)
    geometry_source = inspect.getsource(ProjectionBar._update_overlay_geometry)
    occlusion_source = inspect.getsource(BrowserWidget.set_native_views_occluded)
    lazy_source = inspect.getsource(
        LazyPageController.set_browser_native_views_occluded
    )

    assert "self.overlay = QWidget(parent)" in setup_source
    assert "WindowType.Tool" not in setup_source
    assert "mapToGlobal" not in geometry_source
    assert "self.overlay.setGeometry(0, 0" in geometry_source
    assert "set_native_surface_visible" in occlusion_source
    assert "set_native_views_occluded" in lazy_source
