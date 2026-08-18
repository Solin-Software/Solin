from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QStackedWidget, QWidget


def load_browser_widget_type():
    from ..widgets.browser.widget import BrowserWidget

    return BrowserWidget


if TYPE_CHECKING:
    from solin.core.foundation.runtime_paths import ProfilePaths
    from solin.ui.qr_generation import QrGenerationSessionFactory
    from solin.core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from solin.core.media.browser_downloads import BrowserDownloadService
    from solin.core.media.profile_store import ProfileMediaStore
    from solin.core.ingest.wifi_server import WifiReceiveServer
    from solin.core.network.browser_images import BrowserImageFetchService
    from solin.core.network.browser_settings import BrowserZoomSettings
    from solin.core.rendering.document_conversion import DocumentConversionService
    from solin.ui.media_info import MediaInfoService


@dataclass(frozen=True, slots=True)
class LazyPageContext:
    """Stable services required to materialize heavyweight pages."""

    parent: QWidget
    stack: QStackedWidget
    lang_manager: Any
    notifications: Any
    profile_paths: ProfilePaths
    browser_settings: BrowserZoomSettings
    profile_media_store: ProfileMediaStore
    jwpub_import_thread_factory: JwpubImportThreadFactory
    document_conversion_service: DocumentConversionService
    qr_generation_session_factory: QrGenerationSessionFactory
    wifi_receive_server_factory: Callable[[QObject], WifiReceiveServer]
    browser_download_service_factory: Callable[[], BrowserDownloadService]
    browser_image_fetch_service_factory: Callable[[], BrowserImageFetchService]
    projection_aspect_ratio_provider: Callable[[], Any]
    media_info_service_factory: Callable[[QObject], MediaInfoService]
    playback_protection: Any


@dataclass(frozen=True, slots=True)
class LazyPageHandlers:
    """Application callbacks consumed by lazily materialized pages."""

    project_image: Callable[..., None]
    project_video: Callable[..., None]
    stop_projection: Callable[[], None]
    project_tab_frame: Callable[..., None]
    browser_media_destination: Callable[..., None]
    wifi_media_received: Callable[..., None]
    wifi_add_single: Callable[..., None]
    wifi_add_all: Callable[..., None]
    wifi_play: Callable[..., None]


