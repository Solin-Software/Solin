from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
import logging
from typing import Any, TYPE_CHECKING

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from PySide6.QtCore import QObject, QPoint, QTimer, Signal, Slot, QEvent, Qt
from PySide6.QtGui import QImage

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
from .controllers.program_recording_controller import ProgramRecordingController
from .controllers.scene_runtime_controller import SceneRuntimeController
from .controllers.content_frame_ingress_controller import ContentFrameIngressController
from .core.scenes.libobs_engine import libobs_scene_engine_selected
from .controllers.program_content_controller import ProgramContentController
from .controllers.scene_frame_egress_controller import (
    SceneFrameEgressController,
    SceneVideoFrameEgressController,
    video_frame_to_image,
)
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
from .controllers.main_window_nav import MainPage
from .controllers.timer_engine import TimerEngine
from .controllers.timer_monitor_controller import TimerMonitorController
from .controllers.timer_output_controller import TimerOutputController
from .controllers.timer_pdf_export_controller import TimerPdfExportController
from .controllers.timer_projection_controller import (
    TimerProjectionContext,
    TimerProjectionController,
    TimerProjectionHandlers,
)
from .controllers.wifi_media_controller import (
    WifiMediaContext,
    WifiMediaController,
    WifiMediaHandlers,
)
from .controllers.window_state_controller import WindowStateContext, WindowStateController
from .core.projection.aspect_ratio import projection_aspect_ratio_from_windows
from .core.projection.application import (
    ObsSceneSession,
    ProjectionSession,
    projection_presentation_type,
)
from .core.scenes.engine import MAXIMUM_OUTPUT_WINDOW_TARGETS, OutputWindowTarget
from .core.scenes.model import DELIVERY_BUSES
from .core.scenes.model import BusId, CONTENT_SOURCE_ID, SceneDocument
from .core.scenes.recording import ProgramRecordingState, ProgramRecordingStatus
from .core.scenes.workspace import SceneWorkspaceService
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
from .core.foundation.constants import NATIVE_SCENES_SUPPORTED
from .core.foundation.qt_threads import OwnedQThreadRegistry
from .core.playlists.storage import PlaylistRepository, PlaylistStoragePaths
from .core.meetings.tree_store import MeetingTreeStore
from .core.foundation.resource_lanes import ResourceLaneRegistry
from .core.meetings.jwpub_cache import JwpubChecksumStore
from .core.profiles.models import ProfileInfo
from .widgets.projection.monitor_manager import MonitorManagerPopup
from .ui.qml.timer_bridge import TimerBridge
from .ui.window_focus import raise_projection_window


_STARTUP_SCREEN_SETTLE_MS = 900
_OPERATOR_VIDEO_OUTPUT_TARGET_ID = "media-control-video"


def _use_native_media_presentation(
    *,
    native_window_routing_ready: bool,
    mirror_enabled: bool,
    raw_visual: bool,
) -> bool:
    """Keep every media surface on the same native routing policy."""
    return native_window_routing_ready and (mirror_enabled or raw_visual)


def _native_scene_routing_supported() -> bool:
    """Whether the sidecar can paint scene video into shared native window handles.

    True on the Windows native engine, and under the libobs engine on any platform
    whose top-level window handles can be shared cross-process with the sidecar so
    it can bind an ``obs_display`` to them: X11/XWayland (``xcb``) or Windows.
    Native Wayland cannot share a window handle, so those sessions keep the
    CPU-readback (egress) path instead of direct native painting.
    """
    if NATIVE_SCENES_SUPPORTED:
        return True
    if not libobs_scene_engine_selected():
        return False
    from PySide6.QtGui import QGuiApplication

    return QGuiApplication.platformName() in ("xcb", "windows")


log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .bootstrap.application_window import ApplicationWindow
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
    from .core.media.cache_delete import CacheDeletionSessionFactory
    from .core.network.browser_images import BrowserImageFetchService
    from .core.scenes.engine import SceneEngine
    from .core.scenes.ptz import PtzCredentialVault, PtzRecallExecutor
    from .core.playlists.cleanup import PlaylistCleanupQueue
    from .core.rendering.document_conversion import DocumentConversionService
    from .ui.media_info import MediaInfoQueue, MediaInfoService

# ── MainWindow ────────────────────────────────────────────────────────────────


