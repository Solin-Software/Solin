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


def test_browser_overlay_js_keeps_the_hover_bar_outside_the_page():
    overlay_js = browser_widget.OVERLAY_JS

    assert "document.body.appendChild(_bar);" in overlay_js
    assert "'position:fixed'" in overlay_js
    assert "function mediaFromPoint(x, y)" in overlay_js
    assert "document.addEventListener('mouseover', onPointerOver, true);" in overlay_js


def test_browser_overlay_js_context_menu_uses_the_hover_media_pair():
    overlay_js = browser_widget.OVERLAY_JS

    # The {image, video} pair resolved by elementsFromPoint feeds the entire menu.
    assert "function ctxItemsFor(media)" in overlay_js
    assert (
        "if (media.image) _addCtxGroup(items, seen, imageUrlOf(media.image), 'image');"
        in overlay_js
    )
    assert (
        "if (media.video) _addCtxGroup(items, seen, videoUrlOf(media.video), 'video');"
        in overlay_js
    )

    # Toolbar button clicks reuse the pair; media clicks resolve it at the click point.
    assert "var media = (_bar && _bar.contains(e.target) && _barMedia)" in overlay_js
    assert ": mediaFromPoint(e.clientX, e.clientY);" in overlay_js

    # Media is not rediscovered by walking the DOM.
    assert "_gatherCtxItems" not in overlay_js
    assert "_gatherFromElement" not in overlay_js


def test_browser_overlay_js_resolves_relative_image_urls():
    overlay_js = browser_widget.OVERLAY_JS

    # Relative src (wol.jw.org) must become absolute before the HTTP filter;
    # otherwise the media menu does not open and the native menu takes over.
    assert "function absMediaUrl(url)" in overlay_js
    assert "var abs = new URL(url, document.baseURI).href;" in overlay_js

    # All three attribute-to-candidate-URL conversions use the helper.
    assert "return m ? absMediaUrl(m[1]) : '';" in overlay_js
    assert (
        "var url = absMediaUrl((img.getAttribute('src') || img.currentSrc || '')"
        ".split('?')[0]);" in overlay_js
    )
    assert (
        "|| absMediaUrl((el.getAttribute('src') || el.currentSrc || '')"
        ".split('?')[0]);" in overlay_js
    )

    # The old chain stopped at relative src and never reached currentSrc.
    assert "|| (el.getAttribute('src') || '').split('?')[0]" not in overlay_js


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

    class UrlBarDouble:
        def __init__(self):
            self.applied = []

        def set_zoom_factor(self, factor):
            self.applied.append(factor)

    source = TabDouble()
    sibling = TabDouble()
    settings = SettingsDouble()
    url_bar = UrlBarDouble()
    widget = type(
        "WidgetDouble",
        (),
        {
            "_browser_zoom_factor": 1.0,
            "_zoom_settings": settings,
            "_url_bar": url_bar,
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
    assert url_bar.applied == [1.5]
    assert source.view.applied == []
    assert sibling.view.applied == [1.5]
