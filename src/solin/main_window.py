from __future__ import annotations

from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from PySide6.QtWidgets import QMainWindow
from PySide6.QtCore import QObject, QTimer, Signal, QEvent

from .controllers.auto_key_projection_controller import AutoKeyProjectionController
from .controllers.language_controller import LanguageContext, LanguageController
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
from .controllers.main_window_ui_controller import (
    MainWindowUiContext,
    MainWindowUiController,
    MainWindowUiHandlers,
    MainWindowUiResources,
)
from .controllers.media_download_notification_controller import (
    MediaDownloadNotificationController,
)
from .controllers.media_projection_controller import (
    MediaProjectionContext,
    MediaProjectionController,
    MediaProjectionHandlers,
)
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
from .controllers.projection_integration_controller import (
    ProjectionIntegrationContext,
    ProjectionIntegrationController,
)
from .controllers.projection_stop_controller import (
    ProjectionStopContext,
    ProjectionStopController,
    ProjectionStopHandlers,
)
from .controllers.projection_window_controller import (
    ProjectionWindowContext,
    ProjectionWindowController,
)
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
from .controllers.timer_theme_controller import (
    TimerThemeContext,
    TimerThemeController,
    TimerThemeHandlers,
)
from .controllers.wifi_playlist_controller import (
    WifiPlaylistContext,
    WifiPlaylistController,
    WifiPlaylistHandlers,
)
from .controllers.window_state_controller import WindowStateContext, WindowStateController
from .core.projection.application import ObsSceneSession, ProjectionSession
from .core.timer.application import TimerSession
from .core.ui.monitor_allocation import MonitorAllocationStore
from .core.ui.window_settings import WindowGeometrySettingsStore
from .core.i18n.manager import LanguageManager
from .core.integrations.automation.screen_share import (
    execute_start_share,
    execute_stop_share,
    macos_accessibility_trusted,
)
from .core.jw.background_song_service import BackgroundSongService
from .core.jw.background_song_settings import BackgroundSongSettingsStore
from .core.jw.songs import JWSongsStore
from .core.jw.yeartext import YeartextService
from .core.jw.yeartext_settings import YeartextSettingsStore
from .core.ingest.watched_folder_settings import WatchedFolderSettingsStore
from .core.media.playback import MediaController
from .core.media.cache import MediaCacheManager
from .core.media.settings import MediaSettingsStore, ProjectionPlaybackSettingsStore
from .core.media.profile_store import ProfileMediaStore
from .core.media.thumbnail_store import ThumbnailStore
from .core.rendering.fonts import FontManager
from .ui.notifications import NotificationCenter
from .core.ui.screens import ScreenManager
from .core.integrations.automation.obs import OBSWebSocketService
from .core.integrations.automation.settings import (
    AutoKeySettingsStore,
    AutoShareSettingsStore,
    CameraSettingsStore,
    OBSSettingsStore,
    ZoomSettingsStore,
)
from .core.integrations.ndi import NDIReceiverService
from .core.integrations.camera import CameraService
from .core.integrations.automation.zoom.service import ZoomService
from .core.integrations.automation.shortcuts import AutoKeyDispatcher
from .core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from .core.foundation.settings_store import InstallationSettingsStore
from .core.foundation.qt_threads import OwnedQThreadRegistry
from .core.profiles.settings import ProfileSettings
from .core.playlists.storage import PlaylistRepository, PlaylistStoragePaths
from .core.meetings.schedule_settings import MeetingScheduleSettingsStore
from .core.meetings.tree_store import MeetingTreeStore
from .core.meetings.memorial import MemorialService
from .core.meetings.jwpub_cache import JwpubChecksumStore
from .core.meetings.publications import JwpubService
from .core.profiles.models import ProfileInfo
from .core.remote.notification_settings import NotificationSettingsStore
from .core.remote.notifications import NotificationService
from .core.remote.patch_installer import (
    PatchDownloadWorker,
    launch_patch_installer,
    save_pending_patch_cleanup,
)
from .core.remote.updates import UpdateService
from .widgets.projection.monitor_manager import MonitorManagerPopup
from .ui.dialogs.notifications import RemoteNotificationQueue
from .ui.dialogs.update import UpdateDialog
from .ui.qml.timer_bridge import TimerBridge
from .ui.window_focus import raise_projection_window

