import sys
import os
import logging
from pathlib import Path

# Ensure the app root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
from PySide6.QtCore import Qt, QCoreApplication, QTimer, QProcess
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# Apenas constantes puras — sem dependência de caminhos ou QApplication.
from app.core.foundation.constants import (
    IPC_SERVER_NAME,
    IPC_TIMEOUT_MS,
    APP_VERSION,
    QSETTINGS_APP_APP,
    QT_APPLICATION_NAME,
    QT_ORGANIZATION_NAME,
)
from app.core.foundation.settings_keys import SettingsKey
from app.controllers.ipc_controller import IpcController

log = logging.getLogger(__name__)


def _log_ignored_exception(message: str, *, warning: bool = False) -> None:
    if warning:
        log.warning(message, exc_info=True)
    else:
        log.debug(message, exc_info=True)


class _SingleInstanceServer:
    """
    App-wide local socket server.

    It is intentionally owned by main.py instead of MainWindow so the single
    instance guarantee is active while the profile selector/onboarding is open.
    """

    def __init__(self, app: QApplication, window_ref: list, pending_files: list[str]):
        self._app = app
        self._window_ref = window_ref
        self._pending_files = pending_files
        self._server: QLocalServer | None = None

    def start(self) -> bool:
        if self._server is not None and self._server.isListening():
            return True

        server = QLocalServer(self._app)
        server.newConnection.connect(self._on_connection)

        if not server.listen(IPC_SERVER_NAME):
            QLocalServer.removeServer(IPC_SERVER_NAME)
            if not server.listen(IPC_SERVER_NAME):
                return False

        self._server = server
        self._app._solin_single_instance_server = self
        self._app._solin_app_ipc_server_active = True
        try:
            self._app.aboutToQuit.connect(self.close)
        except Exception:  # noqa: BLE001 - Qt application lifecycle boundary
            _log_ignored_exception("Could not register IPC cleanup hook", warning=True)
        return True

    def close(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
        except Exception:  # noqa: BLE001 - Qt IPC cleanup boundary
            _log_ignored_exception("Could not close single-instance IPC server", warning=True)
        QLocalServer.removeServer(IPC_SERVER_NAME)
        self._app._solin_app_ipc_server_active = False

    def _on_connection(self) -> None:
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda: self._on_ipc_data(conn))
        QTimer.singleShot(3000, lambda: conn.disconnectFromServer())

    def _on_ipc_data(self, conn: QLocalSocket) -> None:
        raw = conn.readAll()
        text = raw.data().decode("utf-8", errors="ignore").strip()

        self._bring_current_window_to_front()

        if not text:
            return

        paths = IpcController._valid_payload_paths(text)
        if not paths:
            return

        window = self._current_window()
        if window is not None and hasattr(window, "open_media_files"):
            window.open_media_files(paths)
        else:
            for path in paths:
                if path not in self._pending_files:
                    self._pending_files.append(path)

    def _current_window(self):
        window = self._window_ref[0] if self._window_ref else None
        if window is not None:
            return window

        active = QApplication.activeWindow()
        if active is not None:
            return active

        for candidate in QApplication.topLevelWidgets():
            if candidate.isVisible():
                return candidate
        return None

    def _bring_current_window_to_front(self) -> None:
        window = self._current_window()
        if window is None:
            return

        if hasattr(window, "_bring_to_front"):
            window._bring_to_front()
            return

        if window.isMinimized():
            window.showNormal()
        elif not window.isVisible():
            window.show()

        window.raise_()
        window.activateWindow()

        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(int(window.winId()))
            except Exception:  # noqa: BLE001 - Win32 foreground API boundary
                _log_ignored_exception("Could not force foreground window on Windows")


def _grant_focus_to_first_instance():
    if sys.platform == "win32":
        try:
            import ctypes
            ASFW_ANY = -1
            ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:  # noqa: BLE001 - Win32 foreground API boundary
            _log_ignored_exception("Could not grant foreground permission to first instance")


def _try_forward_to_running(paths: list) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(IPC_SERVER_NAME)
    if not socket.waitForConnected(IPC_TIMEOUT_MS):
        return False
    _grant_focus_to_first_instance()
    payload = ("\n".join(paths) + "\n").encode("utf-8") if paths else b"\n"
    socket.write(payload)
    socket.flush()
    socket.waitForBytesWritten(2000)
    socket.disconnectFromServer()
    return True