class MainWindow(QWidget):
    first_frame_presented = Signal()
    _benchmark_close_requested = Signal()
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
        resource_lanes: ResourceLaneRegistry,
        meeting_linked_folder_sync: MeetingLinkedFolderSync,
        profile_media_store: ProfileMediaStore,
        jwpub_import_thread_factory: JwpubImportThreadFactory,
        document_conversion_service: DocumentConversionService,
        clip_fetch_thread_factory: ClipFetchThreadFactory,
        cache_scan_session_factory: CacheScanSessionFactory,
        cache_deletion_session_factory: CacheDeletionSessionFactory,
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
        *,
        talk_theme_output_settings: Any,
        scene_workspace: SceneWorkspaceService,
        window_host: ApplicationWindow,
        scene_engine: SceneEngine | None = None,
        ptz_executor: PtzRecallExecutor | None = None,
        ptz_credentials: PtzCredentialVault | None = None,
    ):
        super().__init__(window_host.content_parent())
        from .bootstrap.startup_timeline import startup_timeline

        startup = startup_timeline()
        startup.mark("main_window_constructor_started")
        self._window_host = window_host
        self._benchmark_close_requested.connect(
            window_host.close,
            Qt.ConnectionType.QueuedConnection,
        )
        self._content_layout = QVBoxLayout(self)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        self._construction_cleanup = ExitStack()
        self._construction_cleanup.callback(self._abort_partial_resources)
        self._construction_finalized = False
        window_host.register_runtime_candidate(self)
        self.lang = lang_manager
        self.runtime_paths = runtime_paths
        self.profile_paths = profile_paths
        self._profile_settings = profile_settings_bundle
        self.media_cache_manager = media_cache_manager
        self.media_tree_runtime = MediaTreeRuntime(
            media_cache_manager.media_cache_dir,
            resource_lanes=resource_lanes,
            parent=self,
        )
        watched_folder_playlist_store.bind_resource_lanes(self.media_tree_runtime.resource_lanes)
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
        self.scene_workspace = scene_workspace
        program_output = self.scene_documents.document.output(BusId.VIRTUAL_CAMERA)
        if libobs_scene_engine_selected():
            # The libobs engine consumes a cross-platform SHARED_MEMORY_BGRA
            # channel; publish content through it and skip the D3D11 transport.
            from .core.scenes.content_frame_publisher import SharedMemoryContentPublisher

            self._content_frame_ingress = ContentFrameIngressController(
                self,
                canvas_width=program_output.video_format.width,
                canvas_height=program_output.video_format.height,
                publisher_factory=lambda width, height: SharedMemoryContentPublisher(width, height),
                accelerated_publisher_factory=lambda width, height: None,
            )
        else:
            self._content_frame_ingress = ContentFrameIngressController(
                self,
                canvas_width=program_output.video_format.width,
                canvas_height=program_output.video_format.height,
            )
        # Fork A: with the libobs engine, local media files decode in the sidecar.
        # Route the foreground controller through the engine and mirror its
        # media_playback_state events back onto the controller's usual signals.
        self._unsubscribe_media_engine = None
        if libobs_scene_engine_selected() and scene_engine is not None:
            from .controllers.media_engine_route import SceneEngineMediaRoute
            from .core.scenes.media_control import MEDIA_SLOT_BACKGROUND

            self.media_ctrl.set_engine_media_route(SceneEngineMediaRoute(scene_engine))
            # The background song plays through a second, monitored-only sidecar
            # audio slot (not composited into the scene).
            self._background_media_controller.set_engine_media_route(
                SceneEngineMediaRoute(scene_engine, slot=MEDIA_SLOT_BACKGROUND)
            )
            self._unsubscribe_media_engine = scene_engine.subscribe(
                self._on_engine_media_event
            )
        self._program_content = ProgramContentController(
            self.projection_session,
            self.font_manager,
            self._content_frame_ingress.submit_frame,
            self._current_yearly_projection_text,
            media_epoch_sink=self._content_frame_ingress.begin_presentation,
            image_transform_sink=self._content_frame_ingress.set_image_transform,
            # Lazy: scene_runtime is constructed just after this controller.
            yeartext_reloaded=lambda: self.scene_runtime.reload_yeartext(),
            width=program_output.video_format.width,
            height=program_output.video_format.height,
            parent=self,
        )
        # Content identity and retained pixels must advance before the scene
        # observer prepares Program. ProjectionSession preserves subscription
        # order, so constructing these owners in data-flow order removes the
        # first-frame race without coupling either controller to the other.
        self.scene_runtime = SceneRuntimeController(
            scene_workspace,
            self.projection_session,
            engine=scene_engine,
            ptz=ptz_executor,
            parent=self,
        )
        self._program_recording = ProgramRecordingController(
            scene_workspace,
            self.scene_runtime,
            engine=scene_engine,
            profile_paths=profile_paths,
            parent=self,
        )
        self._content_frame_ingress.descriptor_changed.connect(
            self.scene_runtime.set_content_ingress
        )
        self.scene_runtime.source_health_changed.connect(
            self._on_native_content_source_health_changed
        )
        self.scene_runtime.set_content_ingress(self._content_frame_ingress.descriptor)
        self.scene_runtime.content_ingress_demand_changed.connect(
            self._on_content_ingress_demand_changed
        )
        self._on_content_ingress_demand_changed(
            self.scene_runtime.content_ingress_required
        )
        self._obs_scene_session = ObsSceneSession()
        self._monitor_popup = None
        self._ipc_controller = None
        self._remote_services = None
        self._remote_control = None
        self._remote_media_thumbnail_extractor = None
        self._media_info_queue_factory = media_info_queue_factory
        self._first_frame_presented = False
        self._deferred_startup_started = False
        self._application_maintenance = service_factories.application_maintenance
        self._jwl_tmp_files: set[str] = set()
        self._conversion_threads = OwnedQThreadRegistry()
        self._shutdown_controller = None
        self._auto_keys = service_factories.auto_key_dispatcher(
            self._auto_key_settings,
            self,
        )
        self._profile_switch = ProfileSwitchController(
            self.switch_profile_requested.emit,
            can_switch=lambda: not self._program_recording.busy,
            notify_blocked=self._notify_recording_blocks_profile_switch,
        )
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
                content_frame_sink=self._program_content.submit_idle_frame,
                refresh_program_content=self._program_content.refresh,
                set_projection_screen_count=(lambda count: self.proj_bar.set_screen_count(count)),
                set_toolbar_screen_count=(
                    lambda count: self._quick_toolbar.set_screen_count(count)
                ),
                monitor_popup=lambda: self._monitor_popup,
                monitor_anchor=lambda: self._quick_toolbar._monitor_btn,
                dialog_parent=self,
                timer_output=lambda: getattr(self, "timer_output", None),
                timer_bridge=lambda: getattr(self, "timer_bridge", None),
                program_mirror_enabled=self._program_mirror_enabled,
                program_content_requested=self._program_content_requested,
                native_outputs_changed=self._reconcile_native_scene_surfaces,
            )
        )
        self._media_mirror_was_enabled = self._program_mirror_enabled()
        self._native_fallback_mirror_required = False
        self._native_window_output_suppressed = False
        self._unsubscribe_native_projection_state = self.projection_session.subscribe(
            self._on_projection_state_changed_for_native
        )
        self.scene_runtime.runtime_changed.connect(self._on_scene_window_route_changed)
        self.destroyed.connect(
            lambda _object=None: self._unsubscribe_native_projection_state()
        )
        if self._unsubscribe_media_engine is not None:
            self.destroyed.connect(
                lambda _object=None: self._unsubscribe_media_engine()
            )
        document = self.scene_documents.document
        preview_output = document.output(BusId.MEDIA_WINDOWS)
        program_output = document.output(BusId.VIRTUAL_CAMERA)
        if libobs_scene_engine_selected():
            # The libobs sidecar composites the edited scene into a
            # cross-platform BGRA block the app owns (see libobs_preview_egress).
            from .controllers.shared_memory_preview_egress import (
                SharedMemoryPreviewEgressController,
            )

            self._scene_preview_egress = SharedMemoryPreviewEgressController(
                preview_output.video_format.width,
                preview_output.video_format.height,
                self,
            )
        else:
            self._scene_preview_egress = SceneFrameEgressController(
                preview_output.video_format.width,
                preview_output.video_format.height,
                self,
                worker_name="solin-scene-preview-egress",
            )
        self._scene_preview_egress.descriptor_changed.connect(
            self._on_scene_frame_egress_descriptor_changed
        )
        self._scene_preview_egress.frame_ready.connect(
            self._on_scene_preview_frame
        )
        if libobs_scene_engine_selected():
            # The libobs sidecar mirrors the program main mix into a
            # cross-platform BGRA block for the operator Program tab.
            from .controllers.shared_memory_preview_egress import (
                SharedMemoryPreviewEgressController,
            )

            self._scene_program_egress = SharedMemoryPreviewEgressController(
                program_output.video_format.width,
                program_output.video_format.height,
                self,
                channel_id="solin-program",
            )
        else:
            self._scene_program_egress = SceneVideoFrameEgressController(
                program_output.video_format.width,
                program_output.video_format.height,
                self,
                worker_name="solin-scene-program-egress",
            )
        self._scene_program_egress.descriptor_changed.connect(
            self._on_scene_frame_egress_descriptor_changed
        )
        self._scene_program_egress.frame_ready.connect(
            self._on_scene_program_frame
        )
        self.scene_runtime.set_preview_egress(self._scene_preview_egress.descriptor)
        self.scene_runtime.set_program_egress(None)
        self.scene_runtime.document_changed.connect(
            self._on_scene_document_changed_for_egress
        )

        self.notifications = NotificationCenter(self)
        self._last_program_recording_status = ProgramRecordingStatus.IDLE
        self._program_recording.state_changed.connect(
            self._on_program_recording_state_changed
        )
        if self._program_recording.state.status is ProgramRecordingStatus.FAILED:
            self._on_program_recording_state_changed(self._program_recording.state)
        self.scene_runtime.transition_fallback.connect(
            self._on_scene_transition_fallback
        )
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
        self._timer_output_startup_timer = QTimer(self)
        self._timer_output_startup_timer.setSingleShot(True)
        self._timer_output_startup_timer.timeout.connect(self._reconcile_timer_output_after_startup)
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

        self._camera_service = (
            service_factories.camera(self)
            if service_factories.camera is not None
            else None
        )

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

        self._window_host.setWindowTitle(self.tr("Solin"))
        self._window_state = WindowStateController(
            WindowStateContext(
                width=self._window_host.width,
                height=self._window_host.height,
                move=self._window_host.move,
                is_minimized=self._window_host.isMinimized,
                show_normal=self._window_host.showNormal,
                is_visible=self._window_host.isVisible,
                show=self._window_host.show,
                raise_window=self._window_host.raise_,
                activate_window=self._window_host.activateWindow,
                win_id=self._window_host.winId,
                titlebar_window=self._window_host,
            )
        )

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
                set_central_widget=self._set_content_widget,
                active_profile_id=active_profile.id,
                active_profile_name=active_profile.name,
                translate=self.tr,
                lang_manager=self.lang,
                notifications=self.notifications,
                profile_paths=self.profile_paths,
                projection_session=self.projection_session,
                scene_runtime=self.scene_runtime,
                program_recording=self._program_recording,
                ptz_credentials=ptz_credentials,
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
                talk_theme_settings=profile_settings_bundle.talk_theme,
                talk_theme_output_settings=talk_theme_output_settings,
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
                cache_deletion_session_factory=cache_deletion_session_factory,
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
                camera_selection_changed=(
                    lambda option: self._live_integrations.on_camera_selection_changed(option)
                ),
                profile_switch_requested=self._profile_switch.request_switch,
            ),
            media_info_queue_factory=media_info_queue_factory,
            media_info_service_factory=media_info_service_factory,
            browser_download_service_factory=browser_download_service_factory,
            browser_image_fetch_service_factory=browser_image_fetch_service_factory,
        )
        self._build_ui()
        startup.mark("main_window_tree_built")
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
            ),
            PlaylistImportHandlers(
                switch_to_playlist=lambda: self._navigation.switch_page(int(MainPage.PLAYLISTS)),
            ),
        )
        self._media_destinations = MediaDestinationController(
            MediaDestinationContext(
                dialog_parent=self,
                playlist_widget=self.playlist_widget,
                meetings_widget=self.meetings_widget,
                playlist_imports=self._playlist_imports,
                notifications=self.notifications,
            ),
            parent=self,
        )
        self._wifi_media_controller = WifiMediaController(
            WifiMediaContext(
                destination_controller=self._media_destinations,
                wifi_receive_widget=lambda: self._lazy_pages.wifi_receive_widget,
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
            ),
            OpenMediaHandlers(
                switch_to_playlist=lambda: self._navigation.switch_page(int(MainPage.PLAYLISTS)),
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
        startup.mark("media_routing_controllers_ready")
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
                projection_windows=self._raw_projection_windows,
                playlist_edit_is_temp=lambda: getattr(
                    self.playlist_widget, "is_temporary_edit", False
                ),
                meeting_service=lambda: self.meetings_widget.get_service(),
                dialog_parent=self,
                sjjm_announce_mode=self.settings_widget.get_sjjm_announce_mode,
                start_videos_paused=self.settings_widget.get_start_videos_paused,
                playback_protection=self.playback_protection,
                content_frame_sink=self._program_content.submit_frame,
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
                projection_windows=self._raw_projection_windows,
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
                obs_service=self._obs_service,
                ndi_service=self._ndi_service,
                zoom_service=self._zoom_service,
                notifications=self.notifications,
                projection_bar=self.proj_bar,
                media_controller=self.media_ctrl,
                quick_toolbar=lambda: getattr(self, "_quick_toolbar", None),
                projection_windows=self._raw_projection_windows,
                playback_protection=self.playback_protection,
                content_frame_sink=self._program_content.submit_frame,
                camera_settings=self._camera_settings,
                camera_service=self._camera_service,
            ),
            LiveIntegrationHandlers(
                stop_projection=self._projection_stop.stop_projection,
                stop_browser_tab_projection=(self._navigation.stop_browser_tab_projection),
                update_projection_status=(self._projection_integrations.update_status),
            ),
        )
        self._timer_projection_controller = TimerProjectionController(
            TimerProjectionContext(
                projection_session=self.projection_session,
                projection_bar=self.proj_bar,
                media_controller=self.media_ctrl,
                ndi_service=self._ndi_service,
                camera_service=self._camera_service,
                projection_windows=self._raw_projection_windows,
                playback_protection=self.playback_protection,
                program_content=self._program_content,
            ),
            TimerProjectionHandlers(
                stop_browser_tab_projection=(self._navigation.stop_browser_tab_projection),
                update_projection_status=(self._projection_integrations.update_status),
            ),
        )
        self._language_controller = LanguageController(
            LanguageContext(
                set_window_title=self._window_host.setWindowTitle,
                sidebar_title_label=self._sidebar_title_lbl,
                sidebar_subtitle_label=self._sidebar_subtitle_lbl,
                nav_buttons=self._nav_buttons_by_name,
                translate=self.tr,
                sidebar_chrome=self._sidebar_chrome,
            )
        )
        self._signal_connections = SignalConnectionController(
            MainWindowSignalSources(
                library_widget=self.library_widget,
                meetings_widget=self.meetings_widget,
                timer_widget=self.timer_widget,
                talk_theme_widget=self.talk_theme_widget,
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
                timer_projection=self._timer_projection_controller,
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
        startup.mark("projection_controllers_ready")
        self._signal_connections.connect_signals()
        self.settings_widget.yearly_text_changed.connect(
            self._program_content.update_yearly_text
        )
        self.settings_widget.remote_control_settings_changed.connect(
            self._ensure_remote_control_started
        )
        startup.mark("main_window_signals_connected")

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
            )
        )
        startup_resources = self._bootstrap_controller.prepare_before_show()
        self._monitor_popup = startup_resources.monitor_popup
        self._ipc_controller = startup_resources.ipc_controller
        self._shutdown_controller = ShutdownController(
            ShutdownDependencies(
                projection_session=self.projection_session,
                timer_output=self.timer_output,
                services=ShutdownServices(
                    remote_control=lambda: self._remote_control,
                    remote_services=lambda: self._remote_services,
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
                    ipc=lambda: self._ipc_controller,
                    scenes=self.scene_runtime,
                    program_recording=self._program_recording,
                    program_content=self._program_content,
                    content_frame_ingress=self._content_frame_ingress,
                    scene_frame_egresses=(
                        self._scene_preview_egress,
                        self._scene_program_egress,
                    ),
                ),
                widget_providers=(
                    lambda: self.settings_widget,
                    lambda: self.meetings_widget,
                    lambda: self.library_widget,
                    lambda: self._lazy_pages.wifi_receive_widget,
                    lambda: self.playlist_widget,
                    lambda: self.timer_widget,
                    lambda: self.scenes_widget,
                ),
                conversion_threads=self._conversion_threads,
                jwl_temp_files=self._jwl_tmp_files,
                queue_pending_deletion=queue_pending_deletion,
                cleanup_lazy_pages=self._lazy_pages.cleanup_browser,
                cancel_ui_preparation=self._ui_preparation.cancel,
            )
        )
        startup.mark("main_window_constructor_ready")

    def _on_scene_document_changed_for_egress(
        self,
        document: SceneDocument,
    ) -> None:
        preview_output = document.output(BusId.MEDIA_WINDOWS)
        self._scene_preview_egress.reconfigure(
            preview_output.video_format.width,
            preview_output.video_format.height,
        )
        program_output = document.output(BusId.VIRTUAL_CAMERA)
        self._scene_program_egress.reconfigure(
            program_output.video_format.width,
            program_output.video_format.height,
        )
        self._reconcile_native_scene_surfaces()

    def _on_scene_frame_egress_descriptor_changed(self, _descriptor) -> None:
        self._reconcile_scene_media_egress()

    def _on_scene_desired_changed_for_egress(self, _scenes) -> None:
        self._reconcile_native_scene_surfaces()

    def _reconcile_scene_media_egress(self) -> None:
        self.scene_runtime.set_preview_egress(self._scene_preview_egress.descriptor)
        # Under libobs the sidecar paints projection windows directly, so the
        # program egress only feeds the operator Program tab — demand it whenever
        # the program is mirrored. The native engine demands it as a window
        # fallback instead.
        if libobs_scene_engine_selected():
            program_required = self._program_mirror_enabled()
        else:
            program_required = self._native_fallback_mirror_required
        self.scene_runtime.set_program_egress(
            self._scene_program_egress.descriptor if program_required else None
        )

    @staticmethod
    def _native_window_target(
        surface,
        target_id: str,
        bus_id: BusId,
    ) -> OutputWindowTarget:
        screen = surface.screen() or QApplication.primaryScreen()
        origin = surface.mapToGlobal(QPoint(0, 0))
        return OutputWindowTarget(
            bus_id=bus_id,
            target_id=target_id,
            screen_id=(screen.name() or "primary") if screen is not None else "primary",
            native_handle=surface.native_handle,
            x=origin.x(),
            y=origin.y(),
            width=max(1, surface.width()),
            height=max(1, surface.height()),
            device_pixel_ratio=surface.devicePixelRatioF(),
        )

    def _on_projection_state_changed_for_native(self) -> None:
        self._reconcile_native_scene_surfaces()

    def _on_engine_media_event(self, event: object) -> None:
        # Runs on the engine's event thread; on_engine_media_state only emits
        # (queued) Qt signals and sets plain attributes, so this is thread-safe.
        from .core.scenes.engine import MediaPlaybackEvent
        from .core.scenes.media_control import MEDIA_SLOT_BACKGROUND

        if not isinstance(event, MediaPlaybackEvent):
            return
        if getattr(event.state, "slot", 0) == MEDIA_SLOT_BACKGROUND:
            self._background_media_controller.on_engine_media_state(event.state)
        else:
            self.media_ctrl.on_engine_media_state(event.state)

    def _on_native_content_source_health_changed(self, source_id: str) -> None:
        if source_id != CONTENT_SOURCE_ID:
            return
        health = self.scene_runtime.source_health(source_id)
        if health is not None and health.error_code:
            self._content_frame_ingress.recover_accelerated_transport(
                health.error_code
            )

    def _reconcile_native_scene_surfaces(self) -> None:
        mirror_enabled = self._program_mirror_enabled()
        render_bus = (
            BusId.VIRTUAL_CAMERA if mirror_enabled else BusId.MEDIA_WINDOWS
        )
        state = self.projection_session.state
        raw_visual = projection_presentation_type(state) in {
            "idle",
            "video",
            "image",
            "browser",
            "timer",
            "obs_stream",
            "camera_stream",
        }
        # The projection windows render the composited main mix (the program). The
        # program's idle scene is the Default scene (which shows the Year text), so
        # at idle the projection shows the year text with no special-casing here;
        # media presentations transition the program to the Content scene as usual.
        native_window_routing_ready = (
            _native_scene_routing_supported()
            and self.scene_runtime.native_window_routing_ready
            and not self._native_window_output_suppressed
        )
        native_presentation = _use_native_media_presentation(
            native_window_routing_ready=native_window_routing_ready,
            mirror_enabled=mirror_enabled,
            raw_visual=raw_visual,
        )
        physical_windows = tuple(self.projection_session.projection_windows)
        targets: list[OutputWindowTarget] = []
        raw_fallback_windows: list[object] = []

        for index, window in enumerate(physical_windows):
            set_native_active = getattr(window, "set_native_output_active", None)
            if callable(set_native_active):
                was_native = bool(getattr(window, "native_output_active", False))
                set_native_active(native_presentation)
                if was_native and not native_presentation and not mirror_enabled:
                    raw_fallback_windows.append(window)
            if native_presentation:
                surface = window.native_video_surface
                targets.append(
                    self._native_window_target(
                        surface,
                        f"media-window-{index}",
                        render_bus,
                    )
                )
        for window in self.projection_session.all_windows():
            if window in physical_windows:
                continue
            set_native_active = getattr(window, "set_native_output_active", None)
            if callable(set_native_active):
                was_native = bool(getattr(window, "native_output_active", False))
                auxiliary_native = (
                    native_presentation
                    and hasattr(window, "native_video_surface")
                )
                set_native_active(auxiliary_native)
                if was_native and not auxiliary_native and not mirror_enabled:
                    raw_fallback_windows.append(window)
                if auxiliary_native:
                    targets.append(
                        self._native_window_target(
                            window.native_video_surface,
                            "media-floating-preview",
                            render_bus,
                        )
                    )

        projection_bar = getattr(self, "proj_bar", None)
        if projection_bar is not None:
            operator_video_native = (
                native_presentation
                and projection_bar.native_video_output_requested
                and len(targets) < MAXIMUM_OUTPUT_WINDOW_TARGETS
            )
            projection_bar.set_native_video_output_active(
                operator_video_native
            )
            operator_video_surface = (
                projection_bar.native_video_output_surface
            )
            if operator_video_native and operator_video_surface is not None:
                targets.append(
                    self._native_window_target(
                        operator_video_surface,
                        _OPERATOR_VIDEO_OUTPUT_TARGET_ID,
                        BusId.MEDIA_WINDOWS,
                    )
                )

        fallback_windows = (
            tuple(self.projection_session.all_windows())
            if mirror_enabled
            else ()
        )
        self._native_fallback_mirror_required = any(
            not getattr(window, "native_output_active", False)
            for window in fallback_windows
        )
        if targets:
            self.scene_runtime.set_window_targets(tuple(targets))
        else:
            self.scene_runtime.set_window_targets(())
        for window in raw_fallback_windows:
            self._projection_targets.restore_state_to_window(window)
        self._reconcile_scene_media_egress()

    def _on_native_scene_engine_ready_changed(self, ready: bool) -> None:
        if ready:
            self._native_window_output_suppressed = False
        self._reconcile_native_scene_surfaces()

    def _on_native_scene_engine_error(self, _message: str) -> None:
        if self.scene_runtime.last_engine_error_code != "native_window_output_failed":
            return
        self._native_window_output_suppressed = True
        self._reconcile_native_scene_surfaces()

    def _on_scene_transition_fallback(self, message: str) -> None:
        self.notifications.warning(
            message,
            title=self.tr("Scenes"),
            dedupe_key="scene-transition-fallback",
        )

    def _on_program_recording_state_changed(
        self,
        state: ProgramRecordingState,
    ) -> None:
        previous = self._last_program_recording_status
        self._last_program_recording_status = state.status
        if state.status is ProgramRecordingStatus.FAILED:
            if previous is not ProgramRecordingStatus.FAILED:
                preserved_path = None
                if state.output_path is not None:
                    staging_path = state.output_path.with_suffix(
                        state.output_path.suffix + ".part"
                    )
                    if state.output_path.exists():
                        preserved_path = state.output_path
                    elif staging_path.exists():
                        preserved_path = staging_path
                if preserved_path is None:
                    message = self.tr("Recording failed.")
                else:
                    message = self.tr(
                        "Recording failed. The file was preserved at %1"
                    ).replace("%1", str(preserved_path))
                self.notifications.error(
                    message,
                    title=self.tr("Recording"),
                    dedupe_key="program-recording-failed",
                )
            return
        if (
            state.status is ProgramRecordingStatus.IDLE
            and previous
            in {
                ProgramRecordingStatus.STARTING,
                ProgramRecordingStatus.RECORDING,
                ProgramRecordingStatus.STOPPING,
            }
            and state.output_path is not None
        ):
            self.notifications.success(
                self.tr("Recording saved to %1").replace("%1", str(state.output_path)),
                title=self.tr("Recording complete"),
                dedupe_key=f"program-recording-complete:{state.output_path}",
            )

    def _notify_recording_blocks_profile_switch(self) -> None:
        self.notifications.warning(
            self.tr("Stop recording before switching profiles."),
            title=self.tr("Recording in progress"),
            dedupe_key="program-recording-blocks-profile-switch",
        )

    def _on_scene_preview_frame(self, image) -> None:
        self.scene_runtime.publish_preview_frame(image)

    def _on_scene_program_frame(self, frame) -> None:
        if not self._program_mirror_enabled():
            return
        # The libobs egress emits a QImage directly; the native one a VideoFrame.
        image = frame if isinstance(frame, QImage) else video_frame_to_image(frame)
        if image is None or image.isNull():
            return
        # Under libobs the sidecar paints the projection windows itself, so only
        # the operator Program tab is fed here; the native engine also uses these
        # frames as a fallback to paint windows it did not render natively.
        if not libobs_scene_engine_selected():
            for window in self.projection_session.all_windows():
                if getattr(window, "native_output_active", False):
                    continue
                show_image = getattr(window, "show_image_from_qimage", None)
                if callable(show_image):
                    show_image(image, cache_pixmap=False)
        if hasattr(self, "proj_bar"):
            self.proj_bar.update_tab_live_preview(image)

    def _program_mirror_enabled(self) -> bool:
        return self.scene_live.state.output(BusId.MEDIA_WINDOWS).enabled

    @property
    def scene_documents(self):
        return self.scene_workspace.documents

    @property
    def scene_live(self):
        return self.scene_workspace.runtime

    def _program_content_requested(self) -> bool:
        state = self.scene_live.state
        return any(state.output(bus_id).enabled for bus_id in DELIVERY_BUSES)

    def _on_content_ingress_demand_changed(self, required: bool) -> None:
        self._content_frame_ingress.set_enabled(required)
        if not required:
            return
        # Media video is composited by the libobs sidecar; the app only re-primes
        # its own rendered content (text/images) here.
        self._program_content.refresh()

    def _raw_projection_windows(self) -> list:
        if self._program_mirror_enabled():
            return []
        return [
            window
            for window in self.projection_session.all_windows()
            if not getattr(window, "native_output_active", False)
        ]

    def _on_scene_window_route_changed(self, _state) -> None:
        mirror_enabled = self._program_mirror_enabled()
        if mirror_enabled == self._media_mirror_was_enabled:
            return
        self._media_mirror_was_enabled = mirror_enabled
        self._reconcile_native_scene_surfaces()
        if mirror_enabled:
            self._program_content.refresh()
            return
        QTimer.singleShot(0, self._restore_raw_projection_windows)

    def _restore_raw_projection_windows(self) -> None:
        if self._program_mirror_enabled() or getattr(self, "_closing", False):
            return
        for window in self._raw_projection_windows():
            self._projection_targets.apply_full_state_to_window(window)

    def _start_deferred_startup(self) -> None:
        if self._deferred_startup_started or getattr(self, "_closing", False):
            return
        self._deferred_startup_started = True
        resources = self._bootstrap_controller.start_after_first_frame()
        self._remote_services = resources.remote_services
        self._application_maintenance()
        self.settings_widget.start_deferred_services()
        self._ui_preparation.start()
        if self.scene_runtime.engine_configured:
            self.scene_runtime.start_engine()
        self._ensure_remote_control_started()
        self._timer_output_startup_timer.start(_STARTUP_SCREEN_SETTLE_MS)

        from .bootstrap.startup_timeline import startup_timeline

        startup_timeline().mark("deferred_startup_started")

    def complete_startup_handoff(self) -> None:
        """Release deferred services only after the visible window handoff."""

        self._start_deferred_startup()

    def _reconcile_timer_output_after_startup(self) -> None:
        if not getattr(self, "_closing", False):
            self.timer_output.reconcile()

    def _on_ui_preparation_completed(self) -> None:
        if getattr(self, "_closing", False):
            return
        from .bootstrap.startup_timeline import (
            startup_benchmark_exit_requested,
            startup_timeline,
        )

        timeline = startup_timeline()
        timeline.record_measurements(
            "ui_preparation_dispatch",
            self._ui_preparation.dispatch_durations_ms,
        )
        timeline.record_measurements(
            "ui_preparation_units",
            self._ui_preparation.unit_durations_ms,
        )
        for task_index, durations in self._ui_preparation.unit_durations_by_task.items():
            timeline.record_measurements(
                f"ui_preparation_task_{task_index}",
                durations,
            )
        timeline.mark("deferred_startup_complete")
        timeline.emit_json()
        if startup_benchmark_exit_requested():
            # Leave the current cooperative-dispatch event before closing the
            # native window; Windows COM rejects teardown from an input-sync
            # callback with RPC_E_CANTCALLOUT_ININPUTSYNCCALL.
            self._benchmark_close_requested.emit()

    def _ensure_remote_control_started(self) -> None:
        if self._remote_control is not None or not self._remote_control_settings.enabled():
            return
        self._remote_control = self._create_remote_control()
        self._remote_control.start()

    def _create_remote_control(self):
        from .controllers.remote_control_controller import (
            RemoteControlController,
            RemoteControlDependencies,
        )
        from .controllers.remote_media_thumbnail_extractor import (
            RemoteMediaThumbnailExtractor,
        )

        extractor = RemoteMediaThumbnailExtractor(
            self._media_info_queue_factory,
            self,
        )
        self._remote_media_thumbnail_extractor = extractor
        controller = RemoteControlController(
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
                media_thumbnail_extractor=extractor,
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
        controller.runtime_status_reported.connect(self._quick_toolbar.set_remote_control_status)
        controller.session_inventory_reported.connect(self._quick_toolbar.set_remote_sessions)
        controller.session_revocation_reported.connect(
            self._quick_toolbar.set_remote_session_revocation_result
        )
        self._quick_toolbar.remote_session_disconnect_requested.connect(controller.revoke_session)
        self._quick_toolbar.remote_sessions_disconnect_all_requested.connect(
            controller.revoke_sessions
        )
        self.lang.language_changed.connect(controller.on_language_changed)
        return controller

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
        self._navigation.switch_page(int(MainPage.SETTINGS))
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

    def _set_content_widget(self, widget: QWidget) -> None:
        if self._content_layout.count():
            raise RuntimeError("Main window content can only be installed once.")
        self._content_layout.addWidget(widget)

    def _install_ui_resources(self, resources: MainWindowUiResources) -> None:
        self.stack = resources.stack
        self._lazy_pages = resources.lazy_pages
        self._navigation = resources.navigation
        self._ui_preparation = resources.preparation
        self._ui_preparation.completed.connect(self._on_ui_preparation_completed)
        self.right_col = resources.right_col
        self.proj_bar = resources.projection_bar
        self.proj_bar.video_output_target_changed.connect(
            self._reconcile_native_scene_surfaces
        )
        self.scene_runtime.engine_ready_changed.connect(
            self._on_native_scene_engine_ready_changed
        )
        self.scene_runtime.operational_state_changed.connect(
            self._reconcile_native_scene_surfaces
        )
        self.scene_runtime.engine_error.connect(self._on_native_scene_engine_error)
        self.scene_runtime.desired_scenes_changed.connect(
            self._on_scene_desired_changed_for_egress
        )
        self._reconcile_native_scene_surfaces()
        self.library_widget = resources.library_widget
        self.settings_widget = resources.settings_widget
        self.timer_widget = resources.timer_widget
        self.talk_theme_widget = resources.talk_theme_widget
        self.playlist_widget = resources.playlist_widget
        self.meetings_widget = resources.meetings_widget
        self.scenes_widget = resources.scenes_widget
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
        self._window_host.setStyleSheet(stylesheet)

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
            getattr(self, "library_widget", None),
            getattr(self, "settings_widget", None),
            getattr(self, "timer_widget", None),
            getattr(self, "talk_theme_widget", None),
            getattr(self, "playlist_widget", None),
            getattr(self, "meetings_widget", None),
            getattr(self, "scenes_widget", None),
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
        self._navigation.switch_page(int(MainPage.PLAYLISTS))
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

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._first_frame_presented:
            return
        self._first_frame_presented = True

        from .bootstrap.startup_timeline import startup_timeline

        startup_timeline().mark("first_paint")
        self.first_frame_presented.emit()

    def shutdown(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._closing = True

        if self._shutdown_controller is not None:
            self._shutdown_controller.shutdown()

    def commit_construction(self) -> None:
        if self._construction_finalized:
            return
        self._construction_cleanup.pop_all()
        self._construction_finalized = True

    def abort_construction(self) -> None:
        if self._construction_finalized:
            return
        self._construction_cleanup.close()
        self._construction_finalized = True

    def _abort_partial_resources(self) -> None:
        shutdown_controller = getattr(self, "_shutdown_controller", None)
        if shutdown_controller is not None:
            self._partial_cleanup_call(
                "completed shutdown controller",
                shutdown_controller.shutdown,
            )
            return

        ui_preparation = getattr(self, "_ui_preparation", None)
        self._partial_cleanup_method("UI preparation", ui_preparation, "cancel")
        self._partial_cleanup_method(
            "remote control",
            getattr(self, "_remote_control", None),
            "stop",
        )
        self._partial_cleanup_method(
            "remote services",
            getattr(self, "_remote_services", None),
            "stop",
        )
        self._partial_cleanup_method(
            "media countdown automation",
            getattr(self, "_media_countdown_automation", None),
            "shutdown",
        )

        projection_session = getattr(self, "projection_session", None)
        if projection_session is not None:
            projection_windows = getattr(projection_session, "projection_windows", ())
            for projection_window in tuple(projection_windows):
                self._partial_cleanup_method(
                    "projection window",
                    projection_window,
                    "close",
                )
            self._partial_cleanup_method(
                "projection window registry",
                projection_windows,
                "clear",
            )
            self._partial_cleanup_method(
                "floating projection preview",
                projection_session,
                "close_floating_preview",
            )
        self._partial_cleanup_method(
            "timer output",
            getattr(self, "timer_output", None),
            "close_all",
        )

        for attribute, label in (
            ("_media_download_notifications", "download notifications"),
            ("_media_playback_notifications", "playback notifications"),
        ):
            self._partial_cleanup_method(
                label,
                getattr(self, attribute, None),
                "stop",
            )
        self._partial_cleanup_method(
            "notification center",
            getattr(self, "notifications", None),
            "shutdown",
        )

        for attribute in (
            "meetings_widget",
            "playlist_widget",
            "timer_widget",
        ):
            self._partial_cleanup_method(
                attribute,
                getattr(self, attribute, None),
                "cleanup",
            )
        lazy_pages = getattr(self, "_lazy_pages", None)
        self._partial_cleanup_method("lazy browser", lazy_pages, "cleanup_browser")

        self._partial_cleanup_method(
            "media tree runtime",
            getattr(self, "media_tree_runtime", None),
            "shutdown",
        )
        self._partial_cleanup_method(
            "projection integrations",
            getattr(self, "_projection_integrations", None),
            "cleanup",
        )
        self._partial_cleanup_method(
            "program content",
            getattr(self, "_program_content", None),
            "close",
        )
        self._partial_cleanup_method(
            "content frame ingress",
            getattr(self, "_content_frame_ingress", None),
            "close",
        )
        self._partial_cleanup_method(
            "scene preview egress",
            getattr(self, "_scene_preview_egress", None),
            "close",
        )
        self._partial_cleanup_method(
            "scene program egress",
            getattr(self, "_scene_program_egress", None),
            "close",
        )
        self._partial_cleanup_method(
            "Program recording",
            getattr(self, "_program_recording", None),
            "close",
        )
        self._partial_cleanup_method(
            "scene runtime",
            getattr(self, "scene_runtime", None),
            "close",
        )
        self._partial_cleanup_method(
            "background song",
            getattr(self, "_background_song_service", None),
            "shutdown",
        )
        self._partial_cleanup_method(
            "foreground media controller",
            getattr(self, "media_ctrl", None),
            "stop",
        )
        self._partial_cleanup_method(
            "background media controller",
            getattr(self, "_background_media_controller", None),
            "stop",
        )
        self._partial_cleanup_method(
            "NDI receiver",
            getattr(self, "_ndi_service", None),
            "stop",
            wait=True,
        )
        self._partial_cleanup_method(
            "OBS service",
            getattr(self, "_obs_service", None),
            "stop",
            wait=True,
        )
        self._partial_cleanup_method(
            "Zoom service",
            getattr(self, "_zoom_service", None),
            "stop",
            wait=True,
        )
        conversion_threads = getattr(self, "_conversion_threads", None)
        if conversion_threads is not None:
            self._partial_cleanup_call(
                "conversion threads",
                conversion_threads.stop_all,
                logger=log,
            )
        self._partial_cleanup_method(
            "IPC controller",
            getattr(self, "_ipc_controller", None),
            "close",
        )

    @staticmethod
    def _partial_cleanup_method(
        label: str,
        owner: Any,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if owner is None:
            return
        method = getattr(owner, method_name, None)
        if callable(method):
            MainWindow._partial_cleanup_call(label, method, *args, **kwargs)

    @staticmethod
    def _partial_cleanup_call(
        label: str,
        callback: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        try:
            callback(*args, **kwargs)
        except Exception:  # noqa: BLE001 - fail-safe partial startup teardown
            log.warning(
                "Could not clean up %s after startup failure",
                label,
                exc_info=True,
            )

    def closeEvent(self, event):
        if not self.confirm_close():
            event.ignore()
            return
        self.shutdown()
        super().closeEvent(event)

    def confirm_close(self) -> bool:
        if self._program_recording.busy:
            self.notifications.warning(
                self.tr("Stop recording before closing Solin."),
                dedupe_key="program-recording-blocks-close",
            )
            return False
        widget = getattr(self, "talk_theme_widget", None)
        callback = getattr(widget, "confirm_close", None)
        return bool(callback()) if callable(callback) else True
