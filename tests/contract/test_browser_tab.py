import inspect

from solin.widgets.browser.tab import BrowserTab, ProjectableWebView
import native_webview_widget.widget as native_widget


def test_browser_tab_keeps_overlay_js_as_keyword_only_dependency():
    params = inspect.signature(BrowserTab).parameters
    assert params["overlay_js"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["overlay_js"].default == ""


def test_projectable_webview_disables_hover_overlays_while_projecting():
    scripts = []

    class ViewDouble:
        _projection_active = False

        def _push_overlay_enabled_state(self):
            ProjectableWebView._push_overlay_enabled_state(self)

        def run_javascript(self, script: str):
            scripts.append(script)

    view = ViewDouble()

    ProjectableWebView.set_projection_active(view, True)
    ProjectableWebView.set_projection_active(view, False)

    assert "window.__solinMediaHoverOverlaysEnabled = false;" in scripts[0]
    assert "window.__solinSetMediaHoverOverlaysEnabled(false);" in scripts[0]
    assert "window.__solinMediaHoverOverlaysEnabled = true;" in scripts[1]
    assert "window.__solinSetMediaHoverOverlaysEnabled(true);" in scripts[1]


def test_native_webview_allows_native_ancestors_for_webview_z_order():
    source = inspect.getsource(native_widget.NativeWebView.__init__)

    assert "WA_DontCreateNativeAncestors, False" in source


def test_native_webview_defers_show_creation_until_visible_size_is_valid():
    show_source = inspect.getsource(native_widget.NativeWebView.showEvent)
    ready_source = inspect.getsource(native_widget.NativeWebView._ensure_created_if_ready)

    assert "_schedule_ensure_created()" in show_source
    assert "not self.isVisible()" in ready_source
    assert "self.width() <= 0 or self.height() <= 0" in ready_source
