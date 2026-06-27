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


def test_browser_zoom_change_persists_once_and_updates_other_tabs(monkeypatch):
    class ViewDouble:
        def __init__(self):
            self.applied = []

        def set_zoom_factor(self, factor):
            self.applied.append(factor)

    class TabDouble:
        def __init__(self):
            self.view = ViewDouble()

    class StackDouble:
        def __init__(self, tabs):
            self.tabs = tabs

        def count(self):
            return len(self.tabs)

        def widget(self, index):
            return self.tabs[index]

    class SettingsDouble:
        def __init__(self):
            self.saved = []

        def set_zoom_factor(self, factor):
            self.saved.append(factor)

    source = TabDouble()
    sibling = TabDouble()
    settings = SettingsDouble()
    widget = type(
        "WidgetDouble",
        (),
        {
            "_browser_zoom_factor": 1.0,
            "_zoom_settings": settings,
            "_stack": StackDouble([source, sibling]),
        },
    )()
    monkeypatch.setattr(browser_widget, "BrowserTab", TabDouble)

    browser_widget.BrowserWidget._on_browser_zoom_factor_changed(
        widget,
        source,
        1.5,
    )
    browser_widget.BrowserWidget._on_browser_zoom_factor_changed(
        widget,
        source,
        1.5,
    )

    assert widget._browser_zoom_factor == 1.5
    assert settings.saved == [1.5]
    assert source.view.applied == []
    assert sibling.view.applied == [1.5]
