import os
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

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QCoreApplication, QTimer

# Apenas constantes puras — sem dependência de caminhos ou QApplication.
from solin.bootstrap.config import default_app_config
from solin.bootstrap.container import initialize_application_container
from solin.core.foundation.resources import application_asset_path
from solin.bootstrap.profile_flow import wire_profile_switch
from solin.bootstrap.runtime_args import parse_runtime_args
from solin.bootstrap.single_instance import (
    SingleInstanceServer,
    try_forward_to_running,
)
from solin.core.profiles.application import ProfileRegistryLoadError

def _launch_main_window(
    app,
    lang_manager,
    file_args,
    runtime_paths,
    profile_paths,
    profile_settings,
    media,
    font_manager,
    jw_catalog_cache_paths,
    jw_songs_store,
    jwpub_checksum_store,
    timer_session,
    active_profile,
):
    """
    Cria e exibe o MainWindow para o perfil já ativo.
    Retorna a instância do MainWindow.
    """
    from solin.main_window import MainWindow
    from solin.core.meetings.tree_store import MeetingTreeStore
    from solin.core.media.settings import MediaSettingsStore
    from solin.core.playlists.storage import PlaylistRepository, PlaylistStoragePaths
    from solin.core.ui.titlebar import apply_titlebar_color

    playlist_storage_paths = PlaylistStoragePaths(
        playlists_file=profile_paths.playlists_file,
        pending_deletions_file=runtime_paths.pending_del_file,
    )
    playlist_repository = PlaylistRepository.from_paths(playlist_storage_paths)
    meeting_tree_store = MeetingTreeStore(profile_paths.meeting_trees_file)
    media_settings = MediaSettingsStore.for_profile_settings(profile_settings)
    media_controller = media.create_playback(media_settings)
    background_media_controller = media.create_playback(media_settings)
    window = MainWindow(
        lang_manager,
        runtime_paths,
        profile_paths,
        profile_settings,
        media.cache_manager,
        media_controller,
        background_media_controller,
        media.create_info_queue,
        media.create_info_service,
        media.create_browser_download_service,
        media.create_browser_image_fetch_service,
        media_settings,
        font_manager,
        jw_catalog_cache_paths,
        jw_songs_store,
        jwpub_checksum_store,
        playlist_storage_paths,
        playlist_repository,
        meeting_tree_store,
        timer_session,
        active_profile,
    )
    media_controller.setParent(window)
    background_media_controller.setParent(window)
    window.show()

    apply_titlebar_color(window, "#1A231F")

    if file_args:
        QTimer.singleShot(200, lambda: window.open_media_files(file_args))

    return window


def _launch_zoom_poll_window(filepath: str, lang_manager):
    from solin.widgets.zoom_poll_widget import launch_zoom_poll

    return launch_zoom_poll(filepath, lang_manager)


def _run_zoom_poll_standalone(app, filepath: str, lang_manager) -> int:
    app._solin_zoom_poll_window = _launch_zoom_poll_window(filepath, lang_manager)
    return app.exec()


def _create_profile_screen(container, lang_manager):
    from solin.controllers.onboarding_obs_probe import OnboardingOBSProbe
    from solin.ui.profile_screen import ProfileScreen

    obs_probe = OnboardingOBSProbe()
    container.lifecycle.register_cleanup(obs_probe.shutdown)
    return ProfileScreen(
        lang_manager,
        profile_service=container.profile_service,
        profile_settings_for=lambda profile_id: (
            container.profile_runtime.create(profile_id).settings
        ),
        onboarding_service=container.onboarding_service,
        obs_probe=obs_probe,
    )


def _launch_profile_window(container, lang_manager, file_args, profile_id: str):
    from solin.core.meetings.publications import current_monday
    from solin.core.timer.application import TimerSession
    from solin.core.timer.infrastructure import QSettingsTimerRepository

    profile_context = container.profile_runtime.create(profile_id)
    active_profile = container.profile_service.get_profile(profile_id)
    if active_profile is None:
        raise RuntimeError(f"Profile disappeared during startup: {profile_id}")

    lang_manager.activate_profile(profile_context.settings)
    window = _launch_main_window(
        container.app,
        lang_manager,
        file_args,
        container.runtime_paths,
        profile_context.paths,
        profile_context.settings,
        container.media,
        container.font_manager,
        container.jw_catalog_cache_paths,
        container.jw_songs_store,
        container.jwpub_checksum_store,
        TimerSession(
            QSettingsTimerRepository.for_profile_settings(profile_context.settings),
            current_week_monday=current_monday(),
        ),
        active_profile,
    )
    container.window_ref[0] = window
    wire_profile_switch(
        container.app,
        container.window_ref,
        container.profile_service,
    )
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

    # ── LanguageManager ───────────────────────────────────────────────────────
    lang_manager = LanguageManager(
        global_settings=container.global_settings,
        jw_languages_cache_file=container.runtime_paths.cache_dir / "jw_languages.json"
    )
    container.lifecycle.register_cleanup(lang_manager.shutdown)

    # ── CSV do Zoom: janela standalone ────────────────────────────────────────
    csv_args = [
        a for a in file_args
        if os.path.isfile(a) and os.path.splitext(a)[1].lower() == ".csv"
    ]
    if csv_args:
        sys.exit(_run_zoom_poll_standalone(app, csv_args[0], lang_manager))

    # ── Single-instance ───────────────────────────────────────────────────────
    if try_forward_to_running(file_args):
        sys.exit(0)
    single_instance = SingleInstanceServer(app, _main_window_ref, file_args)
    single_instance_started = single_instance.start()
    if not single_instance_started and try_forward_to_running(file_args):
        sys.exit(0)
    if single_instance_started:
        container.lifecycle.register_single_instance(single_instance)

    # ── Primeira instância — limpezas ─────────────────────────────────────────
    from solin.core.remote.patch_installer import cleanup_pending_patch
    cleanup_pending_patch()

    from solin.core.media.download_storage import cleanup_orphan_temps, cleanup_incomplete_cache
    cleanup_orphan_temps()
    cleanup_incomplete_cache(container.runtime_paths.media_cache_dir)

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
