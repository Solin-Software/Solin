import os
import logging
import sys

_QT_LOGGING_RULES = (
    "qt.qpa.mime=false",
    "qt.multimedia.ffmpeg=false",
)


def _configure_qt_logging_rules() -> None:
    existing = os.environ.get("QT_LOGGING_RULES", "")
    entries = [entry.strip() for entry in existing.split(";") if entry.strip()]
    configured = set(entries)
    entries.extend(rule for rule in _QT_LOGGING_RULES if rule not in configured)
    os.environ["QT_LOGGING_RULES"] = ";".join(entries)


_configure_qt_logging_rules()


def _configure_qt_gl_integration() -> None:
    """Prepare Qt to coexist with libobs' OpenGL when the libobs engine is on.

    Two settings, Linux/X11 only, applied before QApplication is constructed:

    * ``QT_XCB_GL_INTEGRATION=xcb_egl`` — libobs' OpenGL backend uses EGL. If Qt
      uses GLX (the xcb default), the two can't share a context and the libobs
      projection display fails with EGL_BAD_ACCESS. Selecting ``xcb_egl`` makes
      Qt use EGL so — with ``obs_set_nix_platform_display`` sharing Qt's X
      connection — both live on a single EGLDisplay.
    * ``QT_QUICK_BACKEND=software`` — but with ``xcb_egl`` active, Qt Quick's
      OpenGL RHI cannot make a context current on some Intel GPUs (``QRhiGles2:
      Failed to make context current`` / ``eglMakeCurrent failed: 3009``), so the
      QML operator UI never leaves the splash. Rendering Qt Quick in *software*
      sidesteps that entirely and leaves the GPU to libobs (fine for the
      operator UI — it is not GPU-bound).
    """
    if os.environ.get("SOLIN_MEDIA_ENGINE", "").strip().lower() != "obs":
        return
    if sys.platform.startswith("linux"):
        if not os.environ.get("QT_XCB_GL_INTEGRATION"):
            os.environ["QT_XCB_GL_INTEGRATION"] = "xcb_egl"
        os.environ.setdefault("QT_QUICK_BACKEND", "software")


_configure_qt_gl_integration()

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QCoreApplication, QTimer


def _install_qt_message_filter() -> None:
    """Drop the high-volume, benign ``QQuickWidget cannot be used as a native
    child widget`` warning.

    It fires because the app's native host windows (the single native top-level,
    the native cursor hosts) sit above QML content — the message is harmless, but
    it is emitted in a tight loop, and writing that flood to an *interactive
    terminal* back-pressures the GUI thread enough to wedge startup on the splash
    (redirected output is unaffected — which is why it "only hangs in a
    terminal"). Every other Qt message passes straight through to stderr.
    """
    from PySide6.QtCore import qInstallMessageHandler

    def _handler(_mode, _context, message: str) -> None:
        if "cannot be used as a native child widget" in message:
            return
        if sys.stderr is not None:  # packaged/windowed apps may have no stderr
            sys.stderr.write(message + "\n")

    qInstallMessageHandler(_handler)


_install_qt_message_filter()

# Apenas constantes puras — sem dependência de caminhos ou QApplication.
from solin.bootstrap.config import default_app_config
from solin.bootstrap.container import initialize_application_container
from solin.bootstrap.file_open import (
    ApplicationFileOpenRouter,
)
log = logging.getLogger(__name__)
from solin.core.foundation.resources import application_asset_path
from solin.bootstrap.profile_flow import wire_profile_switch
from solin.bootstrap.runtime_args import parse_runtime_args
from solin.bootstrap.single_instance import (
    SingleInstanceServer,
    try_forward_to_running,
)
from solin.core.foundation.constants import IPC_SERVER_NAME
from solin.core.profiles.application import ProfileRegistryLoadError
from solin.bootstrap.startup_timeline import startup_timeline