def _stop_process_ipc(app, window=None):
    server = getattr(app, "_solin_single_instance_server", None)
    if server is not None and hasattr(server, "close"):
        server.close()
        return server

    try:
        if window is not None and hasattr(window, "_ipc_server"):
            window._ipc_server.close()
    except Exception:  # noqa: BLE001 - legacy Qt IPC cleanup boundary
        _log_ignored_exception("Could not close legacy window IPC server", warning=True)
    QLocalServer.removeServer(IPC_SERVER_NAME)
    return None


def _restart_process_ipc(app, server, window=None) -> None:
    restarted = False
    if server is not None and hasattr(server, "start"):
        try:
            restarted = bool(server.start())
        except Exception:  # noqa: BLE001 - Qt IPC lifecycle boundary
            _log_ignored_exception("Could not restart app-owned IPC server", warning=True)
            restarted = False
    if restarted:
        return

    try:
        if window is not None and hasattr(window, "_start_ipc_server"):
            window._start_ipc_server()
    except Exception:  # noqa: BLE001 - legacy Qt IPC lifecycle boundary
        _log_ignored_exception("Could not restart legacy window IPC server", warning=True)


def _split_runtime_args(argv: list[str]) -> tuple[str, bool, list[str]]:
    """
    Extrai argumentos internos do Solin e devolve apenas os caminhos de mídia.

    --profile <id> é usado pelo relaunch controlado de troca de perfil.
    --create-profile abre o onboarding para criar um perfil adicional.
    O restante continua compatível com o comportamento atual: só arquivos
    existentes entram como mídia de abertura.
    """
    requested_profile = ""
    create_profile = False
    file_args: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--create-profile":
            create_profile = True
            i += 1
            continue
        if arg == "--profile":
            if i + 1 < len(argv):
                requested_profile = argv[i + 1]
                i += 2
                continue
            i += 1
            continue
        if arg.startswith("--profile="):
            requested_profile = arg.split("=", 1)[1]
            i += 1
            continue
        if os.path.isfile(arg):
            file_args.append(arg)
        i += 1
    return requested_profile, create_profile, file_args


def _relaunch_with_profile(app, profile_id: str, window) -> None:
    """
    Troca de perfil com fronteira de processo: agenda o próximo perfil, fecha a
    janela atual ainda no contexto antigo e inicia outro processo.
    """
    from app.core.foundation.constants import QSETTINGS_GLOBAL_APP, QSETTINGS_ORG_NAME
    from app.core.profiles.manager import get as _get_pm
    from PySide6.QtCore import QSettings

    if not _get_pm().get_profile(profile_id):
        return

    gs = QSettings(QSETTINGS_ORG_NAME, QSETTINGS_GLOBAL_APP)
    gs.setValue(SettingsKey.LAST_ACTIVE_PROFILE, profile_id)
    gs.sync()

    ipc_server = _stop_process_ipc(app, window)

    script_path = Path(__file__).resolve()
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        program = QCoreApplication.applicationFilePath() or sys.executable
        args = ["--profile", profile_id]
    else:
        program = sys.executable
        args = [str(script_path), "--profile", profile_id]

    if not QProcess.startDetached(program, args, str(script_path.parent)):
        try:
            if window is not None:
                window.setEnabled(True)
                _restart_process_ipc(app, ipc_server, window)
                window.show()
        except Exception:  # noqa: BLE001 - Qt relaunch recovery boundary
            _log_ignored_exception("Could not restore window after profile relaunch failure", warning=True)
        return

    try:
        if window is not None:
            window.close()
    except Exception:  # noqa: BLE001 - Qt window lifecycle boundary
        _log_ignored_exception("Could not close old window after profile relaunch")
    app.processEvents()
    app.quit()


