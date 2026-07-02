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
        self.browser_download_service = object()
        self.browser_download_service_factory = (
            lambda: self.browser_download_service
        )
        self.browser_image_fetch_service = object()
        self.browser_image_fetch_service_factory = (
            lambda: self.browser_image_fetch_service
        )
        self.projection_aspect_ratio_provider = lambda: object()
        self.browser_settings = object()
        self.playback_protection = object()
        self.profile_media_store = object()
        self.jwpub_import_thread_factory = object()
        self.document_conversion_service = object()
        self.cache_scan_session_factory = object()
        self.qr_generation_session_factory = object()
        self.wifi_receive_server_factory = lambda _parent: object()
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


class _ThemeAwareWidgetStub:
    def __init__(self):
        self.theme_applied = False
        self.updated = False

    def apply_theme(self):
        self.theme_applied = True

    def update(self):
        self.updated = True


class _SignalStub:
    def __init__(self):
        self.connected = []

    def connect(self, callback):
        self.connected.append(callback)


class _BrowserSignalStub(_BrowserStub):
    def __init__(self):
        super().__init__()
        self.project_image_signal = _SignalStub()
        self.project_video_signal = _SignalStub()
        self.stop_projection_signal = _SignalStub()
        self.project_tab_pixmap_signal = _SignalStub()
        self.stop_tab_projection_signal = _SignalStub()
        self.media_destination_signal = _SignalStub()