def _build_main_window_profile_settings(profile_settings):
    from solin.controllers.main_window_profile_settings import MainWindowProfileSettings
    from solin.core.ingest.watched_folder_settings import WatchedFolderSettingsStore
    from solin.core.integrations.automation.settings import (
        AutoKeySettingsStore,
        AutoShareSettingsStore,
        CameraSettingsStore,
        OBSSettingsStore,
        ZoomSettingsStore,
    )
    from solin.core.jw.background_song_settings import BackgroundSongSettingsStore
    from solin.core.jw.yeartext_settings import YeartextSettingsStore
    from solin.core.media.settings import (
        MediaSettingsStore,
        ProjectionPlaybackSettingsStore,
    )
    from solin.core.meetings.schedule_settings import MeetingScheduleSettingsStore
    from solin.core.timer.media_countdown_settings import (
        MediaCountdownSettingsStore,
    )
    from solin.core.network.browser_settings import BrowserSettingsStore
    from solin.core.projection.monitor_allocation import MonitorAllocationStore
    from solin.core.remote.notification_settings import NotificationSettingsStore
    from solin.core.remote_control.security import RemoteControlCredentialsStore
    from solin.core.remote_control.settings import RemoteControlSettingsStore
    from solin.core.talk_theme.settings import TalkThemeSettingsStore
    from solin.core.windowing.settings import WindowGeometrySettingsStore

    return MainWindowProfileSettings(
        app=profile_settings.app_settings(),
        media=MediaSettingsStore.for_profile_settings(profile_settings),
        browser=BrowserSettingsStore.for_profile_settings(profile_settings),
        obs=OBSSettingsStore.for_profile_settings(profile_settings),
        zoom=ZoomSettingsStore.for_profile_settings(profile_settings),
        auto_share=AutoShareSettingsStore.for_profile_settings(profile_settings),
        auto_key=AutoKeySettingsStore.for_profile_settings(profile_settings),
        camera=CameraSettingsStore.for_profile_settings(profile_settings),
        projection_playback=ProjectionPlaybackSettingsStore.for_profile_settings(profile_settings),
        meeting_schedule=MeetingScheduleSettingsStore.for_profile_settings(profile_settings),
        media_countdown=(
            MediaCountdownSettingsStore.for_profile_settings(profile_settings)
        ),
        watched_folder=WatchedFolderSettingsStore.for_profile_settings(profile_settings),
        yeartext=YeartextSettingsStore.for_profile_settings(profile_settings),
        background_song=BackgroundSongSettingsStore.for_profile_settings(profile_settings),
        monitor_allocation=MonitorAllocationStore.for_profile_settings(profile_settings),
        window_geometry=WindowGeometrySettingsStore.for_profile_settings(profile_settings),
        notification=NotificationSettingsStore.for_profile_settings(profile_settings),
        talk_theme=TalkThemeSettingsStore.for_profile_settings(profile_settings),
        remote_control=RemoteControlSettingsStore.create(profile_settings.app_settings()),
        remote_control_credentials=RemoteControlCredentialsStore.create(
            profile_settings.app_settings()
        ),
    )


def _meeting_weekday_resolver(schedule_settings):
    from solin.core.meetings.schedule import UNCONFIGURED_WEEKDAY

    def weekday_for_pub_type(pub_type: str) -> int:
        schedule = schedule_settings.load()
        if pub_type == "mwb":
            weekday = schedule.midweek.weekday
        elif pub_type == "wt":
            weekday = schedule.weekend.weekday
        else:
            return UNCONFIGURED_WEEKDAY
        if 0 <= weekday <= 6:
            return weekday
        return UNCONFIGURED_WEEKDAY

    return weekday_for_pub_type


def _import_main_window_class():
    timeline = startup_timeline()
    timeline.mark("main_window_import_started")
    from solin.main_window import MainWindow

    timeline.mark("main_window_imported")
    return MainWindow


def _prepare_profile_main_window(profile_paths, cancellation=None):
    """Prepare code and fail-closed housekeeping before profile UI is mutable."""

    main_window_class = _import_main_window_class()
    from solin.core.playlists.cleanup import (
        ProfileMaintenanceCancelled,
        ProfileMaintenanceService,
    )

    try:
        from solin.core.foundation.resource_lanes import ResourceLaneRegistry
        from solin.core.meetings.meeting_weeks import meeting_week_bounds
        from solin.core.meetings.tree_store import MeetingTreeStore
        from solin.core.playlists.storage import PlaylistRepository, PlaylistStoragePaths

        storage_paths = PlaylistStoragePaths(
            playlists_file=profile_paths.playlists_file,
            pending_deletions_file=profile_paths.pending_deletions_file,
        )
        lanes = ResourceLaneRegistry()
        meeting_tree_store = MeetingTreeStore(
            profile_paths.meeting_trees_file,
            resource_lanes=lanes,
        )
        maintenance = ProfileMaintenanceService(
            storage_paths=storage_paths,
            playlist_repository=PlaylistRepository.from_paths(
                storage_paths,
                resource_lanes=lanes,
            ),
            meeting_tree_store=meeting_tree_store,
            profile_paths=profile_paths,
            resource_lanes=lanes,
        )
        lanes.run(
            maintenance.resource_claim,
            lambda: maintenance.run(cancellation),
        )

        def prune_expired_trees() -> None:
            meeting_tree_store.prune_before(meeting_week_bounds()[0])

        if cancellation is None:
            prune_expired_trees()
        else:
            cancellation.run_if_active(prune_expired_trees)
    except ProfileMaintenanceCancelled:
        pass
    except Exception:  # noqa: BLE001 - fail-closed maintenance boundary
        from solin.core.foundation.exception_logging import log_ignored_exception

        log_ignored_exception(__name__, "Pre-UI profile maintenance failed")
    return main_window_class