class LazyPageController:
    """Owns lazy page state, creation, lifecycle, and signal wiring."""

    BROWSER_INDEX = 2
    WIFI_INDEX = 7

    def __init__(
        self,
        context: LazyPageContext,
        handlers: LazyPageHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers
        self._browser_widget = None
        self._wifi_receive_widget = None
        self._browser_preparation = None
        self._browser_widget_type = None
        self._browser_installed: Callable[[], None] = lambda: None
        self._browser_requested = False
        self._browser_signals_connected = False
        self._wifi_signals_connected = False

    @property
    def browser_widget(self):
        return self._browser_widget

    @property
    def wifi_receive_widget(self):
        return self._wifi_receive_widget

    def placeholder(self) -> QWidget:
        page = QWidget(self._context.parent)
        page.setObjectName("LazyPage")
        return page

    def ensure_page(self, index: int) -> None:
        if index == self.BROWSER_INDEX:
            self._request_browser_widget()
        elif index == self.WIFI_INDEX:
            self.ensure_wifi_receive_widget()

    def set_browser_preparation(self, preparation) -> None:
        self._browser_preparation = preparation
        preparation.completed.connect(self._materialize_requested_browser)
        preparation.failed.connect(lambda _message: self._materialize_requested_browser())

    def set_browser_widget_type(self, widget_type) -> None:
        self._browser_widget_type = widget_type

    def set_browser_installed_callback(self, callback: Callable[[], None]) -> None:
        self._browser_installed = callback

    def _request_browser_widget(self) -> None:
        preparation = self._browser_preparation
        if preparation is None or preparation.is_terminal:
            self.ensure_browser_widget()
            return
        self._browser_requested = True
        preparation.start()

    def _materialize_requested_browser(self) -> None:
        if self._browser_requested:
            self.ensure_browser_widget()

    def apply_theme(self) -> None:
        """Refresh already-materialized lazy pages without creating new ones."""

        for widget in (
            self._browser_widget,
            self._wifi_receive_widget,
        ):
            if widget is None:
                continue
            if hasattr(widget, "apply_theme"):
                widget.apply_theme()
            if hasattr(widget, "update"):
                widget.update()

    def ensure_browser_widget(self):
        if self._browser_widget is not None:
            return self._browser_widget

        context = self._context
        widget_type = self._browser_widget_type
        if widget_type is None:
            widget_type = load_browser_widget_type()
        self._browser_widget = widget_type(
            context.lang_manager,
            profile_paths=context.profile_paths,
            zoom_settings=context.browser_settings,
            download_service=context.browser_download_service_factory(),
            image_fetch_service=context.browser_image_fetch_service_factory(),
            aspect_ratio_provider=context.projection_aspect_ratio_provider,
            playback_protection=context.playback_protection,
            defer_initial_tabs=True,
            parent=context.parent,
        )
        self._connect_browser_signals()
        self._browser_widget.initial_tabs_preparation.completed.connect(
            self._install_browser_widget
        )
        self._browser_widget.initial_tabs_preparation.start()
        return self._browser_widget

    def _install_browser_widget(self) -> None:
        browser = self._browser_widget
        if browser is None or self._context.stack.indexOf(browser) >= 0:
            return
        self._replace_stack_widget(self.BROWSER_INDEX, browser)
        self._browser_installed()

    def ensure_wifi_receive_widget(self):
        if self._wifi_receive_widget is not None:
            return self._wifi_receive_widget

        from ..widgets.wifi_receive_widget import WifiReceiveWidget

        context = self._context
        self._wifi_receive_widget = WifiReceiveWidget(
            context.lang_manager,
            notifications=context.notifications,
            document_conversion_service=context.document_conversion_service,
            profile_media_store=context.profile_media_store,
            jwpub_import_thread_factory=context.jwpub_import_thread_factory,
            qr_generation_session_factory=context.qr_generation_session_factory,
            wifi_receive_server_factory=context.wifi_receive_server_factory,
            media_info_service_factory=context.media_info_service_factory,
            parent=context.parent,
        )
        self._replace_stack_widget(self.WIFI_INDEX, self._wifi_receive_widget)
        self._connect_wifi_receive_signals()
        return self._wifi_receive_widget

    def stop_browser_tab_projection(self) -> None:
        if self._browser_widget is not None:
            self._browser_widget._stop_tab_projection_internal()

    def set_browser_native_views_occluded(self, occluded: bool) -> None:
        if self._browser_widget is not None:
            self._browser_widget.set_native_views_occluded(occluded)

    def cleanup_browser(self) -> None:
        if self._browser_widget is not None:
            self._browser_widget.cleanup_browser()

    def _replace_stack_widget(self, index: int, widget: QWidget) -> None:
        stack = self._context.stack
        old = stack.widget(index)
        was_current = old is not None and stack.currentWidget() is old
        stack.insertWidget(index, widget)
        if was_current:
            stack.setCurrentWidget(widget)
        if old is not None:
            stack.removeWidget(old)
            old.deleteLater()

    def _connect_browser_signals(self) -> None:
        browser = self._browser_widget
        if self._browser_signals_connected or browser is None:
            return

        handlers = self._handlers
        browser.project_image_signal.connect(handlers.project_image)
        browser.project_video_signal.connect(self._project_browser_video)
        browser.stop_projection_signal.connect(handlers.stop_projection)
        browser.project_tab_pixmap_signal.connect(handlers.project_tab_frame)
        browser.stop_tab_projection_signal.connect(handlers.stop_projection)
        browser.media_destination_signal.connect(handlers.browser_media_destination)
        self._browser_signals_connected = True

    def _project_browser_video(self, url: str, title: str) -> None:
        item = {"url": url, "title": title, "type": "video"}
        self._handlers.project_video(url, title, [item], None)

    def _connect_wifi_receive_signals(self) -> None:
        wifi_receive = self._wifi_receive_widget
        if self._wifi_signals_connected or wifi_receive is None:
            return

        handlers = self._handlers
        wifi_receive.media_received.connect(handlers.wifi_media_received)
        wifi_receive.request_add_to_destination.connect(handlers.wifi_add_single)
        wifi_receive.request_add_all_to_destination.connect(handlers.wifi_add_all)
        wifi_receive.request_play.connect(handlers.wifi_play)
        self._wifi_signals_connected = True
