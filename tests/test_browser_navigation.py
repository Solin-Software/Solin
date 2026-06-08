import app.widgets.browser.widget as browser_widget
from app.widgets.browser.navigation import _BrowserNavigationMixin


def test_browser_widget_uses_navigation_mixin():
    assert browser_widget._BrowserNavigationMixin is _BrowserNavigationMixin
    assert issubclass(browser_widget.BrowserWidget, _BrowserNavigationMixin)
    assert browser_widget.BrowserWidget._new_tab is _BrowserNavigationMixin._new_tab
    assert (
        browser_widget.BrowserWidget._url_from_mime_data
        is _BrowserNavigationMixin._url_from_mime_data
    )


def test_browser_widget_exposes_overlay_js_for_tab_creation():
    assert browser_widget.BrowserWidget._OVERLAY_JS is browser_widget.OVERLAY_JS
