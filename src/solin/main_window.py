from PySide6.QtWidgets import QMainWindow
from PySide6.QtCore import (
    Slot, QTimer, Signal, QDateTime, QEvent
)

from .controllers.auto_key_projection_controller import AutoKeyProjectionController
from .controllers.language_controller import LanguageController
from .controllers.live_integration_controller import LiveIntegrationController
from .controllers.main_window_bootstrap_controller import MainWindowBootstrapController
from .controllers.main_window_ui_controller import MainWindowUiController
from .controllers.media_download_notification_controller import (
    MediaDownloadNotificationController,
)
from .controllers.media_projection_controller import MediaProjectionController
from .controllers.open_media_controller import OpenMediaController
from .controllers.playlist_import_controller import PlaylistImportController
from .controllers.profile_switch_controller import ProfileSwitchController
from .controllers.projection_integration_controller import ProjectionIntegrationController
from .controllers.projection_stop_controller import ProjectionStopController
from .controllers.projection_window_controller import ProjectionWindowController
from .controllers.shutdown_controller import ShutdownController
from .controllers.signal_connection_controller import SignalConnectionController
from .controllers.timer_output_controller import TimerOutputController
from .controllers.timer_theme_controller import TimerThemeController
from .controllers.wifi_playlist_controller import WifiPlaylistController
from .controllers.window_state_controller import WindowStateController
from .core.timer.engine import TimerEngine
from .core.timer.store import TimerStore
from .core.ui.monitor_allocation import MonitorAllocationStore
from .core.ui.window_settings import WindowGeometrySettingsStore
from .projection.window import ProjectionWindow
from .core.i18n.manager import LanguageManager
from .core.jw.background_song_service import BackgroundSongService
from .core.jw.background_song_settings import BackgroundSongSettingsStore
from .core.jw.catalog import JWMediaCatalogCachePaths
from .core.jw.songs import JWSongsStore
from .core.ingest.watched_folder_settings import WatchedFolderSettingsStore
from .core.media.playback import MediaController
from .core.media.cache import MediaCacheManager
from .core.media.settings import MediaSettingsStore
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
from .core.profiles.settings import ProfileSettings
from .core.playlists.storage import PlaylistStoragePaths
from .core.meetings.schedule import MeetingScheduleSettingsStore
from .core.meetings.tree_store import MeetingTreeStore
from .core.meetings.publications import JwpubChecksumStore
from .core.profiles.manager import ProfileManager
from .core.foundation.constants import AUDIO_EXTS as _AUDIO_EXTS_LOCAL

