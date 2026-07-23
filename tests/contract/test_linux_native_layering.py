from __future__ import annotations

import inspect

from solin.controllers.lazy_page_controller import LazyPageController
from solin.widgets.browser.ui import BrowserUiMixin
from solin.widgets.browser.widget import BrowserWidget
from solin.widgets.projection.bar import ProjectionBar
from solin.widgets.quick_access_toolbar import (
    QuickAccessToolbar,
    _LinuxBrowserToolbarSurface,
)


def test_linux_browser_promotes_only_the_toolbar_to_a_transient_window():
    setup_source = inspect.getsource(QuickAccessToolbar.__init__)
    surface_source = inspect.getsource(_LinuxBrowserToolbarSurface.__init__)
    overlay_source = inspect.getsource(QuickAccessToolbar.set_browser_overlay_mode)
    lazy_surface_source = inspect.getsource(QuickAccessToolbar._ensure_browser_surface)
    projection_source = inspect.getsource(
        QuickAccessToolbar.set_projection_overlay_active
    )
    position_source = inspect.getsource(QuickAccessToolbar._anchor_point)
    corner_source = inspect.getsource(QuickAccessToolbar.set_browser_rect_mode)
    browser_ui_source = inspect.getsource(BrowserUiMixin._build_ui)

    assert "super().__init__(parent)" in setup_source
    assert "self.setParent(parent)" not in setup_source
    assert "WindowType.Tool" in surface_source
    assert "FramelessWindowHint" in surface_source
    assert "NoDropShadowWindowHint" in surface_source
    assert "WindowDoesNotAcceptFocus" in surface_source
    assert "self.setParent(parent_window, flags)" in surface_source
    assert "self.setParent" not in overlay_source
    assert "if not _LINUX" in overlay_source
    assert "self._ensure_browser_surface()" in overlay_source
    assert "browser_surface.show()" in overlay_source
    assert "browser_surface.hide()" in overlay_source
    assert "_LinuxBrowserToolbarSurface" in lazy_surface_source
    assert "self._bridge, self._anchor_window" in lazy_surface_source
    assert "surface.hide()" in projection_source
    assert "_restore_browser_surface_after_projection" in projection_source
    assert "mapToGlobal" in position_source
    assert "bool(enabled) and _WINDOWS" in corner_source
    assert "root.setContentsMargins(0, 0, 0, 0)" in browser_ui_source


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