def _build_main_window_service_factories(
    lang_manager,
    runtime_paths,
    profile_settings,
    jwpub_checksum_store,
    installation_settings,
    application_maintenance,
):
    startup_timeline().mark("service_factories_started")
    from solin.controllers.main_window_service_factories import (
        MainWindowServiceFactories,
    )
    from solin.core.foundation.identity import get_install_id
    from solin.core.foundation.thread_workers import ThreadedWorkerPool
    from solin.core.integrations.automation.obs import OBSWebSocketService
    from solin.core.integrations.automation.shortcuts import AutoKeyDispatcher
    from solin.core.integrations.automation.zoom.service import ZoomService
    from solin.core.integrations.camera import CameraService
    from solin.core.integrations.ndi import NDIReceiverService
    from solin.core.jw.background_song_service import BackgroundSongService
    from solin.core.jw.yeartext import YeartextService
    from solin.core.meetings.memorial import MemorialService
    from solin.core.meetings.publications import JwpubService
    install_id_provider = lambda: get_install_id(installation_settings)

    def create_remote_services(parent):
        from typing import cast

        from PySide6.QtWidgets import QWidget
        from solin.controllers.remote_services_controller import (
            RemoteNotificationQueue as RemoteNotificationQueuePort,
            RemoteNotificationService,
            RemoteServicesController,
            RemoteUpdateService,
        )
        from solin.core.remote.notifications import NotificationService
        from solin.core.remote.patch_installer import (
            PatchDownloadWorker,
            launch_patch_installer,
            save_pending_patch_cleanup,
        )
        from solin.core.remote.update_policy import UpdateInfo
        from solin.core.remote.updates import UpdateService
        from solin.ui.dialogs.notifications import RemoteNotificationQueue
        from solin.ui.dialogs.update import UpdateDialog

        return RemoteServicesController(
            cast(QWidget, parent),
            notification_service=cast(
                RemoteNotificationService,
                NotificationService(
                    lang_manager,
                    profile_settings.notification,
                    install_id_provider,
                    parent,
                ),
            ),
            notification_queue=cast(
                RemoteNotificationQueuePort,
                RemoteNotificationQueue(lang_manager, parent),
            ),
            update_service=cast(
                RemoteUpdateService,
                UpdateService(
                    install_id_provider,
                    lambda: lang_manager.api_code,
                    parent,
                ),
            ),
            update_dialog_factory=lambda info: UpdateDialog(
                cast(UpdateInfo, info),
                parent,
                patch_downloader_factory=PatchDownloadWorker,
                save_cleanup_path=lambda path: save_pending_patch_cleanup(
                    installation_settings,
                    path,
                ),
                launch_patch=launch_patch_installer,
            ),
        )

    factories = MainWindowServiceFactories(
        auto_key_dispatcher=AutoKeyDispatcher,
        obs_websocket=OBSWebSocketService,
        ndi_receiver=NDIReceiverService,
        camera=CameraService,
        zoom=ZoomService,
        background_song=BackgroundSongService,
        yeartext=lambda parent: YeartextService(
            cache_file=runtime_paths.cache_dir / "yeartext_cache.json",
            parent=parent,
        ),
        jwpub=lambda parent: JwpubService(
            runtime_paths.jwpub_cache_dir,
            jwpub_checksum_store,
            parent,
        ),
        memorial=lambda parent: MemorialService(
            runtime_paths.jwpub_cache_dir,
            jwpub_checksum_store,
            parent,
        ),
        remote_services=create_remote_services,
        auto_share_workers=ThreadedWorkerPool,
        application_maintenance=application_maintenance,
    )
    startup_timeline().mark("service_factories_ready")
    return factories


