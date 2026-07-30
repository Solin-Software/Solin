import inspect

from solin.widgets.browser.tab import BrowserTab, ProjectableWebView


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

        # Capture keep-alive is orthogonal to the overlay behavior under test.
        def _unpark_native_surface(self):
            pass

        def _teardown_keepalive_host(self):
            pass

        def run_javascript(self, script: str):
            scripts.append(script)

    view = ViewDouble()

    ProjectableWebView.set_projection_active(view, True)
    ProjectableWebView.set_projection_active(view, False)

    assert "window.__solinMediaHoverOverlaysEnabled = false;" in scripts[0]
    assert "window.__solinSetMediaHoverOverlaysEnabled(false);" in scripts[0]
    assert "window.__solinMediaHoverOverlaysEnabled = true;" in scripts[1]
    assert "window.__solinSetMediaHoverOverlaysEnabled(true);" in scripts[1]


def test_projectable_webview_accepts_initial_shared_zoom_factor():
    params = inspect.signature(ProjectableWebView).parameters

    assert params["zoom_factor"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["zoom_factor"].default == 1.0


def test_projectable_webview_rejects_site_zoom_during_navigation():
    source = inspect.getsource(ProjectableWebView._on_native_zoom_factor_changed)

    assert "if self._navigation_in_progress:" in source
    assert "super().set_zoom_factor(self._shared_zoom_factor)" in source
    assert "self.user_zoom_factor_changed.emit(factor)" in source


def test_projectable_webview_forwards_native_user_zoom_requests():
    emitted = []

    class SignalDouble:
        def emit(self, factor):
            emitted.append(factor)

    view = type(
        "ProjectableViewDouble",
        (),
        {"user_zoom_factor_changed": SignalDouble()},
    )()

    ProjectableWebView._on_native_zoom_factor_requested(view, 1.25)

    assert emitted == [1.25]
