import solin.widgets.browser.widget as browser_widget
from solin.core.projection.aspect_ratio import DEFAULT_PROJECTION_ASPECT_RATIO
from solin.widgets.browser.aspect_frame import AspectRatioViewFrame
from solin.widgets.browser.crop_overlay import CropOverlay
from solin.ui.browser.native_adapters import (
    HistoryAdapter,
    NativePageAdapter,
    UrlValue,
)
from solin.ui.browser.scripts import CURSOR_SPOTLIGHT_JS, CURSOR_SPOTLIGHT_REMOVE_JS
from solin.core.network.browser_urls import normalize_browser_input


def test_normalize_browser_input_keeps_known_urls():
    assert normalize_browser_input("https://example.com/path") == "https://example.com/path"
    assert normalize_browser_input("about:blank") == "about:blank"


def test_normalize_browser_input_repairs_file_urls():
    assert normalize_browser_input("file:C:/Temp/a.html") == "file:///C:/Temp/a.html"
    assert normalize_browser_input("file://C:/Temp/a.html") == "file:///C:/Temp/a.html"


def test_normalize_browser_input_searches_plain_text_when_requested():
    assert normalize_browser_input("kingdom song", search_if_text=True) == (
        "https://www.google.com/search?q=kingdom%20song"
    )


def test_normalize_browser_input_adds_scheme_for_domains_and_localhost():
    assert normalize_browser_input("example.com") == "https://example.com"
    assert normalize_browser_input("localhost") == "https://localhost"
    assert normalize_browser_input("localhost:8000") == "localhost:8000"


def test_normalize_browser_input_converts_existing_relative_path(tmp_path, monkeypatch):
    local_file = tmp_path / "index.html"
    local_file.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert normalize_browser_input("./index.html") == local_file.resolve().as_uri()


def test_browser_overlay_widgets_keep_geometry_defaults():
    assert CropOverlay._MIN_DRAG == 8
    assert hasattr(AspectRatioViewFrame, "set_aspect_ratio_lock")
    assert DEFAULT_PROJECTION_ASPECT_RATIO.label == "16:9"


def test_native_adapters_preserve_old_browser_surface():
    class _View:
        def __init__(self):
            self.scripts = []

        def can_go_back(self):
            return True

        def can_go_forward(self):
            return False

        def run_javascript(self, script):
            self.scripts.append(script)

        def zoom_factor(self):
            return 1.25

    view = _View()

    assert UrlValue("https://example.test").toString() == "https://example.test"
    assert HistoryAdapter(view).canGoBack() is True
    assert HistoryAdapter(view).canGoForward() is False
    NativePageAdapter(view).runJavaScript("1 + 1")
    assert view.scripts == ["1 + 1"]
    assert NativePageAdapter(view).zoomFactor() == 1.25


def test_browser_widget_uses_shared_cursor_spotlight_scripts():
    assert browser_widget.BrowserWidget._CURSOR_SPOTLIGHT_JS is CURSOR_SPOTLIGHT_JS
    assert browser_widget.BrowserWidget._CURSOR_SPOTLIGHT_REMOVE_JS is CURSOR_SPOTLIGHT_REMOVE_JS
    assert "__solinCursorSpotlight" in CURSOR_SPOTLIGHT_JS