def _build_main_window_runtime(
    lang_manager,
    runtime_paths,
    profile_paths,
    profile_settings,
    media,
    font_manager,
    jw_catalog_cache_paths,
    jw_songs_store,
    jwpub_checksum_store,
    installation_settings,
    application_maintenance,
    timer_session,
    active_profile,
    *,
    talk_theme_output_settings,
    window_host,
    main_window_class=None,
):
    """Build application content for an already visible native window host."""
    timeline = startup_timeline()
    MainWindow = main_window_class or _import_main_window_class()
    from solin.core.meetings.tree_store import MeetingTreeStore
    from solin.core.meetings.linked_folder_sync import MeetingLinkedFolderSync
    from solin.core.media.profile_store import ProfileMediaStore
    from solin.core.media.thumbnail_store import ThumbnailStore
    from solin.core.media.cache_scan import CacheScanSessionFactory
    from solin.core.jw.clip_fetch import ClipFetchThreadFactory
    from solin.core.jw.catalog_service import JWMediaCatalogService
    from solin.core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from solin.core.jw.thumbnail_fetch import JWCatalogThumbnailSessionFactory
    from solin.core.rendering.document_conversion import DocumentConversionService
    from solin.core.ingest.watched_folder import WatchedFolderWatcher
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
    from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
    from solin.ui.qr_generation import QrGenerationSessionFactory
    from solin.core.ingest.wifi_server import WifiReceiveServer
    from solin.core.playlists.cleanup import PlaylistCleanupQueue
    from solin.core.playlists.storage import (
        PendingDeletionRepository,
        PlaylistRepository,
        PlaylistStoragePaths,
    )
    from solin.core.foundation.resource_lanes import ResourceLaneRegistry

    playlist_storage_paths = PlaylistStoragePaths(
        playlists_file=profile_paths.playlists_file,
        pending_deletions_file=profile_paths.pending_deletions_file,
    )
    resource_lanes = ResourceLaneRegistry()
    playlist_repository = PlaylistRepository.from_paths(
        playlist_storage_paths,
        resource_lanes=resource_lanes,
    )
    pending_deletion_repository = PendingDeletionRepository.from_paths(
        playlist_storage_paths,
        resource_lanes=resource_lanes,
    )

    def queue_pending_deletion(path: str) -> None:
        try:
            pending = pending_deletion_repository.load_strict()
        except (OSError, UnicodeError, TypeError, ValueError):
            from solin.core.foundation.exception_logging import log_ignored_exception

            log_ignored_exception(__name__, "Could not load pending deletions queue")
            return
        if path not in pending:
            pending.append(path)
            pending_deletion_repository.save(pending)

    meeting_tree_store = MeetingTreeStore(
        profile_paths.meeting_trees_file,
        resource_lanes=resource_lanes,
    )
    profile_media_store = ProfileMediaStore(
        profile_paths.embedded_dir,
        profile_paths.images_dir,
    )
    jwpub_import_thread_factory = JwpubImportThreadFactory(
        profile_paths.images_dir,
    )
    document_conversion_service = DocumentConversionService(
        pdf_pages_dir=profile_paths.pdf_pages_dir,
        pptx_pages_dir=profile_paths.pptx_pages_dir,
        docx_pages_dir=profile_paths.docx_pages_dir,
    )

    def jw_catalog_service_factory(parent):
        return JWMediaCatalogService(jw_catalog_cache_paths, parent)

    jw_catalog_thumbnail_session_factory = JWCatalogThumbnailSessionFactory(
        jw_catalog_cache_paths,
    )
    clip_fetch_thread_factory = ClipFetchThreadFactory()
    cache_scan_session_factory = CacheScanSessionFactory()
    qr_generation_session_factory = QrGenerationSessionFactory()
    playlist_thumbnail_store = ThumbnailStore(profile_paths.thumb_cache_dir)
    meeting_thumbnail_store = ThumbnailStore(
        profile_paths.meeting_thumb_cache_dir,
    )
    watched_folder_file_store = WatchedFolderFileStore()
    watched_folder_playlist_store = WatchedFolderPlaylistStore()
    main_window_profile_settings = _build_main_window_profile_settings(profile_settings)
    meeting_linked_folder_sync = MeetingLinkedFolderSync(
        _meeting_weekday_resolver(main_window_profile_settings.meeting_schedule)
    )
    main_window_service_factories = _build_main_window_service_factories(
        lang_manager,
        runtime_paths,
        main_window_profile_settings,
        jwpub_checksum_store,
        installation_settings,
        application_maintenance,
    )
    media_controller = media.create_playback(main_window_profile_settings.media)
    background_media_controller = media.create_playback(
        main_window_profile_settings.media, projection=False
    )
    timeline.mark("critical_ui_started")
    try:
        runtime = MainWindow(
            lang_manager,
            runtime_paths,
            profile_paths,
            main_window_profile_settings,
            main_window_service_factories,
            media.cache_manager,
            media_controller,
            background_media_controller,
            media.create_info_queue,
            media.create_info_service,
            media.create_browser_download_service,
            media.create_browser_image_fetch_service,
            font_manager,
            jw_catalog_service_factory,
            jw_catalog_thumbnail_session_factory,
            jw_songs_store,
            jwpub_checksum_store,
            playlist_storage_paths,
            playlist_repository,
            queue_pending_deletion,
            meeting_tree_store,
            resource_lanes,
            meeting_linked_folder_sync,
            profile_media_store,
            jwpub_import_thread_factory,
            document_conversion_service,
            clip_fetch_thread_factory,
            cache_scan_session_factory,
            qr_generation_session_factory,
            playlist_thumbnail_store,
            meeting_thumbnail_store,
            watched_folder_file_store,
            watched_folder_playlist_store,
            lambda parent: WifiReceiveServer(
                embedded_dir=profile_paths.embedded_dir,
                parent=parent,
            ),
            WatchedFolderWatcher,
            PlaylistCleanupQueue,
            timer_session,
            active_profile,
            talk_theme_output_settings=talk_theme_output_settings,
            window_host=window_host,
        )
    except Exception:  # noqa: BLE001 - transactional startup rollback boundary
        window_host.abort_runtime_construction()
        for controller in (media_controller, background_media_controller):
            stop = getattr(controller, "stop", None)
            if not callable(stop):
                continue
            try:
                stop()
            except Exception:  # noqa: BLE001 - startup rollback must preserve root error
                log.warning(
                    "Could not stop media controller after startup failure",
                    exc_info=True,
                )
        raise
    media_controller.setParent(runtime)
    background_media_controller.setParent(runtime)
    timeline.mark("critical_ui_ready")
    return runtime