def _controller(window, *, project_video=None):
    if project_video is None:
        project_video = lambda *_args: None
    return LazyPageController(
        LazyPageContext(
            parent=window,
            stack=window.stack,
            lang_manager=window.lang,
            notifications=window.notifications,
            profile_paths=window.profile_paths,
            browser_settings=window.browser_settings,
            media_cache_manager=window.media_cache_manager,
            profile_media_store=window.profile_media_store,
            jwpub_import_thread_factory=window.jwpub_import_thread_factory,
            document_conversion_service=window.document_conversion_service,
            cache_scan_session_factory=window.cache_scan_session_factory,
            qr_generation_session_factory=window.qr_generation_session_factory,
            wifi_receive_server_factory=window.wifi_receive_server_factory,
            browser_download_service_factory=(
                window.browser_download_service_factory
            ),
            browser_image_fetch_service_factory=(
                window.browser_image_fetch_service_factory
            ),
            projection_aspect_ratio_provider=window.projection_aspect_ratio_provider,
            media_info_service_factory=window.media_info_service_factory,
            playback_protection=window.playback_protection,
        ),
        LazyPageHandlers(
            project_image=lambda *_args: None,
            project_video=project_video,
            stop_projection=lambda: None,
            project_tab_frame=lambda *_args: None,
            browser_media_destination=lambda *_args: None,
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


def test_lazy_page_controller_adapts_browser_video_signal_to_projection_contract():
    window = _WindowStub()
    calls = []
    controller = _controller(window, project_video=lambda *args: calls.append(args))
    browser = _BrowserSignalStub()
    controller._browser_widget = browser

    controller._connect_browser_signals()
    browser.project_video_signal.connected[0]("https://example.test/video.mp4", "Talk")

    assert calls == [
        (
            "https://example.test/video.mp4",
            "Talk",
            [
                {
                    "url": "https://example.test/video.mp4",
                    "title": "Talk",
                    "type": "video",
                }
            ],
            None,
        )
    ]


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


def test_lazy_page_controller_applies_theme_to_materialized_pages_only(monkeypatch):
    window = _WindowStub()
    controller = _controller(window)
    browser = _ThemeAwareWidgetStub()
    cache = _ThemeAwareWidgetStub()
    controller._browser_widget = browser
    controller._cache_manager_widget = cache

    def fail_if_materialized():
        raise AssertionError("apply_theme should not materialize lazy pages")

    for name in (
        "ensure_browser_widget",
        "ensure_cache_manager_widget",
        "ensure_wifi_receive_widget",
    ):
        monkeypatch.setattr(controller, name, fail_if_materialized)

    controller.apply_theme()

    assert browser.theme_applied is True
    assert browser.updated is True
    assert cache.theme_applied is True
    assert cache.updated is True
    assert controller.wifi_receive_widget is None


def test_lazy_page_controller_builds_browser_without_window_parent(monkeypatch):
    import solin.widgets.browser.widget as browser_module

    class _BrowserFactory:
        def __init__(
            self,
            lang_manager,
            *,
            profile_paths,
            zoom_settings,
            download_service,
            image_fetch_service,
            aspect_ratio_provider,
            playback_protection,
            parent=None,
        ):
            self.lang_manager = lang_manager
            self.profile_paths = profile_paths
            self.zoom_settings = zoom_settings
            self.download_service = download_service
            self.image_fetch_service = image_fetch_service
            self.aspect_ratio_provider = aspect_ratio_provider
            self.playback_protection = playback_protection
            self.parent = parent

    window = _WindowStub()
    controller = _controller(window)
    monkeypatch.setattr(browser_module, "BrowserWidget", _BrowserFactory)
    monkeypatch.setattr(controller, "_connect_browser_signals", lambda: None)

    browser = controller.ensure_browser_widget()

    assert browser is controller.browser_widget
    assert browser.lang_manager is window.lang
    assert browser.profile_paths is window.profile_paths
    assert browser.zoom_settings is window.browser_settings
    assert browser.download_service is window.browser_download_service
    assert browser.image_fetch_service is window.browser_image_fetch_service
    assert browser.aspect_ratio_provider is window.projection_aspect_ratio_provider
    assert browser.playback_protection is window.playback_protection
    assert browser.parent is None


def test_lazy_page_controller_builds_wifi_with_injected_jwpub_factory(monkeypatch):
    import solin.widgets.wifi_receive_widget as wifi_module

    class _WifiFactory:
        def __init__(
            self,
            lang_manager,
            *,
            notifications,
            document_conversion_service,
            profile_media_store,
            jwpub_import_thread_factory,
            qr_generation_session_factory,
            wifi_receive_server_factory,
            media_info_service_factory,
            parent,
        ):
            self.lang_manager = lang_manager
            self.notifications = notifications
            self.document_conversion_service = document_conversion_service
            self.profile_media_store = profile_media_store
            self.jwpub_import_thread_factory = jwpub_import_thread_factory
            self.qr_generation_session_factory = qr_generation_session_factory
            self.wifi_receive_server_factory = wifi_receive_server_factory
            self.media_info_service_factory = media_info_service_factory
            self.parent = parent

    window = _WindowStub()
    controller = _controller(window)
    monkeypatch.setattr(wifi_module, "WifiReceiveWidget", _WifiFactory)
    monkeypatch.setattr(controller, "_connect_wifi_receive_signals", lambda: None)

    wifi = controller.ensure_wifi_receive_widget()

    assert wifi is controller.wifi_receive_widget
    assert wifi.lang_manager is window.lang
    assert wifi.notifications is window.notifications
    assert (
        wifi.document_conversion_service
        is window.document_conversion_service
    )
    assert wifi.profile_media_store is window.profile_media_store
    assert (
        wifi.jwpub_import_thread_factory
        is window.jwpub_import_thread_factory
    )
    assert (
        wifi.qr_generation_session_factory
        is window.qr_generation_session_factory
    )
    assert wifi.wifi_receive_server_factory is window.wifi_receive_server_factory
    assert wifi.media_info_service_factory is window.media_info_service_factory
    assert wifi.parent is window


def test_lazy_page_controller_builds_cache_with_injected_scan_factory(monkeypatch):
    import solin.widgets.cache_manager_widget as cache_module

    class _CacheFactory:
        def __init__(
            self,
            lang_manager,
            media_cache_manager,
            *,
            cache_scan_session_factory,
            media_info_service_factory,
            parent,
        ):
            self.lang_manager = lang_manager
            self.media_cache_manager = media_cache_manager
            self.cache_scan_session_factory = cache_scan_session_factory
            self.media_info_service_factory = media_info_service_factory
            self.parent = parent

    window = _WindowStub()
    controller = _controller(window)
    monkeypatch.setattr(cache_module, "CacheManagerWidget", _CacheFactory)
    monkeypatch.setattr(controller, "_connect_cache_manager_signals", lambda: None)

    cache = controller.ensure_cache_manager_widget()

    assert cache is controller.cache_manager_widget
    assert cache.lang_manager is window.lang
    assert cache.media_cache_manager is window.media_cache_manager
    assert cache.cache_scan_session_factory is window.cache_scan_session_factory
    assert cache.media_info_service_factory is window.media_info_service_factory
    assert cache.parent is window
