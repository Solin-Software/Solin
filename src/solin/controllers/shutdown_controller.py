from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solin.main_window import MainWindow

log = logging.getLogger(__name__)


class ShutdownController:
    """Coordinates MainWindow shutdown without owning the app subsystems."""

    _CLEANUP_WIDGET_ATTRS = (
        "meetings_widget",
        "cache_manager_widget",
        "wifi_receive_widget",
        "playlist_widget",
        "timer_widget",
    )
    _CONVERSION_THREAD_ATTRS = (
        "_pdf_argv_threads",
        "_jwpub_argv_threads",
        "_lo_argv_threads",
    )

    def __init__(self, window: MainWindow) -> None:
        self._window = window

    def shutdown(self) -> None:
        self.close_projection_targets()
        self.stop_remote_services()
        self.stop_notifications()
        self.cleanup_widgets()
        self.stop_media_services()
        self.stop_conversion_threads()
        self.close_ipc()
        self.cleanup_jwl_temp_files()
        self.save_window_state()
        self.cleanup_lazy_pages()

    def close_projection_targets(self) -> None:
        for win in self._window.projection_windows:
            win.close()
        if self._window.floating_preview_window is not None:
            self._window.floating_preview_window.close()
            self._window.floating_preview_window = None
        timer_output = getattr(self._window, "_timer_output", None)
        if timer_output is not None:
            timer_output.close_all()

    def stop_remote_services(self) -> None:
        remote_services = getattr(self._window, "_remote_services", None)
        if remote_services is not None:
            remote_services.stop()

    def stop_notifications(self) -> None:
        download_notifications = getattr(
            self._window,
            "_media_download_notifications",
            None,
        )
        if download_notifications is not None:
            download_notifications.stop()
        notifications = getattr(self._window, "notifications", None)
        if notifications is not None:
            notifications.shutdown()

    def cleanup_widgets(self) -> None:
        for attr in self._CLEANUP_WIDGET_ATTRS:
            self._cleanup_widget(getattr(self._window, attr, None))

    def stop_media_services(self) -> None:
        self._window._background_song_service.shutdown()
        self._window.media_ctrl.stop()
        self._window._ndi_service.stop(wait=True)
        self._window._camera_service.stop()
        self._window._obs_service.stop(wait=True)
        self._window._zoom_service.stop(wait=True)

    def stop_conversion_threads(self) -> None:
        for attr in self._CONVERSION_THREAD_ATTRS:
            threads = getattr(self._window, attr, [])
            for thread in list(threads):
                self._stop_conversion_thread(thread, threads)

    def close_ipc(self) -> None:
        ipc_controller = getattr(self._window, "_ipc_controller", None)
        if ipc_controller is not None:
            ipc_controller.close()

    def cleanup_jwl_temp_files(self) -> None:
        for tmp_path in getattr(self._window, "_jwl_tmp_files", set()):
            self._remove_or_queue_tmp_file(tmp_path)

    def save_window_state(self) -> None:
        self._window._window_state.save_size()

    def cleanup_lazy_pages(self) -> None:
        lazy_pages = getattr(self._window, "_lazy_pages", None)
        if lazy_pages is None:
            return
        try:
            lazy_pages.cleanup_browser()
        except Exception:  # noqa: BLE001 - application shutdown cleanup boundary
            log.warning("Failed to cleanup lazy browser page during shutdown", exc_info=True)

    @staticmethod
    def _cleanup_widget(widget) -> None:
        if widget and hasattr(widget, "cleanup"):
            try:
                widget.cleanup()
            except Exception:  # noqa: BLE001 - application shutdown cleanup boundary
                log.warning("Widget cleanup failed during shutdown", exc_info=True)

    @staticmethod
    def _stop_conversion_thread(thread, threads) -> None:
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(3000)
            if thread.isRunning():
                thread.setParent(None)
                thread.finished.connect(thread.deleteLater)
            elif thread in threads:
                threads.remove(thread)
        except Exception:  # noqa: BLE001 - worker-thread shutdown boundary
            log.warning("Failed to stop conversion thread during shutdown", exc_info=True)

    @classmethod
    def _remove_or_queue_tmp_file(cls, tmp_path: str) -> None:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            cls._queue_pending_deletion(tmp_path)

    @staticmethod
    def _queue_pending_deletion(tmp_path: str) -> None:
        from ..core.playlists.storage import (
            load_pending_deletions,
            save_pending_deletions,
        )

        pending = load_pending_deletions()
        if tmp_path not in pending:
            pending.append(tmp_path)
            save_pending_deletions(pending)