def _launch_zoom_poll_window(filepath: str, lang_manager):
    from solin.widgets.zoom_poll_widget import launch_zoom_poll

    return launch_zoom_poll(filepath, lang_manager)


def _run_zoom_poll_standalone(app, filepath: str, lang_manager) -> int:
    app._solin_zoom_poll_window = _launch_zoom_poll_window(filepath, lang_manager)
    return app.exec()


def _create_profile_screen(container, lang_manager):
    from solin.controllers.onboarding_obs_probe import OnboardingOBSProbe
    from solin.core.integrations.automation.obs import OBSWebSocketService
    from solin.ui.profile_screen import ProfileScreen
    from solin.widgets.screen_picker_overlay import ScreenPickerOverlay

    obs_probe = OnboardingOBSProbe(
        lambda settings, parent: OBSWebSocketService(settings, parent=parent)
    )
    container.lifecycle.register_cleanup(obs_probe.shutdown)
    return ProfileScreen(
        lang_manager,
        profile_service=container.profile_service,
        profile_settings_for=lambda profile_id: (
            container.profile_runtime.create(profile_id).settings
        ),
        onboarding_service=container.onboarding_service,
        obs_probe=obs_probe,
        target_picker_factory=ScreenPickerOverlay,
    )


def _launch_profile_window(
    container,
    lang_manager,
    file_args,
    profile_id: str,
):
    from solin.styles.theme import (
        activate_theme,
        app_stylesheet,
        apply_application_palette,
    )

    profile_context = container.profile_runtime.create(profile_id)
    active_profile = container.profile_service.get_profile(profile_id)
    if active_profile is None:
        raise RuntimeError(f"Profile disappeared during startup: {profile_id}")

    lang_manager.activate_profile(profile_context.settings)
    theme = activate_theme(profile_context.settings.app_settings().app_theme_id())
    apply_application_palette(container.app, theme)
    container.app.setStyleSheet(app_stylesheet(theme))
    startup_timeline().mark("profile_ready")

    from solin.core.windowing.settings import WindowGeometrySettingsStore
    from solin.bootstrap.application_window import ApplicationWindow

    geometry_settings = WindowGeometrySettingsStore.for_profile_settings(
        profile_context.settings
    )
    width, height = geometry_settings.initial_size(1200, 760)
    window = ApplicationWindow(
        width=width,
        height=height,
        geometry=geometry_settings.geometry(),
        save_geometry=geometry_settings.save_geometry,
        pending_files=file_args,
    )
    container.window_ref[0] = window

    def build_main_window(main_window_class=None):
        from solin.core.meetings.meeting_weeks import current_monday
        from solin.core.timer.application import TimerSession
        from solin.core.timer.infrastructure import QSettingsTimerRepository

        timer_session = TimerSession(
            QSettingsTimerRepository.for_profile_settings(profile_context.settings),
            current_week_monday=current_monday(),
        )
        runtime = _build_main_window_runtime(
            lang_manager,
            container.runtime_paths,
            profile_context.paths,
            profile_context.settings,
            container.media,
            container.font_manager,
            container.jw_catalog_cache_paths,
            container.jw_songs_store,
            container.jwpub_checksum_store,
            container.installation_settings,
            container.deferred_maintenance.start,
            timer_session,
            active_profile,
            talk_theme_output_settings=container.talk_theme_output_settings,
            window_host=window,
            main_window_class=main_window_class,
        )
        window.install_runtime(runtime)
        wire_profile_switch(
            container.app,
            container.window_ref,
            container.profile_service,
        )
        return runtime

    def install_main_window(main_window_class) -> None:
        if not window.begin_hydration():
            return
        build_main_window(main_window_class)

    def finish_startup_handoff() -> None:
        window.complete_startup_handoff()

    window.application_frame_presented.connect(
        finish_startup_handoff,
        Qt.ConnectionType.QueuedConnection,
    )

    def start_runtime_preparation() -> None:
        from solin.bootstrap.application_window import ApplicationWindowState
        from solin.core.foundation.thread_workers import CancellationFlag
        from solin.ui.async_load import AsyncLoadHandle

        if window.state is not ApplicationWindowState.LOADING:
            return
        startup_cancellation = CancellationFlag()
        load_handle = AsyncLoadHandle(
            lambda: _prepare_profile_main_window(
                profile_context.paths,
                startup_cancellation,
            ),
            install_main_window,
            window,
            thread_name_prefix="solin-startup-import",
            cancel_load=startup_cancellation.set,
        )
        window.set_load_handle(load_handle)

        def show_startup_error(message: str) -> None:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(window, "Solin", message)
            window.set_load_handle(None)
            window.close()

        load_handle.failed.connect(show_startup_error)
        load_handle.start()

    window.loading_frame_presented.connect(
        start_runtime_preparation,
        Qt.ConnectionType.QueuedConnection,
    )
    window.show()
    startup_timeline().mark("show_returned")
    return window