if TYPE_CHECKING:
    from .core.ingest.watched_folder import WatchedFolderWatcher
    from .core.ingest.watched_folder_files import WatchedFolderFileStore
    from .core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
    from .core.ingest.wifi_server import WifiReceiveServer
    from .core.ingest.qr_generation import QrGenerationSessionFactory
    from .core.jw.clip_fetch import ClipFetchThreadFactory
    from .core.jw.catalog_service import JWMediaCatalogService
    from .core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from .core.jw.thumbnail_fetch import JWCatalogThumbnailSessionFactory
    from .core.media.browser_downloads import BrowserDownloadService
    from .core.media.cache_scan import CacheScanSessionFactory
    from .core.network.browser_images import BrowserImageFetchService
    from .core.playlists.cleanup import PlaylistCleanupQueue
    from .core.rendering.document_conversion import DocumentConversionService
    from .ui.media_info import MediaInfoQueue, MediaInfoService

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
        browser_download_service_factory: Callable[[], BrowserDownloadService],
        browser_image_fetch_service_factory: Callable[[], BrowserImageFetchService],
        media_settings: MediaSettingsStore,
        font_manager: FontManager,
        jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
        jw_catalog_thumbnail_session_factory: JWCatalogThumbnailSessionFactory,
        jw_songs_store: JWSongsStore,
        jwpub_checksum_store: JwpubChecksumStore,
        installation_settings: InstallationSettingsStore,
        playlist_storage_paths: PlaylistStoragePaths,
        playlist_repository: PlaylistRepository,
        meeting_tree_store: MeetingTreeStore,
        profile_media_store: ProfileMediaStore,
        jwpub_import_thread_factory: JwpubImportThreadFactory,
        document_conversion_service: DocumentConversionService,
        clip_fetch_thread_factory: ClipFetchThreadFactory,
        cache_scan_session_factory: CacheScanSessionFactory,
        qr_generation_session_factory: QrGenerationSessionFactory,
        playlist_thumbnail_store: ThumbnailStore,
        meeting_thumbnail_store: ThumbnailStore,
        watched_folder_file_store: WatchedFolderFileStore,
        watched_folder_playlist_store: WatchedFolderPlaylistStore,
        wifi_receive_server_factory: Callable[[QObject], WifiReceiveServer],
        watched_folder_watcher_factory: Callable[[QObject], WatchedFolderWatcher],
        playlist_cleanup_queue_factory: Callable[..., PlaylistCleanupQueue],
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
        self.jw_songs_store = jw_songs_store
        self.jwpub_checksum_store = jwpub_checksum_store
        self._installation_settings = installation_settings
        self.playlist_storage_paths = playlist_storage_paths
        self.playlist_repository = playlist_repository
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
        self._conversion_threads = OwnedQThreadRegistry()
        self._shutdown_controller = None
        self._auto_keys = AutoKeyDispatcher(self._auto_key_settings, self)
        self._profile_switch = ProfileSwitchController(
            self.switch_profile_requested.emit
        )
        self._projection_targets = ProjectionWindowController(
            ProjectionWindowContext(
                session=self.projection_session,
                font_manager=self.font_manager,
                secondary_screens=ScreenManager.secondary_screens,
                sync_projection_integrations=(
                    lambda: self._projection_integrations.sync_projection_integrations()
                ),
                sync_obs_scene=lambda active: (
                    self._projection_integrations.sync_obs_scene(active)
                ),
                yearly_text=self._current_yearly_projection_text,
                set_projection_screen_count=(
                    lambda count: self.proj_bar.set_screen_count(count)
                ),
                set_toolbar_screen_count=(
                    lambda count: self._quick_toolbar.set_screen_count(count)
                ),
                monitor_popup=lambda: self._monitor_popup,
                monitor_anchor=lambda: self._quick_toolbar._monitor_btn,
                translate=self.tr,
                dialog_parent=self,
                timer_output=lambda: getattr(self, "timer_output", None),
                timer_bridge=lambda: getattr(self, "timer_bridge", None),
            )
        )

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
            WindowStateContext(
                minimum_width=self.minimumWidth,
                minimum_height=self.minimumHeight,
                resize=self.resize,
                width=self.width,
                height=self.height,
                set_window_icon=self.setWindowIcon,
                move=self.move,
                is_minimized=self.isMinimized,
                show_normal=self.showNormal,
                is_visible=self.isVisible,
                show=self.show,
                raise_window=self.raise_,
                activate_window=self.activateWindow,
                win_id=self.winId,
                titlebar_window=self,
            ),
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
            MainWindowUiContext(
                parent=self,
                event_filter=self,
                set_central_widget=self.setCentralWidget,
                active_profile_name=active_profile.name,
                translate=self.tr,
                lang_manager=self.lang,
                notifications=self.notifications,
                profile_paths=self.profile_paths,
                runtime_paths=self.runtime_paths,
                media_cache_manager=self.media_cache_manager,
                media_controller=self.media_ctrl,
                screen_manager=self.screen_mgr,
                obs_service=self._obs_service,
                ndi_service=self._ndi_service,
                zoom_service=self._zoom_service,
                camera_service=self._camera_service,
                obs_settings=self._obs_settings,
                zoom_settings=self._zoom_settings,
                auto_share_settings=self._auto_share_settings,
                camera_settings=self._camera_settings,
                auto_key_settings=self._auto_key_settings,
                media_settings=self._media_settings,
                meeting_schedule_settings=self._meeting_schedule_settings,
                watched_folder_settings=self._watched_folder_settings,
                yeartext_settings=self._yeartext_settings,
                yeartext_service_factory=lambda parent: YeartextService(
                    cache_file=self.runtime_paths.cache_dir / "yeartext_cache.json",
                    parent=parent,
                ),
                auto_share_accessibility_trusted=lambda: bool(
                    macos_accessibility_trusted()
                ),
                background_song_settings=self._background_song_settings,
                projection_playback_settings=self._projection_playback_settings,
                background_song_service=self._background_song_service,
                timer_bridge=self.timer_bridge,
                playlist_storage_paths=self.playlist_storage_paths,
                playlist_repository=self.playlist_repository,
                meeting_tree_store=self.meeting_tree_store,
                profile_media_store=profile_media_store,
                jwpub_import_thread_factory=jwpub_import_thread_factory,
                document_conversion_service=document_conversion_service,
                clip_fetch_thread_factory=clip_fetch_thread_factory,
                cache_scan_session_factory=cache_scan_session_factory,
                qr_generation_session_factory=qr_generation_session_factory,
                playlist_thumbnail_store=playlist_thumbnail_store,
                meeting_thumbnail_store=meeting_thumbnail_store,
                watched_folder_file_store=watched_folder_file_store,
                watched_folder_playlist_store=watched_folder_playlist_store,
                wifi_receive_server_factory=wifi_receive_server_factory,
                watched_folder_watcher_factory=watched_folder_watcher_factory,
                playlist_cleanup_queue_factory=playlist_cleanup_queue_factory,
                jw_catalog_service_factory=jw_catalog_service_factory,
                jw_catalog_thumbnail_session_factory=(
                    jw_catalog_thumbnail_session_factory
                ),
                jw_songs_store=self.jw_songs_store,
                jwpub_service_factory=lambda parent: JwpubService(
                    self._media_settings,
                    self.media_cache_manager,
                    self.runtime_paths.jwpub_cache_dir,
                    self.jwpub_checksum_store,
                    parent,
                ),
                memorial_service_factory=lambda parent: MemorialService(
                    self.runtime_paths.jwpub_cache_dir,
                    self.jwpub_checksum_store,
                    parent,
                ),
            ),
            MainWindowUiHandlers(
                project_image=lambda data: self._media_projection.project_image_bytes(
                    data
                ),
                project_video=lambda url, title, playlist, playback_order: (
                    self._media_projection.project_video(
                        url,
                        title,
                        playlist,
                        playback_order,
                    )
                ),
                stop_projection=lambda: self._projection_stop.stop_projection(),
                project_tab_frame=lambda frame: (
                    self._media_projection.project_tab_frame(frame)
                ),
                add_current_to_playlist=lambda url, title, meta: (
                    self._playlist_imports.add_current_to_playlist(url, title, meta)
                ),
                add_downloaded_file_to_playlist=lambda path, title, kind: (
                    self._playlist_imports.add_browser_downloaded_file(
                        path,
                        title,
                        kind,
                    )
                ),
                report_download_failure=lambda title, error: (
                    self._playlist_imports.browser_download_failed(title, error)
                ),
                play_cached_media=lambda path, media_type, original_url="", display_title="": (
                    self._media_projection.on_cache_play(
                        path,
                        media_type,
                        original_url,
                        display_title,
                    )
                ),
                wifi_media_received=lambda path, original_name: (
                    self._wifi_playlist_controller.on_wifi_media_received(
                        path,
                        original_name,
                    )
                ),
                wifi_add_single=lambda path, title, original_name: (
                    self._wifi_playlist_controller.on_wifi_request_add_single(
                        path,
                        title,
                        original_name,
                    )
                ),
                wifi_add_all=lambda items: (
                    self._wifi_playlist_controller.on_wifi_send_all_to_playlist(items)
                ),
                wifi_play=lambda path, title: (
                    self._wifi_playlist_controller.on_wifi_request_play(path, title)
                ),
                monitor_manager_requested=(
                    self._projection_targets.on_monitor_manager_requested
                ),
                quick_obs_scene_change=lambda scene_name: (
                    self._live_integrations.on_quick_obs_scene_change(scene_name)
                ),
                quick_obs_return_scene_change=lambda scene_name: (
                    self._live_integrations.on_quick_obs_return_scene_change(
                        scene_name
                    )
                ),
                project_obs_stream=lambda: (
                    self._live_integrations.project_obs_ndi_stream()
                ),
                project_camera_stream=lambda: (
                    self._live_integrations.project_camera_stream()
                ),
                camera_selection_changed=lambda option: (
                    self._live_integrations.on_camera_selection_changed(option)
                ),
                profile_switch_requested=self._profile_switch.request_switch,
            ),
            media_info_queue_factory=media_info_queue_factory,
            media_info_service_factory=media_info_service_factory,
            browser_download_service_factory=browser_download_service_factory,
            browser_image_fetch_service_factory=browser_image_fetch_service_factory,
        )
        self._build_ui()
        self._playlist_imports = PlaylistImportController(
            PlaylistImportContext(
                dialog_parent=self,
                document_conversion_service=document_conversion_service,
                profile_media_store=profile_media_store,
                jwpub_import_thread_factory=jwpub_import_thread_factory,
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
                play_cached_media=lambda *args, **kwargs: (
                    self._media_projection.on_cache_play(*args, **kwargs)
                ),
                project_video=lambda *args, **kwargs: (
                    self._media_projection.project_video(*args, **kwargs)
                ),
            ),
        )
        self._open_media_controller = OpenMediaController(
            OpenMediaContext(
                dialog_parent=self,
                document_conversion_service=document_conversion_service,
                jwpub_import_thread_factory=jwpub_import_thread_factory,
                language_manager=self.lang,
                notifications=self.notifications,
                thread_registry=self._conversion_threads,
                temp_files=self._jwl_tmp_files,
                translate=self.tr,
            ),
            OpenMediaHandlers(
                switch_to_playlist=lambda: self._navigation.switch_page(7),
                project_media_at_index=lambda *args, **kwargs: (
                    self._media_projection.project_media_at_index(*args, **kwargs)
                ),
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
            ProjectionIntegrationContext(
                projection_session=self.projection_session,
                obs_scene_session=self._obs_scene_session,
                auto_key_projection=self._auto_key_projection,
                projection_windows=self.projection_session.all_windows,
                obs_service=self._obs_service,
                obs_settings=self._obs_settings,
                auto_share_settings=self._auto_share_settings,
                projection_bar=self.proj_bar,
                auto_share_finished=self._auto_share_finished.emit,
                start_auto_share=execute_start_share,
                stop_auto_share=execute_stop_share,
                raise_projection_window=raise_projection_window,
            )
        )
        self._media_projection = MediaProjectionController(
            MediaProjectionContext(
                projection_session=self.projection_session,
                projection_bar=self.proj_bar,
                media_controller=self.media_ctrl,
                ndi_service=self._ndi_service,
                camera_service=self._camera_service,
                projection_windows=self.projection_session.all_windows,
                playlist_edit_is_temp=lambda: getattr(
                    self.playlist_widget._edit_view,
                    "_is_temp",
                    False,
                ),
                meeting_service=lambda: self.meetings_widget.get_service(),
                dialog_parent=self,
                translate=self.tr,
                sjjm_announce_mode=self.settings_widget.get_sjjm_announce_mode,
                start_videos_paused=self.settings_widget.get_start_videos_paused,
            ),
            MediaProjectionHandlers(
                stop_browser_tab_projection=(
                    self._navigation.stop_browser_tab_projection
                ),
                update_projection_status=(
                    self._projection_integrations.update_status
                ),
                prepare_video_session=(
                    self._auto_key_projection.prepare_video_session
                ),
            ),
        )
        self._projection_stop = ProjectionStopController(
            ProjectionStopContext(
                projection_session=self.projection_session,
                projection_bar=self.proj_bar,
                media_controller=self.media_ctrl,
                ndi_service=self._ndi_service,
                camera_service=self._camera_service,
                projection_windows=self.projection_session.all_windows,
                auto_share_configured=(
                    self._projection_integrations.auto_share_configured
                ),
            ),
            ProjectionStopHandlers(
                stop_browser_tab_projection=(
                    self._navigation.stop_browser_tab_projection
                ),
                update_projection_status=(
                    self._projection_integrations.update_status
                ),
                set_obs_stream_active=lambda active: (
                    self._live_integrations.set_obs_stream_active(active)
                ),
                set_camera_stream_active=lambda active: (
                    self._live_integrations.set_camera_stream_active(active)
                ),
            ),
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
        self._timer_theme_controller = TimerThemeController(
            TimerThemeContext(
                projection_session=self.projection_session,
                projection_bar=self.proj_bar,
                media_controller=self.media_ctrl,
                ndi_service=self._ndi_service,
                camera_service=self._camera_service,
                projection_windows=self.projection_session.all_windows,
                translate=self.tr,
            ),
            TimerThemeHandlers(
                stop_browser_tab_projection=(
                    self._navigation.stop_browser_tab_projection
                ),
                update_projection_status=(
                    self._projection_integrations.update_status
                ),
            ),
        )
        self._language_controller = LanguageController(
            LanguageContext(
                set_window_title=self.setWindowTitle,
                sidebar_title_label=self._sidebar_title_lbl,
                sidebar_subtitle_label=self._sidebar_subtitle_lbl,
                nav_buttons=self._nav_buttons_by_name,
                translate=self.tr,
            )
        )
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
                    notification_service=NotificationService(
                        self.lang,
                        NotificationSettingsStore.for_profile_settings(
                            self.profile_settings
                        ),
                        self,
                    ),
                    notification_queue=RemoteNotificationQueue(self.lang, self),
                    update_service=UpdateService(self),
                    update_dialog_factory=lambda info: UpdateDialog(
                        info,
                        self,
                        patch_downloader_factory=PatchDownloadWorker,
                        save_cleanup_path=lambda path: save_pending_patch_cleanup(
                            self._installation_settings,
                            path,
                        ),
                        launch_patch=launch_patch_installer,
                    ),
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
        self._install_ui_resources(self._ui_controller.build_ui())

    def _install_ui_resources(self, resources: MainWindowUiResources) -> None:
        self.stack = resources.stack
        self._lazy_pages = resources.lazy_pages
        self._navigation = resources.navigation
        self.right_col = resources.right_col
        self.proj_bar = resources.projection_bar
        self.songs_widget = resources.songs_widget
        self.settings_widget = resources.settings_widget
        self.timer_widget = resources.timer_widget
        self.clips_widget = resources.clips_widget
        self.sermon_theme_widget = resources.sermon_theme_widget
        self.playlist_widget = resources.playlist_widget
        self.meetings_widget = resources.meetings_widget
        self._quick_toolbar = resources.quick_toolbar
        self._sidebar_title_lbl = resources.sidebar_title_label
        self._sidebar_subtitle_lbl = resources.sidebar_subtitle_label
        self._profile_avatar_btn = resources.profile_avatar_button
        self._nav_btns = resources.nav_buttons
        self._nav_buttons_by_name = resources.nav_buttons_by_name
        for attr_name, button in resources.nav_buttons_by_name.items():
            setattr(self, attr_name, button)

    # Signal emitted when user wants to return to profile selector.
    switch_profile_requested = Signal()

    # ── Projection ────────────────────────────────────────────────────────

    def _current_yearly_projection_text(self) -> tuple[str, str, str]:
        quote, reference = self.settings_widget.get_yearly_text()
        return quote, reference, self.settings_widget._current_api_code()

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
