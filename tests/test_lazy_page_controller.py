from solin.controllers.lazy_page_controller import (
    LazyPageContext,
    LazyPageController,
    LazyPageHandlers,
)
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.media.cache import MediaCacheManager


class _WidgetStub:
    def __init__(self):
        self.deleted = False

    def deleteLater(self):
        self.deleted = True


class _StackStub:
    def __init__(self):
        self._widgets = [_WidgetStub() for _ in range(10)]

    def widget(self, index):
        return self._widgets[index]

    def insertWidget(self, index, widget):
        self._widgets.insert(index, widget)

    def removeWidget(self, widget):
        self._widgets.remove(widget)

    def count(self):
        return len(self._widgets)


class _WindowStub:
    def __init__(self):
        self.stack = _StackStub()
        self.lang = object()
        self.notifications = object()
        self.media_cache_manager = MediaCacheManager(
            "cache/media",
            downloader_factory=lambda _parent: None,
        )
        self.media_info_queue_factory = lambda _parent: object()
        self.media_info_service_factory = lambda _parent: object()
        self.profile_paths = ProfilePaths.from_roots(
            data_dir="data",
            cache_dir="cache",
            profile_id="main",
        )


class _BrowserStub:
    def __init__(self):
        self.stopped = False
        self.cleaned = False

    def _stop_tab_projection_internal(self):
        self.stopped = True

    def cleanup_browser(self):
        self.cleaned = True


def _controller(window):
    return LazyPageController(
        LazyPageContext(
            parent=window,
            stack=window.stack,
            lang_manager=window.lang,
            notifications=window.notifications,
            profile_paths=window.profile_paths,
            runtime_paths=object(),
            media_cache_manager=window.media_cache_manager,
            media_info_service_factory=window.media_info_service_factory,
        ),
        LazyPageHandlers(
            project_image=lambda *_args: None,
            project_video=lambda *_args: None,
            stop_projection=lambda: None,
            project_tab_frame=lambda *_args: None,
            add_current_to_playlist=lambda *_args: None,
            add_downloaded_file_to_playlist=lambda *_args: None,
            report_download_failure=lambda *_args: None,
            play_cached_media=lambda *_args: None,
            wifi_media_received=lambda *_args: None,
            wifi_add_single=lambda *_args: None,
            wifi_add_all=lambda *_args: None,
            wifi_play=lambda *_args: None,
        ),
    )


def test_lazy_page_controller_owns_lazy_widget_state():
    window = _WindowStub()

    controller = _controller(window)

    assert controller.browser_widget is None
    assert controller.cache_manager_widget is None
    assert controller.wifi_receive_widget is None
    assert not hasattr(controller, "_window")


def test_lazy_page_controller_replaces_stack_placeholder_in_place():
    window = _WindowStub()
    controller = _controller(window)

    old_widget = window.stack.widget(LazyPageController.BROWSER_INDEX)
    replacement = _WidgetStub()
    controller._replace_stack_widget(LazyPageController.BROWSER_INDEX, replacement)

    assert window.stack.count() == 10
    assert window.stack.widget(LazyPageController.BROWSER_INDEX) is replacement
    assert old_widget.deleted is True


def test_lazy_page_controller_delegates_browser_lifecycle():
    window = _WindowStub()
    controller = _controller(window)
    browser = _BrowserStub()
    controller._browser_widget = browser

    controller.stop_browser_tab_projection()
    controller.cleanup_browser()

    assert browser.stopped is True
    assert browser.cleaned is True


def test_lazy_page_controller_routes_known_stack_indices(monkeypatch):
    window = _WindowStub()
    controller = _controller(window)
    calls = []

    monkeypatch.setattr(
        controller,
        "ensure_browser_widget",
        lambda: calls.append("browser"),
    )
    monkeypatch.setattr(
        controller,
        "ensure_cache_manager_widget",
        lambda: calls.append("cache"),
    )
    monkeypatch.setattr(
        controller,
        "ensure_wifi_receive_widget",
        lambda: calls.append("wifi"),
    )

    controller.ensure_page(LazyPageController.BROWSER_INDEX)
    controller.ensure_page(LazyPageController.CACHE_INDEX)
    controller.ensure_page(LazyPageController.WIFI_INDEX)
    controller.ensure_page(999)

    assert calls == ["browser", "cache", "wifi"]


def test_lazy_page_controller_builds_browser_without_window_parent(monkeypatch):
    import solin.widgets.browser.widget as browser_module

    class _BrowserFactory:
        def __init__(
            self,
            lang_manager,
            *,
            profile_paths,
            media_cache_manager,
            parent=None,
        ):
            self.lang_manager = lang_manager
            self.profile_paths = profile_paths
            self.media_cache_manager = media_cache_manager
            self.parent = parent

    window = _WindowStub()
    controller = _controller(window)
    monkeypatch.setattr(browser_module, "BrowserWidget", _BrowserFactory)
    monkeypatch.setattr(controller, "_connect_browser_signals", lambda: None)

    browser = controller.ensure_browser_widget()

    assert browser is controller.browser_widget
    assert browser.lang_manager is window.lang
    assert browser.profile_paths is window.profile_paths
    assert browser.media_cache_manager is window.media_cache_manager
    assert browser.parent is None