# ── MainWindow ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    _auto_share_finished = Signal(int, bool, bool)

    def __init__(
        self,
        lang_manager: LanguageManager,
        runtime_paths: RuntimePaths,
        profile_paths: ProfilePaths,
        profile_settings: ProfileSettings,
        media_cache_manager: MediaCacheManager,
        font_manager: FontManager,
        jw_catalog_cache_paths: JWMediaCatalogCachePaths,
        jw_songs_store: JWSongsStore,
        jwpub_checksum_store: JwpubChecksumStore,
        playlist_storage_paths: PlaylistStoragePaths,
        meeting_tree_store: MeetingTreeStore,
        profile_manager: ProfileManager,
    ):
        super().__init__()
        self.lang = lang_manager
        self.runtime_paths = runtime_paths
        self.profile_paths = profile_paths
        self.profile_settings = profile_settings
        self.media_cache_manager = media_cache_manager
        self.font_manager = font_manager
        self.jw_catalog_cache_paths = jw_catalog_cache_paths
        self.jw_songs_store = jw_songs_store
        self.jwpub_checksum_store = jwpub_checksum_store
        self.playlist_storage_paths = playlist_storage_paths
        self.meeting_tree_store = meeting_tree_store
        self.profile_manager = profile_manager
        profile_prefs = profile_settings.prefs()
        self.profile_prefs = profile_prefs
        self._obs_settings = OBSSettingsStore.for_profile_settings(profile_settings)
        self._zoom_settings = ZoomSettingsStore.for_profile_settings(profile_settings)
        self._auto_share_settings = AutoShareSettingsStore.for_profile_settings(
            profile_settings,
        )
        self._auto_key_settings = AutoKeySettingsStore.for_profile_settings(
            profile_settings,
        )
        self._camera_settings = CameraSettingsStore.for_profile_settings(profile_settings)
        self._media_settings = MediaSettingsStore.for_profile_settings(profile_settings)
        self._meeting_schedule_settings = (
            MeetingScheduleSettingsStore.for_profile_settings(profile_settings)
        )
        self._watched_folder_settings = WatchedFolderSettingsStore.for_profile_settings(
            profile_settings,
        )
        self._background_song_settings = (
            BackgroundSongSettingsStore.for_profile_settings(profile_settings)
        )
        self.screen_mgr = ScreenManager(self)
        self.media_ctrl = MediaController(
            self._media_settings,
            media_cache_manager,
            self,
        )
        self._auto_keys = AutoKeyDispatcher(self._auto_key_settings, self)
        self._profile_switch = ProfileSwitchController(self, profile_manager)
        self._projection_targets = ProjectionWindowController(self)
        self._live_integrations = LiveIntegrationController(self)
        self._playlist_imports = PlaylistImportController(self, profile_paths)
        self._open_media_controller = OpenMediaController(self, profile_paths)
        self._timer_theme_controller = TimerThemeController(self)
        self._media_projection = MediaProjectionController(self)
        self._wifi_playlist_controller = WifiPlaylistController(self)
        self.projection_windows: list[ProjectionWindow] = []

        # ── Advanced timer + shared monitor allocation ────────────────────────
        # Created before the UI is built so the Timer tab can bind to them.
        # The allocation store is the persistent source of truth for which
        # subsystem (media/timer) owns each monitor — consulted by both the
        # media projection controller and the timer-output controller.
        self._monitor_allocation = MonitorAllocationStore.for_profile_settings(
            profile_settings,
        )
        self._timer_store = TimerStore.for_profile_settings(profile_settings)
        self._timer_engine = TimerEngine(self)
        self._timer_output = TimerOutputController(
            self, self._timer_engine, self._timer_store, self._monitor_allocation
        )
        # Screen names (QScreen.name()) where the user hid media. Restored from
        # the persisted allocation so reconnecting a monitor honours the choice
        # (previously this preference was lost on every restart/reconnect).
        self._deactivated_screens: set[str] = set(
            self._monitor_allocation.media_off_names(ScreenManager.secondary_screens())
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
            self._media_settings,
            self._meeting_schedule_settings,
            jw_songs_store,
            media_cache_manager,
            self,
        )

        self.setWindowTitle(self.tr("Solin"))
        self.setMinimumSize(900, 600)
        self._window_state = WindowStateController(
            self,
            WindowGeometrySettingsStore.for_profile_settings(profile_settings),
        )
        self._shutdown_controller = ShutdownController(self)
        self._window_state.restore_size()
        self._window_state.apply_icon()

        self.notifications = NotificationCenter(self)
        self._media_download_notifications = MediaDownloadNotificationController(
            self.notifications,
            media_cache_manager,
            self,
        )
        self._media_download_notifications.start()

        self._ui_controller = MainWindowUiController(self, profile_manager)
        self._build_ui()
        self._auto_key_projection = AutoKeyProjectionController(self._auto_keys, self.proj_bar)
        self._projection_integrations = ProjectionIntegrationController(self)
        self._projection_stop = ProjectionStopController(self)
        self._language_controller = LanguageController(self)
        self._signal_connections = SignalConnectionController(self, profile_manager)
        self._connect_signals()

        self._bootstrap_controller = MainWindowBootstrapController(self)
        self._bootstrap_controller.finish_startup()

        # Show the clock window on any monitor reserved for the timer once the
        # screens have settled (mirrors the media projection startup timing).
        QTimer.singleShot(900, self._timer_output.reconcile)

    # ── UI Build ──────────────────────────────────────────────────────────

    def _build_ui(self):
        self._ui_controller.build_ui()

    def _build_sidebar(self):
        return self._ui_controller.build_sidebar()

    def _build_bottom_bar(self):
        return self._ui_controller.build_bottom_bar()

    # Signal emitted when user wants to return to profile selector.
    switch_profile_requested = Signal()

    # ── Signals ───────────────────────────────────────────────────────────

    def _connect_signals(self):
        self._signal_connections.connect_signals()

    # ── Projection ────────────────────────────────────────────────────────

    @Slot(str, str, object, str)
    def _on_song_project(self, url: str, title: str, playlist: list, order: str):
        self._media_projection.on_song_project(url, title, playlist, order)

    def _on_sjjm_project(self, url: str, title: str, playlist: list, order: str):
        self._media_projection.on_sjjm_project(url, title, playlist, order)

    def _on_meeting_media_project(self, item):
        self._media_projection.on_meeting_media_project(item)

    def _on_playlist_project(self, url: str, title: str, playlist: list, order: str):
        self._media_projection.on_playlist_project(url, title, playlist, order)

    def _edit_view_is_temp(self) -> bool:
        return self._media_projection.edit_view_is_temp()

    @Slot(str, str)
    def _project_video(self, url: str, title: str,
                       playlist: list | None = None,
                       playback_order: str | None = None,
                       from_saved_playlist: bool = False):
        self._media_projection.project_video(
            url,
            title,
            playlist,
            playback_order,
            from_saved_playlist=from_saved_playlist,
        )

    @Slot(str, str, str)
    def _project_next_auto(self, url: str, title: str, media_type: str):
        self._media_projection.project_next_auto(url, title, media_type)

    def _project_video_core(self, url: str, title: str, keep_expanded: bool = False, is_audio: bool = False):
        self._media_projection.project_video_core(
            url,
            title,
            keep_expanded=keep_expanded,
            is_audio=is_audio,
        )

    @Slot(bytes)
    def _project_image_bytes(self, data: bytes):
        self._media_projection.project_image_bytes(data)

    @Slot(object)
    def _project_tab_frame(self, frame):
        self._media_projection.project_tab_frame(frame)

    def _stop_projection(self):
        self._projection_stop.stop_projection()

    @Slot(float, float, float)
    def _on_image_apply_transform(self, zoom: float, norm_x: float, norm_y: float):
        self._media_projection.on_image_apply_transform(zoom, norm_x, norm_y)

    @Slot()
    def _on_image_reset_transform(self):
        self._media_projection.on_image_reset_transform()

    @Slot()
    def _on_image_reset_transform_instant(self):
        self._media_projection.on_image_reset_transform_instant()

    def _on_cache_play(self, path: str, media_type: str, original_url: str = "", display_title: str = ""):
        self._media_projection.on_cache_play(
            path,
            media_type,
            original_url,
            display_title,
        )

    def _on_wifi_media_received(self, path: str, orig_name: str) -> None:
        self._wifi_playlist_controller.on_wifi_media_received(path, orig_name)

    def _on_wifi_request_play(self, path: str, title: str) -> None:
        self._wifi_playlist_controller.on_wifi_request_play(path, title)

    def _on_wifi_request_add_single(self, path: str, title: str, orig_name: str) -> None:
        self._wifi_playlist_controller.on_wifi_request_add_single(path, title, orig_name)

    def _on_wifi_send_all_to_playlist(self, items: list) -> None:
        self._wifi_playlist_controller.on_wifi_send_all_to_playlist(items)

    def _stop_any(self):
        self._projection_stop.stop_any()

    def _start_timer(self, target_dt: QDateTime):
        self._timer_theme_controller.start_timer(target_dt)

    @Slot(int, int)
    def _on_timer_update_proj(self, remaining: int, total: int):
        self._timer_theme_controller.on_timer_update_proj(remaining, total)

    @Slot(bool)
    def _on_timer_blink_proj(self, on: bool):
        self._timer_theme_controller.on_timer_blink_proj(on)

    @Slot(str)
    def _project_sermon_theme(self, text: str, subtitle: str = ""):
        self._timer_theme_controller.project_sermon_theme(text, subtitle)

    @Slot(str)
    def _on_title_from_metadata(self, title: str):
        self._media_projection.on_title_from_metadata(title)

    def _distribute_frame(self, frame):
        self._media_projection.distribute_frame(frame)

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
        return self._projection_targets.all_windows()

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

    @Slot(int)
    def _on_playlist_navigate(self, index: int):
        self._media_projection.on_playlist_navigate(index)

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

        self._shutdown_controller.shutdown()
        super().closeEvent(event)
