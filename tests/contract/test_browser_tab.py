import inspect

import shiboken6
from PySide6 import QtWidgets

from native_webview_widget import NativeWebView
from native_webview_widget._backend import NativeBackend
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


def test_native_webview_emits_only_effective_native_zoom_changes():
    emitted = []

    class SignalDouble:
        def emit(self, factor):
            emitted.append(factor)

    class ViewDouble:
        _created = True
        _disposed = False
        _zoom_factor = 1.0
        zoomFactorChanged = SignalDouble()

        def _validated_zoom_factor(self, factor):
            return NativeWebView._validated_zoom_factor(factor)

    view = ViewDouble()

    NativeWebView._handle_native_event(
        view,
        NativeBackend.EVENT_ZOOM_FACTOR_CHANGED,
        "1.000000",
    )
    NativeWebView._handle_native_event(
        view,
        NativeBackend.EVENT_ZOOM_FACTOR_CHANGED,
        "1.250000",
    )

    assert emitted == [1.25]


def test_native_webview_allows_native_ancestors_for_webview_z_order():
    source = inspect.getsource(native_widget.NativeWebView.__init__)

    assert "WA_DontCreateNativeAncestors, False" in source


def test_native_webview_defers_show_creation_until_visible_size_is_valid():
    show_source = inspect.getsource(native_widget.NativeWebView.showEvent)
    ready_source = inspect.getsource(native_widget.NativeWebView._ensure_created_if_ready)

    assert "_schedule_ensure_created()" in show_source
    assert "not self.isVisible()" in ready_source
    assert "self.width() <= 0 or self.height() <= 0" in ready_source


def test_native_webview_uses_foreign_window_container_on_linux():
    source = inspect.getsource(native_widget.NativeWebView._ensure_created)

    assert 'qt_platform != "xcb"' in source
    assert "QWindow.fromWinId" in source
    assert "QWidget.createWindowContainer" in source


def test_native_webview_dispose_is_terminal_and_idempotent(monkeypatch):
    calls: list[tuple[str, int]] = []

    class FakeBackend:
        uses_foreign_window = False

        def stop_frame_stream(self, handle):
            calls.append(("stop", handle))

        def destroy(self, handle):
            calls.append(("destroy", handle))

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(native_widget, "NativeBackend", FakeBackend)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = NativeWebView()
    view._handle = 7
    view._created = True

    view.dispose()
    view.dispose()

    assert calls == [("stop", 7), ("destroy", 7)]
    app.processEvents()


def test_parent_destruction_disposes_native_webview(monkeypatch):
    destroyed_handles: list[int] = []

    class FakeBackend:
        uses_foreign_window = False

        def stop_frame_stream(self, _handle):
            pass

        def destroy(self, handle):
            destroyed_handles.append(handle)

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr(native_widget, "NativeBackend", FakeBackend)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    parent = QtWidgets.QWidget()
    view = NativeWebView(parent)
    view._handle = 11
    view._created = True

    shiboken6.delete(parent)

    assert destroyed_handles == [11]
    app.processEvents()
