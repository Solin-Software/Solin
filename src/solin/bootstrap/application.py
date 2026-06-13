import logging
import os
import sys
from typing import Any

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
from solin.core.foundation.constants import (
    APP_VERSION,
    QSETTINGS_APP_APP,
    QT_APPLICATION_NAME,
    QT_ORGANIZATION_NAME,
)
from solin.core.foundation.resources import application_asset_path
from solin.core.foundation.settings_keys import SettingsKey
from solin.bootstrap.profile_flow import wire_profile_switch
from solin.bootstrap.runtime_args import parse_runtime_args
from solin.bootstrap.single_instance import (
    SingleInstanceServer,
    try_forward_to_running,
)

log = logging.getLogger(__name__)


def _launch_main_window(app, lang_manager, file_args):
    """
    Cria e exibe o MainWindow para o perfil já ativo.
    Retorna a instância do MainWindow.
    """
    from solin.main_window import MainWindow
    from solin.core.ui.titlebar import apply_titlebar_color

    window = MainWindow(lang_manager)
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


def main():
    # High-DPI support
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    from PySide6.QtGui import QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    app.setApplicationName(QT_APPLICATION_NAME)
    app.setOrganizationName(QT_ORGANIZATION_NAME)
    app.setApplicationVersion(APP_VERSION)

    # ── Inicializa caminhos de dados/cache via QStandardPaths ─────────────────
    from solin.core.foundation import paths as _paths
    _paths.init()
    _paths.ensure_dirs()

    from solin.core.foundation.logging_config import configure_logging
    configure_logging(_paths.LOG_DIR)
    log.info("Starting Solin %s", APP_VERSION)

    # ── Inicializa ProfileManager ─────────────────────────────────────────────
    from solin.core.profiles.manager import get as _get_pm
    _pm = _get_pm()
    _pm.init(_paths.DATA_DIR)

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
    _main_window_ref: list[Any | None] = [None]   # lista para captura por closure mutável

    # ── LanguageManager ───────────────────────────────────────────────────────
    lang_manager = LanguageManager()

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
    if not single_instance.start() and try_forward_to_running(file_args):
        sys.exit(0)

    # ── Primeira instância — limpezas ─────────────────────────────────────────
    from solin.widgets.update_dialog import cleanup_pending_patch
    cleanup_pending_patch()

    from solin.core.media.downloader import cleanup_orphan_temps, cleanup_incomplete_cache
    cleanup_orphan_temps()
    cleanup_incomplete_cache()

    # ── Decisão: perfil direto ou tela de seleção ─────────────────────────────
    #
    #   • 0 perfis + sem legado  → Onboarding completo
    #   • 0 perfis + com legado  → Tela de migração
    #   • 1 perfil               → Abre direto (sem seletor)
    #   • 2+ perfis              → Seletor Netflix

    if create_profile_mode:
        from solin.ui.profile_screen import ProfileScreen

        screen = ProfileScreen(lang_manager)
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
            window = _launch_main_window(app, lang_manager, file_args)
            _main_window_ref[0] = window
            wire_profile_switch(app, _main_window_ref)
            QTimer.singleShot(400, screen.close)

        screen.profile_ready.connect(_on_profile_ready)
        _pm_screen = screen
        screen.start_new_profile()

    elif requested_profile_id and _pm.get_profile(requested_profile_id):
        # Relaunch controlado: abre diretamente no perfil solicitado.
        _pm.set_active(requested_profile_id)

        from solin.core.profiles import settings as _ps
        s = _ps.prefs(QSETTINGS_APP_APP)
        saved_lang = str(s.value(SettingsKey.APP_LANGUAGE, "", str) or "")
        if saved_lang:
            lang_manager.set_language(saved_lang)

        window = _launch_main_window(app, lang_manager, file_args)
        _main_window_ref[0] = window
        wire_profile_switch(app, _main_window_ref)

    elif _pm.has_profiles() and len(_pm.profiles) == 1:
        # ── Caso rápido: perfil único → pula seletor ──────────────────────
        last_id = _pm.restore_last_active()
        if last_id is not None:
            _pm.set_active(last_id)

        # Restaura idioma do perfil
        from solin.core.profiles import settings as _ps
        s = _ps.prefs(QSETTINGS_APP_APP)
        saved_lang = str(s.value(SettingsKey.APP_LANGUAGE, "", str) or "")
        if saved_lang:
            lang_manager.set_language(saved_lang)

        window = _launch_main_window(app, lang_manager, file_args)
        _main_window_ref[0] = window
        wire_profile_switch(app, _main_window_ref)

    else:
        # ── Mostra ProfileScreen ───────────────────────────────────────────
        from solin.ui.profile_screen import ProfileScreen

        screen = ProfileScreen(lang_manager)
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
            window = _launch_main_window(app, lang_manager, file_args)
            _main_window_ref[0] = window
            wire_profile_switch(app, _main_window_ref)
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
