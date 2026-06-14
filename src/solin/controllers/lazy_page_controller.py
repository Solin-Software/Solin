from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QStackedWidget, QWidget

if TYPE_CHECKING:
    from solin.core.foundation.runtime_paths import ProfilePaths, RuntimePaths
    from solin.core.media.cache import MediaCacheManager
    from solin.widgets.media_info_extractor import MediaInfoService


@dataclass(frozen=True, slots=True)
class LazyPageContext:
    """Stable services required to materialize heavyweight pages."""

    parent: QWidget
    stack: QStackedWidget
    lang_manager: Any
    notifications: Any
    profile_paths: ProfilePaths
    runtime_paths: RuntimePaths
    media_cache_manager: MediaCacheManager
    media_info_service_factory: Callable[[QObject], MediaInfoService]


@dataclass(frozen=True, slots=True)
class LazyPageHandlers:
    """Application callbacks consumed by lazily materialized pages."""

    project_image: Callable[..., None]
    project_video: Callable[..., None]
    stop_projection: Callable[[], None]
    project_tab_frame: Callable[..., None]
    add_current_to_playlist: Callable[..., None]
    add_downloaded_file_to_playlist: Callable[..., None]
    report_download_failure: Callable[..., None]
    play_cached_media: Callable[..., None]
    wifi_media_received: Callable[..., None]
    wifi_add_single: Callable[..., None]
    wifi_add_all: Callable[..., None]
    wifi_play: Callable[..., None]


class LazyPageController:
    """Owns lazy page state, creation, lifecycle, and signal wiring."""

    BROWSER_INDEX = 2
    CACHE_INDEX = 8
    WIFI_INDEX = 9

    def __init__(
        self,
        context: LazyPageContext,
        handlers: LazyPageHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._browser_widget = None
        self._cache_manager_widget = None
        self._wifi_receive_widget = None
        self._browser_signals_connected = False
        self._cache_signals_connected = False
        self._wifi_signals_connected = False

    @property
    def browser_widget(self):
        return self._browser_widget

    @property
    def cache_manager_widget(self):
        return self._cache_manager_widget

    @property
    def wifi_receive_widget(self):
        return self._wifi_receive_widget

    def placeholder(self) -> QWidget:
        page = QWidget(self._context.parent)
        page.setObjectName("LazyPage")
        return page

    def ensure_page(self, index: int) -> None:
        if index == self.BROWSER_INDEX:
            self.ensure_browser_widget()
        elif index == self.CACHE_INDEX:
            self.ensure_cache_manager_widget()
        elif index == self.WIFI_INDEX:
            self.ensure_wifi_receive_widget()

    def ensure_browser_widget(self):
        if self._browser_widget is not None:
            return self._browser_widget

        from ..widgets.browser.widget import BrowserWidget

        context = self._context
        self._browser_widget = BrowserWidget(
            context.lang_manager,
            profile_paths=context.profile_paths,
            media_cache_manager=context.media_cache_manager,
        )
        self._replace_stack_widget(self.BROWSER_INDEX, self._browser_widget)
        self._connect_browser_signals()
        return self._browser_widget

    def ensure_cache_manager_widget(self):
        if self._cache_manager_widget is not None:
            return self._cache_manager_widget

        from ..widgets.cache_manager_widget import CacheManagerWidget

        context = self._context
        self._cache_manager_widget = CacheManagerWidget(
            context.lang_manager,
            context.media_cache_manager,
            media_info_service_factory=context.media_info_service_factory,
            parent=context.parent,
        )
        self._replace_stack_widget(self.CACHE_INDEX, self._cache_manager_widget)
        self._connect_cache_manager_signals()
        return self._cache_manager_widget

    def ensure_wifi_receive_widget(self):
        if self._wifi_receive_widget is not None:
            return self._wifi_receive_widget

        from ..widgets.wifi_receive_widget import WifiReceiveWidget

        context = self._context
        self._wifi_receive_widget = WifiReceiveWidget(
            context.lang_manager,
            notifications=context.notifications,
            profile_paths=context.profile_paths,
            runtime_paths=context.runtime_paths,
            media_info_service_factory=context.media_info_service_factory,
            parent=context.parent,
        )
        self._replace_stack_widget(self.WIFI_INDEX, self._wifi_receive_widget)
        self._connect_wifi_receive_signals()
        return self._wifi_receive_widget

    def stop_browser_tab_projection(self) -> None:
        if self._browser_widget is not None:
            self._browser_widget._stop_tab_projection_internal()

    def cleanup_browser(self) -> None:
        if self._browser_widget is not None:
            self._browser_widget.cleanup_browser()

    def _replace_stack_widget(self, index: int, widget: QWidget) -> None:
        stack = self._context.stack
        old = stack.widget(index)
        stack.insertWidget(index, widget)
        if old is not None:
            stack.removeWidget(old)
            old.deleteLater()

    def _connect_browser_signals(self) -> None:
        browser = self._browser_widget
        if self._browser_signals_connected or browser is None:
            return

        handlers = self._handlers
        browser.project_image_signal.connect(handlers.project_image)
        browser.project_video_signal.connect(handlers.project_video)
        browser.stop_projection_signal.connect(handlers.stop_projection)
        browser.project_tab_pixmap_signal.connect(handlers.project_tab_frame)
        browser.stop_tab_projection_signal.connect(handlers.stop_projection)
        browser.add_to_playlist_signal.connect(
            lambda url, title, media_type: handlers.add_current_to_playlist(
                url,
                title,
                {"type": media_type},
            )
        )
        browser.add_downloaded_file_to_playlist_signal.connect(
            handlers.add_downloaded_file_to_playlist
        )
        browser.download_failed_signal.connect(handlers.report_download_failure)
        self._browser_signals_connected = True

    def _connect_cache_manager_signals(self) -> None:
        cache_manager = self._cache_manager_widget
        if self._cache_signals_connected or cache_manager is None:
            return

        cache_manager.play_media_requested.connect(self._handlers.play_cached_media)
        self._cache_signals_connected = True

    def _connect_wifi_receive_signals(self) -> None:
        wifi_receive = self._wifi_receive_widget
        if self._wifi_signals_connected or wifi_receive is None:
            return

        handlers = self._handlers
        wifi_receive.media_received.connect(handlers.wifi_media_received)
        wifi_receive.request_add_to_playlist.connect(handlers.wifi_add_single)
        wifi_receive.request_add_all_to_playlist.connect(handlers.wifi_add_all)
        wifi_receive.request_play.connect(handlers.wifi_play)
        self._wifi_signals_connected = True
