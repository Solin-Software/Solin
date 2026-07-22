from __future__ import annotations

from collections.abc import Callable
from typing import Any, TYPE_CHECKING

from PySide6.QtWidgets import QApplication, QMainWindow, QWidget
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
from .controllers.main_window_profile_settings import MainWindowProfileSettings
from .controllers.main_window_service_factories import MainWindowServiceFactories
from .controllers.main_window_ui_controller import (
    MainWindowUiContext,
    MainWindowUiController,
    MainWindowUiHandlers,
    MainWindowUiResources,
)
from .controllers.media_download_notification_controller import (
    MediaDownloadNotificationController,
)
from .controllers.media_playback_notification_controller import (
    MediaPlaybackNotificationController,
)
from .controllers.media_projection_controller import (
    MediaProjectionContext,
    MediaProjectionController,
    MediaProjectionHandlers,
)
from .controllers.media_tree_runtime import MediaTreeRuntime
from .controllers.media_destination_controller import (
    MediaDestinationContext,
    MediaDestinationController,
)
from .controllers.media_countdown_automation_controller import (
    MediaCountdownAutomationController,
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
from .controllers.playback_protection_controller import (
    PlaybackProtectionController,
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
from .controllers.remote_control_controller import (
    RemoteControlController,
    RemoteControlDependencies,
)
from .controllers.remote_media_thumbnail_extractor import (
    RemoteMediaThumbnailExtractor,
)
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
from .controllers.wifi_media_controller import (
    WifiMediaContext,
    WifiMediaController,
    WifiMediaHandlers,
)
from .controllers.window_state_controller import WindowStateContext, WindowStateController
from .core.projection.aspect_ratio import projection_aspect_ratio_from_windows
from .core.projection.application import ObsSceneSession, ProjectionSession
from .core.timer.application import TimerSession
from .core.i18n.manager import LanguageManager
from .core.integrations.automation.screen_share import (
    execute_start_share,
    execute_stop_share,
    macos_accessibility_trusted,
)
from .core.jw.songs import JWSongsStore
from .core.media.playback import MediaController
from .core.media.cache import MediaCacheManager
from .core.media.profile_store import ProfileMediaStore
from .core.media.thumbnail_store import ThumbnailStore
from .core.media.destinations import MediaDestinationAsset, MediaDestinationRequest
from .core.playlists.items import create_playlist_item
from .core.rendering.fonts import FontManager
from .ui.notifications import NotificationCenter
from .ui.screens import ScreenManager
from .styles.theme import activate_theme, app_stylesheet, apply_application_palette
from .core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from .core.foundation.qt_threads import OwnedQThreadRegistry
from .core.playlists.storage import PlaylistRepository, PlaylistStoragePaths
from .core.meetings.tree_store import MeetingTreeStore
from .core.meetings.jwpub_cache import JwpubChecksumStore
from .core.profiles.models import ProfileInfo
from .widgets.projection.monitor_manager import MonitorManagerPopup
from .ui.qml.timer_bridge import TimerBridge
from .ui.window_focus import raise_projection_window

if TYPE_CHECKING:
    from .core.ingest.watched_folder import WatchedFolderWatcher
    from .core.ingest.watched_folder_files import WatchedFolderFileStore
    from .core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
    from .core.ingest.wifi_server import WifiReceiveServer
    from .ui.qr_generation import QrGenerationSessionFactory
    from .core.jw.clip_fetch import ClipFetchThreadFactory
    from .core.jw.catalog_service import JWMediaCatalogService
    from .core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from .core.jw.thumbnail_fetch import JWCatalogThumbnailSessionFactory
    from .core.meetings.linked_folder_sync import MeetingLinkedFolderSync
    from .core.media.browser_downloads import BrowserDownloadService
    from .core.media.cache_scan import CacheScanSessionFactory
    from .core.network.browser_images import BrowserImageFetchService
    from .core.playlists.cleanup import PlaylistCleanupQueue
    from .core.rendering.document_conversion import DocumentConversionService
    from .ui.media_info import MediaInfoQueue, MediaInfoService

# ── MainWindow ────────────────────────────────────────────────────────────────


class MainWindow(QMainWindow):
    _auto_share_finished = Signal(int, bool, bool)
    _auto_share_mouse_interference_warning = Signal()
    proj_bar: Any
    right_col: Any
    _quick_toolbar: Any

    def __init__(
        self,
        lang_manager: LanguageManager,
        runtime_paths: RuntimePaths,
        profile_paths: ProfilePaths,
        profile_settings_bundle: MainWindowProfileSettings,
        service_factories: MainWindowServiceFactories,
        media_cache_manager: MediaCacheManager,
        media_controller: MediaController,
        background_media_controller: MediaController,
        media_info_queue_factory: Callable[[QObject], MediaInfoQueue],
        media_info_service_factory: Callable[[QObject], MediaInfoService],
        browser_download_service_factory: Callable[[], BrowserDownloadService],
        browser_image_fetch_service_factory: Callable[[], BrowserImageFetchService],
        font_manager: FontManager,
        jw_catalog_service_factory: Callable[[QObject], JWMediaCatalogService],
        jw_catalog_thumbnail_session_factory: JWCatalogThumbnailSessionFactory,
        jw_songs_store: JWSongsStore,
        jwpub_checksum_store: JwpubChecksumStore,
        playlist_storage_paths: PlaylistStoragePaths,
        playlist_repository: PlaylistRepository,
        queue_pending_deletion: Callable[[str], None],
        meeting_tree_store: MeetingTreeStore,
        meeting_linked_folder_sync: MeetingLinkedFolderSync,
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
        self._profile_settings = profile_settings_bundle
        self.media_cache_manager = media_cache_manager
        self.media_tree_runtime = MediaTreeRuntime(
            media_cache_manager.media_cache_dir,
            parent=self,
        )
        watched_folder_playlist_store.bind_resource_lanes(
            self.media_tree_runtime.resource_lanes
        )
        self.media_ctrl = media_controller
        self._background_media_controller = background_media_controller
        self.font_manager = font_manager
        self.jw_songs_store = jw_songs_store
        self.jwpub_checksum_store = jwpub_checksum_store
        self.playlist_storage_paths = playlist_storage_paths
        self.playlist_repository = playlist_repository
        self.meeting_tree_store = meeting_tree_store
        self.meeting_linked_folder_sync = meeting_linked_folder_sync
        self._watched_folder_playlist_store = watched_folder_playlist_store
        self._playlist_thumbnail_store = playlist_thumbnail_store
        self._meeting_thumbnail_store = meeting_thumbnail_store
        self.timer_session = timer_session
        self.active_profile = active_profile
        self._app_settings = profile_settings_bundle.app
        self._obs_settings = profile_settings_bundle.obs
        self._zoom_settings = profile_settings_bundle.zoom
        self._auto_share_settings = profile_settings_bundle.auto_share
        self._auto_key_settings = profile_settings_bundle.auto_key
        self._camera_settings = profile_settings_bundle.camera
        self._media_settings = profile_settings_bundle.media
        self._browser_settings = profile_settings_bundle.browser
        self._projection_playback_settings = profile_settings_bundle.projection_playback
        self._meeting_schedule_settings = profile_settings_bundle.meeting_schedule
        self._media_countdown_settings = profile_settings_bundle.media_countdown
        self._watched_folder_settings = profile_settings_bundle.watched_folder
        self._remote_control_settings = profile_settings_bundle.remote_control
        self._remote_control_credentials = profile_settings_bundle.remote_control_credentials
        self._yeartext_settings = profile_settings_bundle.yeartext
        self._background_song_settings = profile_settings_bundle.background_song
        self.screen_mgr = ScreenManager(self)
        self._monitor_allocation = profile_settings_bundle.monitor_allocation
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
        self._auto_keys = service_factories.auto_key_dispatcher(
            self._auto_key_settings,
            self,
        )
        self._profile_switch = ProfileSwitchController(self.switch_profile_requested.emit)
        self._projection_targets = ProjectionWindowController(
            ProjectionWindowContext(
                session=self.projection_session,
                font_manager=self.font_manager,
                secondary_screens=ScreenManager.secondary_screens,
                sync_projection_integrations=(
                    lambda: self._projection_integrations.sync_projection_integrations()
                ),
                sync_obs_scene=lambda active: self._projection_integrations.sync_obs_scene(active),
                yearly_text=self._current_yearly_projection_text,
                set_projection_screen_count=(lambda count: self.proj_bar.set_screen_count(count)),
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

        self.notifications = NotificationCenter(self)
        self.playback_protection = PlaybackProtectionController(
            self._media_settings,
            self.media_ctrl,
            self,
        )
        self.playback_protection.manualChangeBlocked.connect(
            self._notify_playback_protection_blocked
        )
        self._media_countdown_automation = MediaCountdownAutomationController(
            settings=self._media_countdown_settings,
            schedule_source=self._meeting_schedule_settings,
            projection_session=self.projection_session,
            playback_protection=self.playback_protection,
            notifications=self.notifications,
            parent=self,
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
            media_countdown_automation=self._media_countdown_automation,
            language_manager=lang_manager,
        )

        # OBS WebSocket integration
        self._obs_service = service_factories.obs_websocket(self._obs_settings, self)

        # OBS/DistroAV NDI program stream receiver
        self._ndi_service = service_factories.ndi_receiver(self)

        # Live camera receiver
        self._camera_service = service_factories.camera(self)

        # Zoom Meetings integration
        self._zoom_service = service_factories.zoom(self._zoom_settings, self)

        self._background_song_service = service_factories.background_song(
            self.lang,
            self._background_song_settings,
            self._meeting_schedule_settings,
            jw_songs_store,
            background_media_controller,
            self,
        )

        self.setWindowTitle(self.tr("Solin"))
        self.setMinimumSize(635, 600)
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
            profile_settings_bundle.window_geometry,
        )
        self._window_state.restore_size()
        self._window_state.apply_icon()

        self._auto_share_mouse_interference_warning.connect(
            self._notify_auto_share_mouse_interference
        )
        self._media_download_notifications = MediaDownloadNotificationController(
            self.notifications,
            media_cache_manager,
            self.media_ctrl,
            self,
        )
        self._media_download_notifications.start()

        self._ui_controller = MainWindowUiController(
            MainWindowUiContext(
                parent=self,
                event_filter=self,
                set_central_widget=self.setCentralWidget,
                active_profile_id=active_profile.id,
                active_profile_name=active_profile.name,
                translate=self.tr,
                lang_manager=self.lang,
                notifications=self.notifications,
                profile_paths=self.profile_paths,
                runtime_paths=self.runtime_paths,
                media_cache_manager=self.media_cache_manager,
                media_tree_runtime=self.media_tree_runtime,
                media_controller=self.media_ctrl,
                screen_manager=self.screen_mgr,
                obs_service=self._obs_service,
                ndi_service=self._ndi_service,
                zoom_service=self._zoom_service,
                camera_service=self._camera_service,
                app_settings=self._app_settings,
                obs_settings=self._obs_settings,
                zoom_settings=self._zoom_settings,
                auto_share_settings=self._auto_share_settings,
                camera_settings=self._camera_settings,
                auto_key_settings=self._auto_key_settings,
                media_settings=self._media_settings,
                playback_protection=self.playback_protection,
                browser_settings=self._browser_settings,
                meeting_schedule_settings=self._meeting_schedule_settings,
                watched_folder_settings=self._watched_folder_settings,
                remote_control_settings=self._remote_control_settings,
                remote_control_credentials=self._remote_control_credentials,
                yeartext_settings=self._yeartext_settings,
                yeartext_service_factory=service_factories.yeartext,
                font_manager=self.font_manager,
                yearly_projection_text=self._current_yearly_projection_text,
                auto_share_accessibility_trusted=lambda: bool(macos_accessibility_trusted()),
                background_song_settings=self._background_song_settings,
                projection_playback_settings=self._projection_playback_settings,
                window_geometry_settings=profile_settings_bundle.window_geometry,
                projection_aspect_ratio_provider=(
                    lambda: projection_aspect_ratio_from_windows(
                        tuple(self.projection_session.projection_windows)
                    )
                ),
                background_song_service=self._background_song_service,
                timer_bridge=self.timer_bridge,
                playlist_storage_paths=self.playlist_storage_paths,
                playlist_repository=self.playlist_repository,
                meeting_tree_store=self.meeting_tree_store,
                meeting_linked_folder_sync=self.meeting_linked_folder_sync,
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
                jw_catalog_thumbnail_session_factory=(jw_catalog_thumbnail_session_factory),
                jw_songs_store=self.jw_songs_store,
                jwpub_service_factory=service_factories.jwpub,
                memorial_service_factory=service_factories.memorial,
            ),
            MainWindowUiHandlers(
                project_image=lambda data: self._media_projection.project_image_bytes(data),
                project_video=lambda url, title, playlist, playback_order: (
                    self._media_projection.project_video(
                        url,
                        title,
                        playlist,
                        playback_order,
                    )
                ),
                stop_projection=lambda: self._projection_stop.stop_projection(),
                project_tab_frame=lambda frame: self._media_projection.project_tab_frame(frame),
                browser_media_destination=lambda url, title, kind, can_play: (
                    self._route_browser_destination(
                        url,
                        title,
                        kind,
                        can_play,
                    )
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
                    self._wifi_media_controller.on_wifi_media_received(
                        path,
                        original_name,
                    )
                ),
                wifi_add_single=lambda path, title, original_name: (
                    self._wifi_media_controller.on_wifi_request_add_single(
                        path,
                        title,
                        original_name,
                    )
                ),
                wifi_add_all=lambda items: self._wifi_media_controller.on_wifi_add_all(items),
                wifi_play=lambda path, title: self._wifi_media_controller.on_wifi_request_play(
                    path, title
                ),
                monitor_manager_requested=(self._projection_targets.on_monitor_manager_requested),
                quick_obs_scene_change=lambda scene_name: (
                    self._live_integrations.on_quick_obs_scene_change(scene_name)
                ),
                quick_obs_return_scene_change=lambda scene_name: (
                    self._live_integrations.on_quick_obs_return_scene_change(scene_name)
                ),
                project_obs_stream=lambda: self._live_integrations.project_obs_ndi_stream(),
                project_camera_stream=lambda: self._live_integrations.project_camera_stream(),
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
        self._media_destinations = MediaDestinationController(
            MediaDestinationContext(
                dialog_parent=self,
                playlist_widget=self.playlist_widget,
                meetings_widget=self.meetings_widget,
                playlist_imports=self._playlist_imports,
                notifications=self.notifications,
                translate=self.tr,
            ),
            parent=self,
        )
        self._wifi_media_controller = WifiMediaController(
            WifiMediaContext(
                destination_controller=self._media_destinations,
                wifi_receive_widget=lambda: self._lazy_pages.wifi_receive_widget,
                translate=self.tr,
            ),
            WifiMediaHandlers(
                play_cached_media=lambda *args, **kwargs: self._media_projection.on_cache_play(
                    *args, **kwargs
                ),
                project_video=lambda *args, **kwargs: self._media_projection.project_video(
                    *args, **kwargs
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
                open_pdf_temp_playlist=(self.playlist_widget.open_pdf_as_temp_playlist),
                open_named_temp_playlist=lambda items, name: (
                    self.playlist_widget.open_temp_playlist(
                        items,
                        self.lang,
                        name=name,
                    )
                ),
                append_temp_playlist_items=(self.playlist_widget.append_temp_playlist_items),
                import_native_playlists=self._import_native_playlists_from_shell,
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
                interaction_guard=self.playback_protection,
                auto_share_finished=self._auto_share_finished.emit,
                start_auto_share=execute_start_share,
                stop_auto_share=execute_stop_share,
                auto_share_mouse_interference_warning=(
                    self._auto_share_mouse_interference_warning.emit
                ),
                raise_projection_window=raise_projection_window,
                auto_share_workers=service_factories.auto_share_workers(),
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
                playback_protection=self.playback_protection,
                projection_aspect_ratio_provider=(
                    lambda: projection_aspect_ratio_from_windows(
                        tuple(self.projection_session.projection_windows)
                    )
                ),
            ),
            MediaProjectionHandlers(
                stop_browser_tab_projection=(self._navigation.stop_browser_tab_projection),
                update_projection_status=(self._projection_integrations.update_status),
                prepare_video_session=(self._auto_key_projection.prepare_video_session),
                prepare_auto_share_playback=(
                    self._projection_integrations.prepare_video_playback_for_auto_share
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
                auto_share_configured=(self._projection_integrations.auto_share_configured),
            ),
            ProjectionStopHandlers(
                stop_browser_tab_projection=(self._navigation.stop_browser_tab_projection),
                update_projection_status=(self._projection_integrations.update_status),
                set_obs_stream_active=lambda active: self._live_integrations.set_obs_stream_active(
                    active
                ),
                set_camera_stream_active=lambda active: (
                    self._live_integrations.set_camera_stream_active(active)
                ),
            ),
        )
        self._media_playback_notifications = MediaPlaybackNotificationController(
            self.notifications,
            self.media_ctrl,
            current_title=self.proj_bar.current_media_title,
            stop_projection=self._projection_stop.stop_projection,
            parent=self,
        )
        self._media_playback_notifications.start()
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
                playback_protection=self.playback_protection,
            ),
            LiveIntegrationHandlers(
                stop_projection=self._projection_stop.stop_projection,
                stop_browser_tab_projection=(self._navigation.stop_browser_tab_projection),
                update_projection_status=(self._projection_integrations.update_status),
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
                playback_protection=self.playback_protection,
            ),
            TimerThemeHandlers(
                stop_browser_tab_projection=(self._navigation.stop_browser_tab_projection),
                update_projection_status=(self._projection_integrations.update_status),
            ),
        )
        self._language_controller = LanguageController(
            LanguageContext(
                set_window_title=self.setWindowTitle,
                sidebar_title_label=self._sidebar_title_lbl,
                sidebar_subtitle_label=self._sidebar_subtitle_lbl,
                nav_buttons=self._nav_buttons_by_name,
                translate=self.tr,
                sidebar_chrome=self._sidebar_chrome,
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
                playback_protection=self.playback_protection,
                screen_manager=self.screen_mgr,
                language_manager=self.lang,
                settings_widget=self.settings_widget,
                obs_service=self._obs_service,
                ndi_service=self._ndi_service,
                camera_service=self._camera_service,
                auto_share_finished=self._auto_share_finished,
                media_countdown_automation=self._media_countdown_automation,
            ),
            MainWindowSignalHandlers(
                media_projection=self._media_projection,
                timer_theme=self._timer_theme_controller,
                playlist_imports=self._playlist_imports,
                media_destinations=self._media_destinations,
                auto_key_projection=self._auto_key_projection,
                projection_stop=self._projection_stop,
                projection_targets=self._projection_targets,
                language_controller=self._language_controller,
                live_integrations=self._live_integrations,
                background_song_service=self._background_song_service,
                projection_integrations=self._projection_integrations,
                apply_theme=self._apply_theme,
                timer_output=self.timer_output,
                timer_bridge=self.timer_bridge,
                open_meeting_schedule_settings=self._open_meeting_schedule_settings,
            ),
        )
        self._signal_connections.connect_signals()

        self._remote_media_thumbnail_extractor = RemoteMediaThumbnailExtractor(
            media_info_queue_factory,
            self,
        )
        self._remote_control = RemoteControlController(
            RemoteControlDependencies(
                runtime_paths=self.runtime_paths,
                active_profile_id=self.active_profile.id,
                active_profile_name=self.active_profile.name,
                active_profile_locale=lambda: self.lang.current_code,
                settings=self._remote_control_settings,
                credentials=self._remote_control_credentials,
                playlist_repository=self.playlist_repository,
                meeting_tree_store=self.meeting_tree_store,
                watched_folder_settings=self._watched_folder_settings,
                watched_folder_playlist_store=self._watched_folder_playlist_store,
                playlist_thumbnail_store=self._playlist_thumbnail_store,
                meeting_thumbnail_store=self._meeting_thumbnail_store,
                media_thumbnail_extractor=self._remote_media_thumbnail_extractor,
                projection_session=self.projection_session,
                projection_bar=self.proj_bar,
                media_controller=self.media_ctrl,
                media_projection=self._media_projection,
                projection_stop=self._projection_stop,
                playback_protection=self.playback_protection,
                settings_widget=self.settings_widget,
            ),
            self,
        )
        self._remote_control.runtime_status_reported.connect(
            self._quick_toolbar.set_remote_control_status
        )
        self._remote_control.session_inventory_reported.connect(
            self._quick_toolbar.set_remote_sessions
        )
        self._remote_control.session_revocation_reported.connect(
            self._quick_toolbar.set_remote_session_revocation_result
        )
        self._quick_toolbar.remote_session_disconnect_requested.connect(
            self._remote_control.revoke_session
        )
        self._quick_toolbar.remote_sessions_disconnect_all_requested.connect(
            self._remote_control.revoke_sessions
        )
        self.lang.language_changed.connect(self._remote_control.on_language_changed)

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
                media_countdown_automation=self._media_countdown_automation,
                monitor_popup_factory=lambda: MonitorManagerPopup(self),
                ipc_controller_factory=lambda: IpcController(
                    self,
                    bring_to_front=self._bring_to_front,
                    open_media_files=self.open_media_files,
                ),
                remote_services_factory=lambda: service_factories.remote_services(self),
                apply_stylesheet=self._apply_global_stylesheet,
            )
        )
        startup_resources = self._bootstrap_controller.finish_startup()
        self._monitor_popup = startup_resources.monitor_popup
        self._ipc_controller = startup_resources.ipc_controller
        self._remote_services = startup_resources.remote_services
        self._remote_control.start()
        self._shutdown_controller = ShutdownController(
            ShutdownDependencies(
                projection_session=self.projection_session,
                timer_output=self.timer_output,
                services=ShutdownServices(
                    remote_control=self._remote_control,
                    remote_services=self._remote_services,
                    download_notifications=self._media_download_notifications,
                    playback_notifications=self._media_playback_notifications,
                    notifications=self.notifications,
                    projection_integrations=self._projection_integrations,
                    background_song=self._background_song_service,
                    media_countdown_automation=self._media_countdown_automation,
                    media_tree_runtime=self.media_tree_runtime,
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
                queue_pending_deletion=queue_pending_deletion,
                save_window_state=self._window_state.save_size,
                cleanup_lazy_pages=self._lazy_pages.cleanup_browser,
            )
        )

        # Show the clock window on any monitor reserved for the timer once the
        # screens have settled (mirrors the media projection startup timing).
        QTimer.singleShot(900, self.timer_output.reconcile)

    def _route_browser_destination(
        self,
        url: str,
        title: str,
        kind: str,
        can_play: bool,
    ) -> None:
        browser = self._lazy_pages.browser_widget
        if browser is None or not url:
            return

        request = self._browser_destination_request(
            url=url,
            title=title,
            kind=kind,
            can_play=can_play,
        )
        needs_preparation = kind in {"pdf", "jwpub", "jwlplaylist"} or (
            kind == "image" and url.startswith(("http://", "https://"))
        )

        def prepare(ready, failed) -> None:
            browser.prepare_destination_media(
                url,
                title,
                kind,
                on_ready=lambda path, prepared_title, prepared_kind: ready(
                    self._browser_destination_request(
                        url=path,
                        title=prepared_title,
                        kind=prepared_kind,
                        can_play=can_play,
                        prepared=True,
                    )
                ),
                on_failed=lambda _failed_title, error: failed(str(error)),
            )

        self._media_destinations.route(
            request,
            play=lambda: browser.play_destination_media(url, kind),
            prepare=prepare if needs_preparation else None,
        )

    def _open_meeting_schedule_settings(self) -> None:
        self._navigation.switch_page(6)
        self.settings_widget.focus_meeting_schedule()

    @staticmethod
    def _browser_destination_request(
        *,
        url: str,
        title: str,
        kind: str,
        can_play: bool,
        prepared: bool = False,
    ) -> MediaDestinationRequest:
        if kind in {"image", "audio", "video"}:
            item = create_playlist_item(title=title, url=url, type=kind)
            return MediaDestinationRequest(
                title=title,
                assets=(
                    MediaDestinationAsset(
                        title=title,
                        source_id=url,
                        item=item,
                    ),
                ),
                can_play=can_play,
            )
        if not prepared:
            return MediaDestinationRequest(title=title, can_play=can_play)
        return MediaDestinationRequest(
            title=title,
            assets=(
                MediaDestinationAsset(
                    title=title,
                    source_id=url,
                    import_path=url,
                    import_kind=kind,
                ),
            ),
            can_play=can_play,
        )

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
        self._sidebar_chrome = resources.sidebar_chrome
        self._nav_btns = resources.nav_buttons
        self._nav_buttons_by_name = resources.nav_buttons_by_name
        for attr_name, button in resources.nav_buttons_by_name.items():
            setattr(self, attr_name, button)

    def _apply_global_stylesheet(self, stylesheet: str) -> None:
        app = QApplication.instance()
        if app is not None:
            apply_application_palette(app)
            app.setStyleSheet(stylesheet)
        self.setStyleSheet(stylesheet)

    def _apply_theme(self, theme_id: str) -> None:
        theme = activate_theme(theme_id)
        self._apply_global_stylesheet(app_stylesheet(theme))
        self._window_state.apply_titlebar_color()
        self._refresh_theme_chrome()

    def _refresh_theme_chrome(self) -> None:
        for button in getattr(self, "_nav_btns", ()):
            if hasattr(button, "apply_theme"):
                button.apply_theme()

        for widget in (
            getattr(self, "songs_widget", None),
            getattr(self, "clips_widget", None),
            getattr(self, "settings_widget", None),
            getattr(self, "timer_widget", None),
            getattr(self, "sermon_theme_widget", None),
            getattr(self, "playlist_widget", None),
            getattr(self, "meetings_widget", None),
            getattr(self, "_lazy_pages", None),
            getattr(self, "proj_bar", None),
            getattr(self, "_quick_toolbar", None),
            getattr(self, "_monitor_popup", None),
            getattr(self, "_profile_avatar_btn", None),
        ):
            if widget is None:
                continue
            if hasattr(widget, "apply_theme"):
                widget.apply_theme()
            if hasattr(widget, "update"):
                widget.update()

        for widget in self.findChildren(QWidget):
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
            widget.update()
        self.update()

    # Signal emitted when user wants to return to profile selector.
    switch_profile_requested = Signal()

    # ── Projection ────────────────────────────────────────────────────────

    def _current_yearly_projection_text(self) -> tuple[str, str, str]:
        quote, reference = self.settings_widget.get_yearly_text()
        return quote, reference, self.settings_widget._current_api_code()

    def _notify_playback_protection_blocked(self) -> None:
        self.notifications.warning(
            self.tr("Pause playback before changing the projected content."),
            title=self.tr("Playback protection"),
            dedupe_key="playback-protection:manual-change",
        )

    def _notify_auto_share_mouse_interference(self) -> None:
        self.notifications.warning(
            self.tr("Keep the mouse still while Solin selects the share target."),
            title=self.tr("Auto Screen Share"),
            dedupe_key="auto-share:mouse-interference",
        )

    def _edit_view_is_temp(self) -> bool:
        return self._media_projection.edit_view_is_temp()

    def _project_video_core(
        self, url: str, title: str, keep_expanded: bool = False, is_audio: bool = False
    ):
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

    def _import_native_playlists_from_shell(
        self,
        paths: list[str],
        open_after: bool,
    ) -> None:
        self._navigation.switch_page(7)
        self.playlist_widget.import_native_playlists(paths, open_after=open_after)

    def _project_media_at_index(
        self,
        playlist: list,
        index: int = 0,
        keep_expanded: bool = False,
        playback_order: str | None = None,
    ):
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