def main():
    # High-DPI support
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    from PySide6.QtGui import QSurfaceFormat

    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)

    config = default_app_config()
    app = QApplication(sys.argv)
    startup_timeline().mark("qt_application_created")
    config.apply_to(app)
    try:
        container = initialize_application_container(app, config)
    except ProfileRegistryLoadError as exc:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(
            None,
            "Solin",
            "Solin could not read the profile registry. The existing file was "
            "preserved and no profile data was changed.\n\n"
            f"{exc}",
        )
        return
    startup_timeline().mark("container_ready")
    profile_service = container.profile_service

    # ── Imports dependentes de caminhos ───────────────────────────────────────
    from solin.core.i18n.manager import LanguageManager

    # ── Ícone da aplicação ────────────────────────────────────────────────────
    icon_path = application_asset_path("icon.ico")
    if icon_path.is_file():
        from PySide6.QtGui import QIcon

        app.setWindowIcon(QIcon(str(icon_path)))

    # ── Argumentos da linha de comando ────────────────────────────────────────
    runtime_args = parse_runtime_args(sys.argv)
    requested_profile_id = runtime_args.requested_profile_id
    create_profile_mode = runtime_args.create_profile
    file_args = list(runtime_args.media_files)
    _main_window_ref = container.window_ref
    file_open_router = ApplicationFileOpenRouter(app, _main_window_ref, file_args)
    container.lifecycle.register_cleanup(file_open_router.close)

    # ── LanguageManager ───────────────────────────────────────────────────────
    lang_manager = LanguageManager(
        global_settings=container.global_settings,
        jw_languages_cache_file=container.runtime_paths.cache_dir / "jw_languages.json",
        jw_language_settings_store_factory=(container.jw_language_settings_store_factory),
    )
    container.lifecycle.register_cleanup(lang_manager.shutdown)

    # ── CSV do Zoom: janela standalone ────────────────────────────────────────
    csv_args = [
        a for a in file_args if os.path.isfile(a) and os.path.splitext(a)[1].lower() == ".csv"
    ]
    if csv_args:
        sys.exit(_run_zoom_poll_standalone(app, csv_args[0], lang_manager))

    # ── Single-instance ───────────────────────────────────────────────────────
    ipc_server_name = (
        os.environ.get("SOLIN_IPC_SERVER_NAME", "").strip() or IPC_SERVER_NAME
    )
    if try_forward_to_running(file_args, server_name=ipc_server_name):
        sys.exit(0)
    single_instance = SingleInstanceServer(
        app,
        _main_window_ref,
        file_args,
        server_name=ipc_server_name,
    )
    single_instance_started = single_instance.start()
    if not single_instance_started and try_forward_to_running(
        file_args,
        server_name=ipc_server_name,
    ):
        sys.exit(0)
    if single_instance_started:
        container.lifecycle.register_single_instance(single_instance)

    # ── Decisão: perfil direto ou tela de seleção ─────────────────────────────
    #
    #   • 0 perfis + sem legado  → Onboarding completo
    #   • 0 perfis + com legado  → Tela de migração
    #   • 1 perfil               → Abre direto (sem seletor)
    #   • 2+ perfis              → Seletor Netflix

    if create_profile_mode:
        screen = _create_profile_screen(container, lang_manager)
        _main_window_ref[0] = screen
        screen.setWindowTitle("Solin")
        screen.setMinimumSize(860, 580)
        screen.resize(960, 640)

        from PySide6.QtGui import QGuiApplication

        geo = QGuiApplication.primaryScreen().availableGeometry()
        screen.move(
            geo.center().x() - screen.width() // 2,
            geo.center().y() - screen.height() // 2,
        )
        screen.show()

        def _on_profile_ready(profile_id: str):
            screen.hide()
            _launch_profile_window(
                container,
                lang_manager,
                file_args,
                profile_id,
            )
            QTimer.singleShot(400, screen.close)

        screen.profile_ready.connect(_on_profile_ready)
        _pm_screen = screen
        screen.start_new_profile()

    elif requested_profile_id and profile_service.get_profile(requested_profile_id):
        # Relaunch controlado: abre diretamente no perfil solicitado.
        profile_service.set_active(requested_profile_id)
        _launch_profile_window(
            container,
            lang_manager,
            file_args,
            requested_profile_id,
        )

    elif profile_service.has_profiles() and len(profile_service.profiles) == 1:
        # ── Caso rápido: perfil único → pula seletor ──────────────────────
        last_id = profile_service.restore_last_active()
        if last_id is not None:
            profile_service.set_active(last_id)
        if last_id is None:
            raise RuntimeError("Profile selection returned no active profile.")
        _launch_profile_window(
            container,
            lang_manager,
            file_args,
            last_id,
        )

    else:
        # ── Mostra ProfileScreen ───────────────────────────────────────────
        screen = _create_profile_screen(container, lang_manager)
        _main_window_ref[0] = screen
        screen.setWindowTitle("Solin")
        screen.setMinimumSize(860, 580)
        screen.resize(960, 640)

        # Centra na tela
        from PySide6.QtGui import QGuiApplication

        geo = QGuiApplication.primaryScreen().availableGeometry()
        screen.move(
            geo.center().x() - screen.width() // 2,
            geo.center().y() - screen.height() // 2,
        )
        screen.show()

        def _on_profile_ready(profile_id: str):
            screen.hide()
            _launch_profile_window(
                container,
                lang_manager,
                file_args,
                profile_id,
            )
            # Fecha a tela de perfil de vez depois da animação
            QTimer.singleShot(400, screen.close)

        screen.profile_ready.connect(_on_profile_ready)
        _pm_screen = screen

        # Inicia fluxo correto
        screen.start()

    sys.exit(app.exec())


def run() -> None:
    main()


if __name__ == "__main__":
    run()
