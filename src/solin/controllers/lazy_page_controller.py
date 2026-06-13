from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QWidget

if TYPE_CHECKING:
    from solin.main_window import MainWindow


class LazyPageController:
    """Owns lazy creation and signal wiring for heavyweight stack pages."""

    BROWSER_INDEX = 2
    CACHE_INDEX = 8
    WIFI_INDEX = 9

    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._browser_signals_connected = False
        self._cache_signals_connected = False
        self._wifi_signals_connected = False

        self._window.browser_widget = None
        self._window.cache_manager_widget = None
        self._window.wifi_receive_widget = None

    def placeholder(self) -> QWidget:
        page = QWidget(self._window)
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
        if self._window.browser_widget is not None:
            return self._window.browser_widget

        from ..widgets.browser.widget import BrowserWidget

        self._window.browser_widget = BrowserWidget(
            self._window.lang,
            profile_paths=self._window.profile_paths,
            media_cache_manager=self._window.media_cache_manager,
        )
        self._replace_stack_widget(self.BROWSER_INDEX, self._window.browser_widget)
        self._connect_browser_signals()
        return self._window.browser_widget

    def ensure_cache_manager_widget(self):
        if self._window.cache_manager_widget is not None:
            return self._window.cache_manager_widget

        from ..widgets.cache_manager_widget import CacheManagerWidget

        self._window.cache_manager_widget = CacheManagerWidget(
            self._window.lang,
            self._window.media_cache_manager,
            self._window.runtime_paths.thumb_cache_dir,
            parent=self._window,
        )
        self._replace_stack_widget(self.CACHE_INDEX, self._window.cache_manager_widget)
        self._connect_cache_manager_signals()
        return self._window.cache_manager_widget

    def ensure_wifi_receive_widget(self):
        if self._window.wifi_receive_widget is not None:
            return self._window.wifi_receive_widget

        from ..widgets.wifi_receive_widget import WifiReceiveWidget

        self._window.wifi_receive_widget = WifiReceiveWidget(
            self._window.lang,
            notifications=self._window.notifications,
            profile_paths=self._window.profile_paths,
            runtime_paths=self._window.runtime_paths,
            media_cache_dir=self._window.media_cache_manager.media_cache_dir,
            thumb_cache_dir=self._window.runtime_paths.thumb_cache_dir,
            parent=self._window,
        )
        self._replace_stack_widget(self.WIFI_INDEX, self._window.wifi_receive_widget)
        self._connect_wifi_receive_signals()
        return self._window.wifi_receive_widget

    def stop_browser_tab_projection(self) -> None:
        browser = getattr(self._window, "browser_widget", None)
        if browser is None:
            return
        browser._stop_tab_projection_internal()

    def cleanup_browser(self) -> None:
        browser = getattr(self._window, "browser_widget", None)
        if browser is None:
            return
        browser.cleanup_browser()

    def _replace_stack_widget(self, index: int, widget: QWidget) -> None:
        old = self._window.stack.widget(index)
        self._window.stack.insertWidget(index, widget)
        if old is not None:
            self._window.stack.removeWidget(old)
            old.deleteLater()

    def _connect_browser_signals(self) -> None:
        browser = self._window.browser_widget
        if self._browser_signals_connected or browser is None:
            return

        browser.project_image_signal.connect(self._window._project_image_bytes)
        browser.project_video_signal.connect(self._window._project_video)
        browser.stop_projection_signal.connect(self._window._stop_projection)
        browser.project_tab_pixmap_signal.connect(self._window._project_tab_frame)
        browser.stop_tab_projection_signal.connect(self._window._stop_projection)
        browser.add_to_playlist_signal.connect(
            lambda url, title, mtype: self._window._playlist_imports.add_current_to_playlist(
                url, title, {"type": mtype}
            )
        )
        browser.add_downloaded_file_to_playlist_signal.connect(
            self._window._playlist_imports.add_browser_downloaded_file
        )
        browser.download_failed_signal.connect(self._window._playlist_imports.browser_download_failed)
        self._browser_signals_connected = True

    def _connect_cache_manager_signals(self) -> None:
        cache_manager = self._window.cache_manager_widget
        if self._cache_signals_connected or cache_manager is None:
            return

        cache_manager.play_media_requested.connect(self._window._on_cache_play)
        self._cache_signals_connected = True

    def _connect_wifi_receive_signals(self) -> None:
        wifi_receive = self._window.wifi_receive_widget
        if self._wifi_signals_connected or wifi_receive is None:
            return

        wifi_receive.media_received.connect(self._window._on_wifi_media_received)
        wifi_receive.request_add_to_playlist.connect(
            self._window._on_wifi_request_add_single
        )
        wifi_receive.request_add_all_to_playlist.connect(
            self._window._on_wifi_send_all_to_playlist
        )
        wifi_receive.request_play.connect(self._window._on_wifi_request_play)
        self._wifi_signals_connected = True