def _relaunch_to_profile_creator(app, window) -> None:
    """Reabre o app diretamente no onboarding de criação de perfil."""
    try:
        from app.core.foundation.constants import QSETTINGS_GLOBAL_APP, QSETTINGS_ORG_NAME
        from app.core.profiles.manager import get as _get_pm
        from PySide6.QtCore import QSettings

        pm = _get_pm()
        current_lang = (
            pm.prefs(QSETTINGS_APP_APP).value(SettingsKey.APP_LANGUAGE, "", str)
            if pm.active_id
            else ""
        )
        if current_lang:
            gs = QSettings(QSETTINGS_ORG_NAME, QSETTINGS_GLOBAL_APP)
            gs.setValue(SettingsKey.BOOTSTRAP_LANGUAGE, current_lang)
            gs.sync()
    except Exception:  # noqa: BLE001 - Qt settings adapter boundary
        _log_ignored_exception("Could not persist bootstrap language for profile creation")

    ipc_server = _stop_process_ipc(app, window)

    script_path = Path(__file__).resolve()
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        program = QCoreApplication.applicationFilePath() or sys.executable
        args = ["--create-profile"]
    else:
        program = sys.executable
        args = [str(script_path), "--create-profile"]

    if not QProcess.startDetached(program, args, str(script_path.parent)):
        try:
            if window is not None:
                window.setEnabled(True)
                _restart_process_ipc(app, ipc_server, window)
                window.show()
        except Exception:  # noqa: BLE001 - Qt relaunch recovery boundary
            _log_ignored_exception("Could not restore window after profile-creator relaunch failure", warning=True)
        return

    try:
        if window is not None:
            window.close()
    except Exception:  # noqa: BLE001 - Qt window lifecycle boundary
        _log_ignored_exception("Could not close old window after profile-creator relaunch")
    app.processEvents()
    app.quit()


def _launch_main_window(app, lang_manager, file_args):
    """
    Cria e exibe o MainWindow para o perfil já ativo.
    Retorna a instância do MainWindow.
    """
    from app.main_window import MainWindow
    from app.core.ui.titlebar import apply_titlebar_color

    window = MainWindow(lang_manager)
    window.show()

    apply_titlebar_color(window, "#1A231F")

    if file_args:
        QTimer.singleShot(200, lambda: window.open_media_files(file_args))

    return window


def _launch_zoom_poll_window(filepath: str, lang_manager):
    from app.widgets.zoom_poll_widget import launch_zoom_poll

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
    from app.core.foundation import paths as _paths
    _paths.init()
    _paths.ensure_dirs()

    from app.core.foundation.logging_config import configure_logging
    configure_logging(_paths.LOG_DIR)
    log.info("Starting Solin %s", APP_VERSION)

    # ── Inicializa ProfileManager ─────────────────────────────────────────────
    from app.core.profiles.manager import get as _get_pm
    _pm = _get_pm()
    _pm.init(_paths.DATA_DIR)

    # ── Imports dependentes de caminhos ───────────────────────────────────────
    from app.core.i18n.manager import LanguageManager

    # ── Ícone da aplicação ────────────────────────────────────────────────────
    _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.ico")
    if os.path.isfile(_icon_path):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(_icon_path))

    # ── Argumentos da linha de comando ────────────────────────────────────────
    requested_profile_id, create_profile_mode, file_args = _split_runtime_args(sys.argv)
    _main_window_ref = [None]   # lista para captura por closure mutável

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
    if _try_forward_to_running(file_args):
        sys.exit(0)
    single_instance = _SingleInstanceServer(app, _main_window_ref, file_args)
    if not single_instance.start() and _try_forward_to_running(file_args):
        sys.exit(0)

    # ── Primeira instância — limpezas ─────────────────────────────────────────
    from app.widgets.update_dialog import cleanup_pending_patch
    cleanup_pending_patch()

    from app.core.media.downloader import cleanup_orphan_temps, cleanup_incomplete_cache
    cleanup_orphan_temps()
    cleanup_incomplete_cache()

    # ── Decisão: perfil direto ou tela de seleção ─────────────────────────────
    #
    #   • 0 perfis + sem legado  → Onboarding completo
    #   • 0 perfis + com legado  → Tela de migração
    #   • 1 perfil               → Abre direto (sem seletor)
    #   • 2+ perfis              → Seletor Netflix

    if create_profile_mode:
        from app.ui.profile_screen import ProfileScreen

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
            _wire_profile_switch(app, lang_manager, file_args, _main_window_ref)
            QTimer.singleShot(400, screen.close)

        screen.profile_ready.connect(_on_profile_ready)
        _pm_screen = screen
        screen.start_new_profile()

    elif requested_profile_id and _pm.get_profile(requested_profile_id):
        # Relaunch controlado: abre diretamente no perfil solicitado.
        _pm.set_active(requested_profile_id)

        from app.core.profiles import settings as _ps
        s = _ps.prefs(QSETTINGS_APP_APP)
        saved_lang = s.value(SettingsKey.APP_LANGUAGE, "", str)
        if saved_lang:
            lang_manager.set_language(saved_lang)

        window = _launch_main_window(app, lang_manager, file_args)
        _main_window_ref[0] = window
        _wire_profile_switch(app, lang_manager, file_args, _main_window_ref)

    elif _pm.has_profiles() and len(_pm.profiles) == 1:
        # ── Caso rápido: perfil único → pula seletor ──────────────────────
        last_id = _pm.restore_last_active()
        _pm.set_active(last_id)

        # Restaura idioma do perfil
        from app.core.profiles import settings as _ps
        s = _ps.prefs(QSETTINGS_APP_APP)
        saved_lang = s.value(SettingsKey.APP_LANGUAGE, "", str)
        if saved_lang:
            lang_manager.set_language(saved_lang)

        window = _launch_main_window(app, lang_manager, file_args)
        _main_window_ref[0] = window
        _wire_profile_switch(app, lang_manager, file_args, _main_window_ref)

    else:
        # ── Mostra ProfileScreen ───────────────────────────────────────────
        from app.ui.profile_screen import ProfileScreen

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
            _wire_profile_switch(app, lang_manager, file_args, _main_window_ref)
            # Fecha a tela de perfil de vez depois da animação
            QTimer.singleShot(400, screen.close)

        screen.profile_ready.connect(_on_profile_ready)
        _pm_screen = screen

        # Inicia fluxo correto
        screen.start()

    sys.exit(app.exec())


