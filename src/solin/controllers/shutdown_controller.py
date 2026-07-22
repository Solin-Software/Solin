from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.foundation.qt_threads import OwnedQThreadRegistry

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ShutdownServices:
    """Long-lived services stopped by the main-window shutdown boundary."""

    remote_control: Any | None
    remote_services: Any | None
    download_notifications: Any | None
    playback_notifications: Any | None
    notifications: Any | None
    projection_integrations: Any
    background_song: Any
    media_countdown_automation: Any
    media_tree_runtime: Any
    media_controller: Any
    ndi: Any
    camera: Any
    obs: Any
    zoom: Any
    ipc: Any | None


@dataclass(frozen=True, slots=True)
class ShutdownDependencies:
    """Explicit state and callbacks required to close the presentation shell."""

    projection_session: Any
    timer_output: Any | None
    services: ShutdownServices
    widget_providers: tuple[Callable[[], Any | None], ...]
    conversion_threads: OwnedQThreadRegistry
    jwl_temp_files: set[str]
    queue_pending_deletion: Callable[[str], None]
    save_window_state: Callable[[], None]
    cleanup_lazy_pages: Callable[[], None] | None


class ShutdownController:
    """Coordinates application shutdown from explicitly owned resources."""

    def __init__(self, dependencies: ShutdownDependencies) -> None:
        self._dependencies = dependencies

    def shutdown(self) -> None:
        self.stop_remote_control()
        self.stop_scheduled_automation()
        self.close_projection_targets()
        self.stop_remote_services()
        self.stop_notifications()
        self.cleanup_widgets()
        self.stop_media_tree_runtime()
        self.stop_media_services()
        self.stop_conversion_threads()
        self.close_ipc()
        self.cleanup_jwl_temp_files()
        self.save_window_state()
        self.cleanup_lazy_pages()

    def stop_scheduled_automation(self) -> None:
        self._dependencies.services.media_countdown_automation.shutdown()

    def stop_remote_control(self) -> None:
        remote_control = self._dependencies.services.remote_control
        if remote_control is not None:
            remote_control.stop()

    def close_projection_targets(self) -> None:
        dependencies = self._dependencies
        projection_session = dependencies.projection_session
        for window in projection_session.projection_windows:
            window.close()
        projection_session.projection_windows.clear()
        projection_session.close_floating_preview()
        if dependencies.timer_output is not None:
            dependencies.timer_output.close_all()

    def stop_remote_services(self) -> None:
        remote_services = self._dependencies.services.remote_services
        if remote_services is not None:
            remote_services.stop()

    def stop_notifications(self) -> None:
        services = self._dependencies.services
        if services.download_notifications is not None:
            services.download_notifications.stop()
        if services.playback_notifications is not None:
            services.playback_notifications.stop()
        if services.notifications is not None:
            services.notifications.shutdown()

    def cleanup_widgets(self) -> None:
        for provider in self._dependencies.widget_providers:
            self._cleanup_widget(provider())

    def stop_media_services(self) -> None:
        services = self._dependencies.services
        services.projection_integrations.cleanup()
        services.background_song.shutdown()
        services.media_controller.stop()
        services.ndi.stop(wait=True)
        services.camera.stop()
        services.obs.stop(wait=True)
        services.zoom.stop(wait=True)

    def stop_media_tree_runtime(self) -> None:
        alive = self._dependencies.services.media_tree_runtime.shutdown()
        if alive:
            log.warning("Media-tree operations still running at shutdown: %s", alive)

    def stop_conversion_threads(self) -> None:
        self._dependencies.conversion_threads.stop_all(logger=log)

    def close_ipc(self) -> None:
        ipc = self._dependencies.services.ipc
        if ipc is not None:
            ipc.close()

    def cleanup_jwl_temp_files(self) -> None:
        for tmp_path in self._dependencies.jwl_temp_files:
            self._remove_or_queue_tmp_file(tmp_path)

    def save_window_state(self) -> None:
        self._dependencies.save_window_state()

    def cleanup_lazy_pages(self) -> None:
        cleanup = self._dependencies.cleanup_lazy_pages
        if cleanup is None:
            return
        try:
            cleanup()
        except Exception:  # noqa: BLE001 - application shutdown cleanup boundary
            log.warning("Failed to cleanup lazy browser page during shutdown", exc_info=True)

    @staticmethod
    def _cleanup_widget(widget) -> None:
        if widget and hasattr(widget, "cleanup"):
            try:
                widget.cleanup()
            except Exception:  # noqa: BLE001 - application shutdown cleanup boundary
                log.warning("Widget cleanup failed during shutdown", exc_info=True)

    def _remove_or_queue_tmp_file(self, tmp_path: str) -> None:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            self._queue_pending_deletion(tmp_path)

    def _queue_pending_deletion(self, tmp_path: str) -> None:
        self._dependencies.queue_pending_deletion(tmp_path)
