from __future__ import annotations

from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from PySide6.QtWidgets import QMainWindow
from PySide6.QtCore import QObject, QTimer, Signal, QEvent

from .controllers.auto_key_projection_controller import AutoKeyProjectionController
from .controllers.language_controller import LanguageController
from .controllers.live_integration_controller import (
    LiveIntegrationContext,
    LiveIntegrationController,
    LiveIntegrationHandlers,
)
from .controllers.ipc_controller import IpcController
from .controllers.main_window_bootstrap_controller import (
    MainWindowBootstrapController,
    MainWindowStartupDependencies,
)
from .controllers.main_window_ui_controller import MainWindowUiController
from .controllers.media_download_notification_controller import (
    MediaDownloadNotificationController,
)
from .controllers.media_projection_controller import MediaProjectionController
from .controllers.open_media_controller import (
    OpenMediaContext,
    OpenMediaController,
    OpenMediaHandlers,
)
from .controllers.playlist_import_controller import (
    PlaylistImportContext,
    PlaylistImportController,
    PlaylistImportHandlers,
)
from .controllers.profile_switch_controller import ProfileSwitchController
from .controllers.projection_integration_controller import ProjectionIntegrationController
from .controllers.projection_stop_controller import ProjectionStopController
from .controllers.projection_window_controller import ProjectionWindowController
from .controllers.remote_services_controller import RemoteServicesController
from .controllers.shutdown_controller import (
    ShutdownController,
    ShutdownDependencies,
    ShutdownServices,
)
from .controllers.signal_connection_controller import (
    MainWindowSignalHandlers,
    MainWindowSignalSources,
    SignalConnectionController,
)
from .controllers.timer_engine import TimerEngine
from .controllers.timer_monitor_controller import TimerMonitorController
from .controllers.timer_output_controller import TimerOutputController
from .controllers.timer_pdf_export_controller import TimerPdfExportController
from .controllers.timer_theme_controller import TimerThemeController
from .controllers.wifi_playlist_controller import (
    WifiPlaylistContext,
    WifiPlaylistController,
    WifiPlaylistHandlers,
)
from .controllers.window_state_controller import WindowStateController
from .core.projection.application import ObsSceneSession, ProjectionSession
from .core.timer.application import TimerSession
from .core.ui.monitor_allocation import MonitorAllocationStore
from .core.ui.window_settings import WindowGeometrySettingsStore
from .core.i18n.manager import LanguageManager
from .core.jw.background_song_service import BackgroundSongService
from .core.jw.background_song_settings import BackgroundSongSettingsStore
from .core.jw.catalog import JWMediaCatalogCachePaths
from .core.jw.songs import JWSongsStore
from .core.jw.yeartext_settings import YeartextSettingsStore
from .core.ingest.watched_folder_settings import WatchedFolderSettingsStore
from .core.media.playback import MediaController
from .core.media.cache import MediaCacheManager
from .core.media.settings import MediaSettingsStore, ProjectionPlaybackSettingsStore
from .core.rendering.fonts import FontManager
from .core.ui.notifications import NotificationCenter
from .core.ui.screens import ScreenManager
from .core.integrations.automation.obs import OBSWebSocketService
from .core.integrations.automation.settings import (
    AutoShareSettingsStore,
    CameraSettingsStore,
    OBSSettingsStore,
    ZoomSettingsStore,
)
from .core.integrations.ndi import NDIReceiverService
from .core.integrations.camera import CameraService
from .core.integrations.automation.zoom.service import ZoomService
from .core.integrations.automation.shortcuts import (
    AutoKeyDispatcher,
    AutoKeySettingsStore,
)
from .core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from .core.foundation.qt_threads import OwnedQThreadRegistry
from .core.profiles.settings import ProfileSettings
from .core.playlists.storage import PlaylistStoragePaths
from .core.meetings.schedule_settings import MeetingScheduleSettingsStore
from .core.meetings.tree_store import MeetingTreeStore
from .core.meetings.publications import JwpubChecksumStore
from .core.profiles.models import ProfileInfo
from .core.media.formats import AUDIO_EXTS as _AUDIO_EXTS_LOCAL
from .widgets.timer_bridge import TimerBridge
from .widgets.projection.monitor_manager import MonitorManagerPopup

