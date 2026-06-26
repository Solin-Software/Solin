import solin.widgets.browser.widget as browser_widget
from solin.widgets.browser.navigation import BrowserNavigationMixin


def test_browser_widget_uses_navigation_mixin():
    assert browser_widget.BrowserNavigationMixin is BrowserNavigationMixin
    assert issubclass(browser_widget.BrowserWidget, BrowserNavigationMixin)
    assert browser_widget.BrowserWidget._new_tab is BrowserNavigationMixin._new_tab
    assert (
        browser_widget.BrowserWidget._url_from_mime_data
        is BrowserNavigationMixin._url_from_mime_data
    )


def test_browser_widget_exposes_overlay_js_for_tab_creation():
    assert browser_widget.BrowserWidget._OVERLAY_JS is browser_widget.OVERLAY_JS


def test_browser_overlay_js_has_live_projection_hover_gate():
    overlay_js = browser_widget.OVERLAY_JS

    assert "__solinSetMediaHoverOverlaysEnabled" in overlay_js
    assert "function mediaHoverOverlaysEnabled()" in overlay_js
    assert "function setOverlayBarVisible(bar, visible)" in overlay_js
    assert "if (!mediaHoverOverlaysEnabled()) return;" in overlay_js
