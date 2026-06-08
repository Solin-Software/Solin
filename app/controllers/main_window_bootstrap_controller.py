from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QCoreApplication

from ..core.foundation.settings_keys import SettingsKey
from ..styles.theme import STYLESHEET
from ..widgets.projection import MonitorManagerPopup
from .ipc_controller import IpcController
from .remote_services_controller import RemoteServicesController

if TYPE_CHECKING:
    from app.main_window import MainWindow


class MainWindowBootstrapController:
    """Runs post-UI startup steps that depend on constructed MainWindow widgets."""

    def __init__(
        self,
        window: MainWindow,
        *,
        app_getter: Callable[[], object | None] | None = None,
        platform: str | None = None,
        monitor_popup_factory=MonitorManagerPopup,
        ipc_controller_factory=IpcController,
        remote_services_factory=RemoteServicesController,
        stylesheet: str = STYLESHEET,
    ) -> None:
        self._window = window
        self._app_getter = app_getter or QCoreApplication.instance
        self._platform = platform or sys.platform
        self._monitor_popup_factory = monitor_popup_factory
        self._ipc_controller_factory = ipc_controller_factory
        self._remote_services_factory = remote_services_factory
        self._stylesheet = stylesheet

    def finish_startup(self) -> None:
        self.initialize_projection_session()
        self.start_obs_integration()
        self.connect_zoom_signals()
        self.start_zoom_if_enabled()
        self.start_background_song_service()
        self.build_monitor_popup()
        self.initialize_open_media_state()
        self.initialize_ipc_controller()
        self.initialize_obs_scene_memory()
        self.start_ipc_if_needed()
        self.start_remote_services()
        self.apply_stylesheet()

    def initialize_projection_session(self) -> None:
        window = self._window
        # Must be set before opening projection targets because startup restore
        # reads these attributes while rebuilding the projection windows.
        window._idle_media_path = ""
        window.floating_preview_window = None
        window._proj_state = {"type": "idle"}
        window._projection_targets.open_projection_windows()
        window._tab_proj_active = False

    def start_obs_integration(self) -> None:
        window = self._window
        if window._obs_prefs.value(SettingsKey.OBS_ENABLED, False, bool):
            window._obs_service.start()
        window._live_integrations.refresh_obs_btn_availability()
        window._live_integrations.refresh_obs_stream_availability()

    def connect_zoom_signals(self) -> None:
        window = self._window
        zoom = window._zoom_service
        live = window._live_integrations
        zoom.connection_changed.connect(live.on_zoom_connection_changed)
        zoom.participants_updated.connect(live.on_zoom_participants_updated)
        zoom.sharing_state_changed.connect(live.on_zoom_sharing_state_changed)
        zoom.share_error.connect(live.on_zoom_share_error)

    def start_zoom_if_enabled(self) -> None:
        window = self._window
        if (
            window._zoom_prefs.value(SettingsKey.ZOOM_ENABLED, False, bool)
            and self._platform == "win32"
        ):
            window._zoom_service.start()

    def start_background_song_service(self) -> None:
        self._window._background_song_service.start()

    def build_monitor_popup(self) -> None:
        window = self._window
        window._monitor_popup = self._monitor_popup_factory(window)
        window._monitor_popup.projection_toggle_requested.connect(
            window._projection_targets.on_monitor_toggle
        )
        window._monitor_popup.projection_all_requested.connect(
            window._projection_targets.on_monitor_all
        )
        window._monitor_popup.floating_toggle_requested.connect(
            window._projection_targets.on_floating_toggle
        )
        window._monitor_popup.idle_media_changed.connect(
            window._projection_targets.on_idle_media_changed
        )

    def initialize_open_media_state(self) -> None:
        window = self._window
        window._jwl_tmp_files = set()
        window._next_is_sjjm = False

    def initialize_ipc_controller(self) -> None:
        self._window._ipc_controller = self._ipc_controller_factory(self._window)

    def start_ipc_if_needed(self) -> None:
        window = self._window
        app = self._app_getter()
        if not getattr(app, "_solin_app_ipc_server_active", False):
            window._ipc_controller.start()

    def initialize_ipc(self) -> None:
        self.initialize_ipc_controller()
        self.start_ipc_if_needed()

    def initialize_obs_scene_memory(self) -> None:
        self._window._obs_pre_media_scene = ""

    def start_remote_services(self) -> None:
        window = self._window
        window._remote_services = self._remote_services_factory(window, window.lang)
        window._remote_services.start()

    def apply_stylesheet(self) -> None:
        self._window.setStyleSheet(self._stylesheet)