if TYPE_CHECKING:
    from .widgets.media_info_extractor import MediaInfoQueue, MediaInfoService

# ── MainWindow ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    _auto_share_finished = Signal(int, bool, bool)
    proj_bar: Any
    right_col: Any
    _quick_toolbar: Any

    def __init__(
        self,
        lang_manager: LanguageManager,
        runtime_paths: RuntimePaths,
        profile_paths: ProfilePaths,
        profile_settings: ProfileSettings,
        media_cache_manager: MediaCacheManager,
        media_controller: MediaController,
        background_media_controller: MediaController,
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        media_info_service_factory: Callable[[QObject], MediaInfoService],
        media_settings: MediaSettingsStore,
        font_manager: FontManager,
        jw_catalog_cache_paths: JWMediaCatalogCachePaths,
        jw_songs_store: JWSongsStore,
        jwpub_checksum_store: JwpubChecksumStore,
        playlist_storage_paths: PlaylistStoragePaths,
        meeting_tree_store: MeetingTreeStore,
        timer_session: TimerSession,
        active_profile: ProfileInfo,
    ):
        super().__init__()
        self.lang = lang_manager
        self.runtime_paths = runtime_paths
        self.profile_paths = profile_paths
        self.profile_settings = profile_settings
        self.media_cache_manager = media_cache_manager
        self.media_ctrl = media_controller
        self._background_media_controller = background_media_controller
        self.font_manager = font_manager
        self.jw_catalog_cache_paths = jw_catalog_cache_paths
        self.jw_songs_store = jw_songs_store
        self.jwpub_checksum_store = jwpub_checksum_store
        self.playlist_storage_paths = playlist_storage_paths
        self.meeting_tree_store = meeting_tree_store
        self.timer_session = timer_session
        self.active_profile = active_profile
        self._obs_settings = OBSSettingsStore.for_profile_settings(profile_settings)
        self._zoom_settings = ZoomSettingsStore.for_profile_settings(profile_settings)
        self._auto_share_settings = AutoShareSettingsStore.for_profile_settings(
            profile_settings,
        )
        self._auto_key_settings = AutoKeySettingsStore.for_profile_settings(
            profile_settings,
        )
        self._camera_settings = CameraSettingsStore.for_profile_settings(profile_settings)
        self._media_settings = media_settings
        self._projection_playback_settings = (
            ProjectionPlaybackSettingsStore.for_profile_settings(profile_settings)
        )
        self._meeting_schedule_settings = (
            MeetingScheduleSettingsStore.for_profile_settings(profile_settings)
        )
        self._watched_folder_settings = WatchedFolderSettingsStore.for_profile_settings(
            profile_settings,
        )
        self._yeartext_settings = YeartextSettingsStore.for_profile_settings(
            profile_settings,
        )
        self._background_song_settings = (
            BackgroundSongSettingsStore.for_profile_settings(profile_settings)
        )
        self.screen_mgr = ScreenManager(self)
        self._monitor_allocation = MonitorAllocationStore.for_profile_settings(
            profile_settings,
        )
        self.projection_session = ProjectionSession(
            allocation=self._monitor_allocation,
            media_hidden_screen_names=self._monitor_allocation.media_off_names(
                ScreenManager.secondary_screens()
            ),
        )
        self._obs_scene_session = ObsSceneSession()
        self._monitor_popup = None
        self._ipc_controller = None
        self._remote_services = None
        self._jwl_tmp_files: set[str] = set()
        self._next_is_sjjm = False
        self._conversion_threads = OwnedQThreadRegistry()
        self._shutdown_controller = None
        self._auto_keys = AutoKeyDispatcher(self._auto_key_settings, self)
        self._profile_switch = ProfileSwitchController(self)
        self._projection_targets = ProjectionWindowController(self)
        self._timer_theme_controller = TimerThemeController(self)
        self._media_projection = MediaProjectionController(self)
        self._projection_stop = ProjectionStopController(self)

        # ── Advanced timer + shared monitor allocation ────────────────────────
        # Created before the UI is built so the Timer tab can bind to them.
        # The allocation store is the persistent source of truth for which
        # subsystem (media/timer) owns each monitor — consulted by both the
        # media projection controller and the timer-output controller.
        self.timer_engine = TimerEngine(self)
        self.timer_output = TimerOutputController(
            self.timer_engine,
            timer_session,
            self._monitor_allocation,
        )
        self.timer_monitors = TimerMonitorController(
            screen_manager=self.screen_mgr,
            allocation=self._monitor_allocation,
            timer_output=self.timer_output,
            projection_windows=lambda: tuple(self.projection_session.projection_windows),
            reconcile_media=self._projection_targets.reconcile_projection_windows,
            deactivate_media_screen=self.projection_session.hide_media_on_screen_name,
        )
        self.timer_bridge = TimerBridge(
            engine=self.timer_engine,
            session=timer_session,
            output=self.timer_output,
            monitors=self.timer_monitors,
            pdf_export=TimerPdfExportController(self),
            language_manager=lang_manager,
        )

        # OBS WebSocket integration
        self._obs_service = OBSWebSocketService(self._obs_settings, self)

        # OBS/DistroAV NDI program stream receiver
        self._ndi_service = NDIReceiverService(self)

        # Live camera receiver
        self._camera_service = CameraService(self)

        # Zoom Meetings integration
        self._zoom_service = ZoomService(self._zoom_settings, self)

        self._background_song_service = BackgroundSongService(
            self.lang,
            self._background_song_settings,
            self._meeting_schedule_settings,
            jw_songs_store,
            background_media_controller,
            self,
        )

        self.setWindowTitle(self.tr("Solin"))
        self.setMinimumSize(900, 600)
        self._window_state = WindowStateController(
            self,
            WindowGeometrySettingsStore.for_profile_settings(profile_settings),
        )
        self._window_state.restore_size()
        self._window_state.apply_icon()

        self.notifications = NotificationCenter(self)
        self._media_download_notifications = MediaDownloadNotificationController(
            self.notifications,
            media_cache_manager,
            self,
        )
        self._media_download_notifications.start()

        self._ui_controller = MainWindowUiController(
            self,
            active_profile,
            media_info_queue_factory=media_info_queue_factory,
            media_info_service_factory=media_info_service_factory,
        )
        self._build_ui()
        self._playlist_imports = PlaylistImportController(
            PlaylistImportContext(
                dialog_parent=self,
                runtime_paths=self.runtime_paths,
                profile_paths=self.profile_paths,
                language_manager=self.lang,
                notifications=self.notifications,
                playlist_widget=self.playlist_widget,
                thread_registry=self._conversion_threads,
                translate=self.tr,
            ),
            PlaylistImportHandlers(
                switch_to_playlist=lambda: self._navigation.switch_page(7),
            ),
        )
        self._wifi_playlist_controller = WifiPlaylistController(
            WifiPlaylistContext(
                dialog_parent=self,
                playlist_widget=self.playlist_widget,
                notifications=self.notifications,
                translate=self.tr,
                wifi_receive_widget=lambda: (
                    self._lazy_pages.wifi_receive_widget
                ),
            ),
            WifiPlaylistHandlers(
                play_cached_media=self._media_projection.on_cache_play,
                project_video=self._media_projection.project_video,
            ),
        )
        self._open_media_controller = OpenMediaController(
            OpenMediaContext(
                dialog_parent=self,
                runtime_paths=self.runtime_paths,
                profile_paths=self.profile_paths,
                language_manager=self.lang,
                notifications=self.notifications,
                thread_registry=self._conversion_threads,
                temp_files=self._jwl_tmp_files,
                translate=self.tr,
            ),
            OpenMediaHandlers(
                switch_to_playlist=lambda: self._navigation.switch_page(7),
                project_media_at_index=self._media_projection.project_media_at_index,
                expand_projection_overlay=self.proj_bar.expand_overlay,
                send_to_temp_playlist=self._playlist_imports.send_to_temp_playlist,
                open_pdf_temp_playlist=(
                    self.playlist_widget.open_pdf_as_temp_playlist
                ),
                open_named_temp_playlist=lambda items, name: (
                    self.playlist_widget.open_temp_playlist(
                        items,
                        self.lang,
                        name=name,
                    )
                ),
                append_temp_playlist_items=(
                    self.playlist_widget.append_temp_playlist_items
                ),
            ),
        )
        self._auto_key_projection = AutoKeyProjectionController(self._auto_keys, self.proj_bar)
        self._projection_integrations = ProjectionIntegrationController(
            self,
            self._obs_scene_session,
        )
        self._live_integrations = LiveIntegrationController(
            LiveIntegrationContext(
                projection_session=self.projection_session,
                obs_scene_session=self._obs_scene_session,
                obs_settings=self._obs_settings,
                camera_settings=self._camera_settings,
                obs_service=self._obs_service,
                ndi_service=self._ndi_service,
                camera_service=self._camera_service,
                zoom_service=self._zoom_service,
                notifications=self.notifications,
                projection_bar=self.proj_bar,
                media_controller=self.media_ctrl,
                quick_toolbar=lambda: getattr(self, "_quick_toolbar", None),
                projection_windows=self.projection_session.all_windows,
                translate=self.tr,
            ),
            LiveIntegrationHandlers(
                stop_projection=self._projection_stop.stop_projection,
                stop_browser_tab_projection=(
                    self._navigation.stop_browser_tab_projection
                ),
                update_projection_status=(
                    self._projection_integrations.update_status
                ),
            ),
        )
        self._language_controller = LanguageController(self)
        self._signal_connections = SignalConnectionController(
            MainWindowSignalSources(
                songs_widget=self.songs_widget,
                meetings_widget=self.meetings_widget,
                clips_widget=self.clips_widget,
                timer_widget=self.timer_widget,
                sermon_theme_widget=self.sermon_theme_widget,
                playlist_widget=self.playlist_widget,
                projection_bar=self.proj_bar,
                media_controller=self.media_ctrl,
                screen_manager=self.screen_mgr,
                language_manager=self.lang,
                settings_widget=self.settings_widget,
                obs_service=self._obs_service,
                ndi_service=self._ndi_service,
                camera_service=self._camera_service,
                auto_share_finished=self._auto_share_finished,
            ),
            MainWindowSignalHandlers(
                media_projection=self._media_projection,
                timer_theme=self._timer_theme_controller,
                playlist_imports=self._playlist_imports,
                auto_key_projection=self._auto_key_projection,
                projection_stop=self._projection_stop,
                projection_targets=self._projection_targets,
                language_controller=self._language_controller,
                live_integrations=self._live_integrations,
                background_song_service=self._background_song_service,
                projection_integrations=self._projection_integrations,
                timer_output=self.timer_output,
                timer_bridge=self.timer_bridge,
            ),
        )
        self._signal_connections.connect_signals()

        self._bootstrap_controller = MainWindowBootstrapController(
            MainWindowStartupDependencies(
                projection_session=self.projection_session,
                obs_scene_session=self._obs_scene_session,
                projection_targets=self._projection_targets,
                obs_settings=self._obs_settings,
                obs_service=self._obs_service,
                live_integrations=self._live_integrations,
                zoom_settings=self._zoom_settings,
                zoom_service=self._zoom_service,
                background_song_service=self._background_song_service,
                monitor_popup_factory=lambda: MonitorManagerPopup(self),
                ipc_controller_factory=lambda: IpcController(
                    self,
                    bring_to_front=self._bring_to_front,
                    open_media_files=self.open_media_files,
                ),
                remote_services_factory=lambda: RemoteServicesController(
                    self,
                    self.lang,
                    self.profile_settings,
                ),
                apply_stylesheet=self.setStyleSheet,
            )
        )
        startup_resources = self._bootstrap_controller.finish_startup()
        self._monitor_popup = startup_resources.monitor_popup
        self._ipc_controller = startup_resources.ipc_controller
        self._remote_services = startup_resources.remote_services
        self._shutdown_controller = ShutdownController(
            ShutdownDependencies(
                projection_session=self.projection_session,
                timer_output=self.timer_output,
                services=ShutdownServices(
                    remote_services=self._remote_services,
                    download_notifications=self._media_download_notifications,
                    notifications=self.notifications,
                    projection_integrations=self._projection_integrations,
                    background_song=self._background_song_service,
                    media_controller=self.media_ctrl,
                    ndi=self._ndi_service,
                    camera=self._camera_service,
                    obs=self._obs_service,
                    zoom=self._zoom_service,
                    ipc=self._ipc_controller,
                ),
                widget_providers=(
                    lambda: self.meetings_widget,
                    lambda: self._lazy_pages.cache_manager_widget,
                    lambda: self._lazy_pages.wifi_receive_widget,
                    lambda: self.playlist_widget,
                    lambda: self.timer_widget,
                ),
                conversion_threads=self._conversion_threads,
                jwl_temp_files=self._jwl_tmp_files,
                playlist_storage_paths=self.playlist_storage_paths,
                save_window_state=self._window_state.save_size,
                cleanup_lazy_pages=self._lazy_pages.cleanup_browser,
            )
        )

        # Show the clock window on any monitor reserved for the timer once the
        # screens have settled (mirrors the media projection startup timing).
        QTimer.singleShot(900, self.timer_output.reconcile)

    # ── UI Build ──────────────────────────────────────────────────────────

    def _build_ui(self):
        self._ui_controller.build_ui()

    def _build_sidebar(self):
        return self._ui_controller.build_sidebar()

    def _build_bottom_bar(self):
        return self._ui_controller.build_bottom_bar()

    # Signal emitted when user wants to return to profile selector.
    switch_profile_requested = Signal()

    # ── Projection ────────────────────────────────────────────────────────

    def _edit_view_is_temp(self) -> bool:
        return self._media_projection.edit_view_is_temp()

    def _project_video_core(self, url: str, title: str, keep_expanded: bool = False, is_audio: bool = False):
        self._media_projection.project_video_core(
            url,
            title,
            keep_expanded=keep_expanded,
            is_audio=is_audio,
        )

    def _stop_projection(self):
        self._projection_stop.stop_projection()

    def eventFilter(self, obj, event):
        if obj is self.right_col and event.type() == QEvent.Type.Resize:
            self._quick_toolbar.reposition()
        return super().eventFilter(obj, event)

    # ── Language ──────────────────────────────────────────────────────────

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslateUi()
        super().changeEvent(event)

    def retranslateUi(self) -> None:
        self._language_controller.retranslate_ui()

    def _all_windows(self) -> list:
        return self.projection_session.all_windows()

    # ── IPC — single instance ─────────────────────────────────────────────

    def _bring_to_front(self):
        self._window_state.bring_to_front()

    # ── Open-with / Drag-to-exe ───────────────────────────────────────────

    _AUDIO_EXTS = _AUDIO_EXTS_LOCAL

    def open_media_files(self, paths: list):
        self._open_media_controller.open_media_files(paths)

    def _project_media_at_index(self, playlist: list, index: int = 0,
                                keep_expanded: bool = False,
                                playback_order: str | None = None):
        self._media_projection.project_media_at_index(
            playlist,
            index=index,
            keep_expanded=keep_expanded,
            playback_order=playback_order,
        )

    def showEvent(self, event):
        super().showEvent(event)
        self._window_state.center_on_primary_screen()
        # Apply custom titlebar color once the native window handle exists.
        # Using a zero-delay singleShot ensures the OS has fully created the
        # window before we touch DWM / NSWindow.
        QTimer.singleShot(0, self._window_state.apply_titlebar_color)

    def closeEvent(self, event):
        if getattr(self, "_closing", False):
            super().closeEvent(event)
            return
        self._closing = True

        if self._shutdown_controller is not None:
            self._shutdown_controller.shutdown()
        super().closeEvent(event)
