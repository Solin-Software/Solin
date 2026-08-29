from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core.foundation.qt_threads import OwnedQThreadRegistry

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ShutdownServices:
    """Long-lived services stopped by the main-window shutdown boundary."""

    remote_control: Callable[[], Any | None]
    remote_services: Callable[[], Any | None]
    download_notifications: Any | None
    playback_notifications: Any | None
    notifications: Any | None
    projection_integrations: Any
    background_song: Any
    media_countdown_automation: Any
    media_tree_runtime: Any
    media_controller: Any
    ndi: Any
    obs: Any
    zoom: Any
    ipc: Callable[[], Any | None]
    scenes: Any | None = None
    program_recording: Any | None = None
    program_content: Any | None = None
    content_frame_ingress: Any | None = None
    scene_frame_egresses: tuple[Any, ...] = ()
    camera: Any | None = None


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
    cleanup_lazy_pages: Callable[[], None] | None
    cancel_ui_preparation: Callable[[], None] | None = None


class ShutdownController:
    """Coordinates application shutdown from explicitly owned resources."""

    def __init__(self, dependencies: ShutdownDependencies) -> None:
        self._dependencies = dependencies
        self._started = False

    def shutdown(self) -> None:
        if self._started:
            return
        self._started = True
        stages = (
            ("cancel UI preparation", self.cancel_ui_preparation),
            ("stop remote control", self.stop_remote_control),
            ("stop scheduled automation", self.stop_scheduled_automation),
            ("close projection targets", self.close_projection_targets),
            ("stop remote services", self.stop_remote_services),
            ("stop notifications", self.stop_notifications),
            ("clean up widgets", self.cleanup_widgets),
            ("stop media-tree runtime", self.stop_media_tree_runtime),
            ("stop media services", self.stop_media_services),
            ("stop conversion threads", self.stop_conversion_threads),
            ("close IPC", self.close_ipc),
            ("clean up temporary files", self.cleanup_jwl_temp_files),
            ("clean up lazy pages", self.cleanup_lazy_pages),
        )
        for label, stage in stages:
            started = time.monotonic()
            try:
                stage()
            except Exception:  # noqa: BLE001 - every shutdown stage is isolated
                log.warning("Application shutdown stage failed: %s", label, exc_info=True)
            elapsed = time.monotonic() - started
            if elapsed >= 2.0:
                log.warning(
                    "Application shutdown stage was slow: %s (%.3f s)",
                    label,
                    elapsed,
                )

    def stop_scheduled_automation(self) -> None:
        self._dependencies.services.media_countdown_automation.shutdown()

    def stop_remote_control(self) -> None:
        remote_control = self._dependencies.services.remote_control()
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
        remote_services = self._dependencies.services.remote_services()
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
        # Stop the scene data plane in reverse dependency order. Producers must
        # stop before their consumers so no frame worker can be left waiting on
        # a mapping/mutex that disappeared with the native engine.
        if services.program_recording is not None:
            services.program_recording.close()
        if services.program_content is not None:
            services.program_content.close()
        if services.content_frame_ingress is not None:
            services.content_frame_ingress.close()
        for egress in services.scene_frame_egresses:
            egress.close()
        if services.scenes is not None:
            services.scenes.close()
        services.background_song.shutdown()
        services.media_controller.stop()
        services.ndi.stop(wait=True)
        if services.camera is not None:
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
        ipc = self._dependencies.services.ipc()
        if ipc is not None:
            ipc.close()

    def cleanup_jwl_temp_files(self) -> None:
        for tmp_path in self._dependencies.jwl_temp_files:
            self._remove_or_queue_tmp_file(tmp_path)

    def cleanup_lazy_pages(self) -> None:
        cleanup = self._dependencies.cleanup_lazy_pages
        if cleanup is None:
            return
        try:
            cleanup()
        except Exception:  # noqa: BLE001 - application shutdown cleanup boundary
            log.warning("Failed to cleanup lazy browser page during shutdown", exc_info=True)

    def cancel_ui_preparation(self) -> None:
        cancel = self._dependencies.cancel_ui_preparation
        if cancel is not None:
            cancel()

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