def _wire_profile_switch(app, lang_manager, file_args, window_ref):
    """
    Conecta o sinal switch_profile_requested do MainWindow para exibir o
    overlay de seleção de perfil SOBRE a janela existente (sem abrir nova janela).

    Fluxo:
      1. Usuário clica no avatar → overlay cobre o MainWindow inteiro
      2. Clica no mesmo perfil / X / Esc → overlay some, nada muda
      3. Clica em perfil diferente → fecha a janela atual e relança o app
         com --profile <id>. Essa fronteira de processo evita reaproveitar
         recursos nativos de browser ainda vivos no processo antigo.
    """
    window = window_ref[0]
    if not hasattr(window, "switch_profile_requested"):
        return

    def _on_switch():
        from app.ui.profile_switch_overlay import ProfileSwitchOverlay
        from app.core.profiles.manager import get as _get_pm

        _pm = _get_pm()
        current_id = _pm.active_profile.id if _pm.active_profile else ""

        old_win = window_ref[0]
        if old_win is None:
            return

        overlay = ProfileSwitchOverlay(old_win, current_id)
        overlay.show()
        overlay.raise_()

        def _on_cancelled():
            # Limpa o event filter e destroi o overlay silenciosamente
            try:
                old_win.removeEventFilter(overlay)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                _log_ignored_exception("Could not remove profile switch event filter on cancel")
            overlay.deleteLater()

        def _on_profile_selected(profile_id: str):
            # ── Passo 1: fecha o overlay ──────────────────────────────────
            try:
                old_win.removeEventFilter(overlay)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                _log_ignored_exception("Could not remove profile switch event filter before relaunch")
            overlay.deleteLater()

            old_win.setEnabled(False)
            _relaunch_with_profile(app, profile_id, old_win)

        def _on_create_profile_requested():
            try:
                old_win.removeEventFilter(overlay)
            except Exception:  # noqa: BLE001 - Qt event-filter cleanup boundary
                _log_ignored_exception("Could not remove profile switch event filter before profile creation")
            overlay.deleteLater()

            old_win.setEnabled(False)
            _relaunch_to_profile_creator(app, old_win)

        overlay.cancelled.connect(_on_cancelled)
        overlay.profile_selected.connect(_on_profile_selected)
        overlay.create_profile_requested.connect(_on_create_profile_requested)

    window.switch_profile_requested.connect(_on_switch)


if __name__ == "__main__":
    main()
