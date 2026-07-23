from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QCoreApplication


@dataclass(frozen=True, slots=True)
class MainWindowStartupDependencies:
    """Services and factories required for post-UI application startup."""

    projection_session: Any
    obs_scene_session: Any
    projection_targets: Any
    obs_settings: Any
    obs_service: Any
    live_integrations: Any
    zoom_settings: Any
    zoom_service: Any
    background_song_service: Any
    media_countdown_automation: Any
    monitor_popup_factory: Callable[[], Any]
    ipc_controller_factory: Callable[[], Any]
    remote_services_factory: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class MainWindowPreparedResources:
    """Resources required by the visible shell before its first frame."""

    monitor_popup: Any
    ipc_controller: Any


@dataclass(frozen=True, slots=True)
class MainWindowDeferredResources:
    """Long-lived resources created only after the first frame."""

    remote_services: Any


class MainWindowBootstrapController:
    """Runs ordered post-UI startup without locating services on MainWindow."""

    def __init__(
        self,
        dependencies: MainWindowStartupDependencies,
        *,
        app_getter: Callable[[], object | None] | None = None,
        platform: str | None = None,
    ) -> None:
        self._dependencies = dependencies
        self._app_getter = app_getter or QCoreApplication.instance
        self._platform = platform or sys.platform
        self._prepared: MainWindowPreparedResources | None = None
        self._deferred: MainWindowDeferredResources | None = None

    def prepare_before_show(self) -> MainWindowPreparedResources:
        if self._prepared is not None:
            return self._prepared
        self.initialize_projection_session()
        self.connect_zoom_signals()
        monitor_popup = self.build_monitor_popup()
        ipc_controller = self.create_ipc_controller()
        self._prepared = MainWindowPreparedResources(
            monitor_popup=monitor_popup,
            ipc_controller=ipc_controller,
        )
        return self._prepared

    def start_after_first_frame(self) -> MainWindowDeferredResources:
        if self._deferred is not None:
            return self._deferred
        prepared = self.prepare_before_show()
        self.start_obs_integration()
        self.start_zoom_if_enabled()
        self.start_background_song_service()
        self.start_media_countdown_automation()
        ipc_controller = prepared.ipc_controller
        self.start_ipc_if_needed(ipc_controller)
        remote_services = self.start_remote_services()
        self._deferred = MainWindowDeferredResources(
            remote_services=remote_services,
        )
        return self._deferred

    def initialize_projection_session(self) -> None:
        dependencies = self._dependencies
        session = dependencies.projection_session
        # Startup restore reads session state while rebuilding projection windows.
        session.set_idle_media_path("")
        session.floating_preview_window = None
        session.reset_state()
        dependencies.obs_scene_session.clear()
        dependencies.projection_targets.open_projection_windows()
        session.set_tab_projection_active(False)

    def start_obs_integration(self) -> None:
        dependencies = self._dependencies
        if dependencies.obs_settings.is_enabled():
            dependencies.obs_service.start()
        dependencies.live_integrations.refresh_obs_btn_availability()
        dependencies.live_integrations.refresh_obs_stream_availability()

    def connect_zoom_signals(self) -> None:
        dependencies = self._dependencies
        zoom = dependencies.zoom_service
        live = dependencies.live_integrations
        zoom.connection_changed.connect(live.on_zoom_connection_changed)
        zoom.participants_updated.connect(live.on_zoom_participants_updated)
        zoom.sharing_state_changed.connect(live.on_zoom_sharing_state_changed)
        zoom.share_error.connect(live.on_zoom_share_error)

    def start_zoom_if_enabled(self) -> None:
        dependencies = self._dependencies
        if dependencies.zoom_settings.is_enabled() and self._platform == "win32":
            dependencies.zoom_service.start()

    def start_background_song_service(self) -> None:
        self._dependencies.background_song_service.start()

    def start_media_countdown_automation(self) -> None:
        self._dependencies.media_countdown_automation.start()

    def build_monitor_popup(self):
        dependencies = self._dependencies
        popup = dependencies.monitor_popup_factory()
        popup.projection_toggle_requested.connect(
            dependencies.projection_targets.on_monitor_toggle
        )
        popup.projection_all_requested.connect(
            dependencies.projection_targets.on_monitor_all
        )
        popup.floating_toggle_requested.connect(
            dependencies.projection_targets.on_floating_toggle
        )
        popup.idle_media_changed.connect(
            dependencies.projection_targets.on_idle_media_changed
        )
        return popup

    def create_ipc_controller(self):
        return self._dependencies.ipc_controller_factory()

    def start_ipc_if_needed(self, ipc_controller) -> None:
        app = self._app_getter()
        if not getattr(app, "_solin_app_ipc_server_active", False):
            ipc_controller.start()

    def start_remote_services(self):
        remote_services = self._dependencies.remote_services_factory()
        remote_services.start()
        return remote_services
