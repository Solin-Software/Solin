from __future__ import annotations

import logging
import sys
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from solin.controllers.ipc_controller import IpcController
from solin.core.foundation.constants import IPC_SERVER_NAME, IPC_TIMEOUT_MS

log = logging.getLogger(__name__)


def _log_ignored_exception(message: str, *, warning: bool = False) -> None:
    if warning:
        log.warning(message, exc_info=True)
    else:
        log.debug(message, exc_info=True)


class SingleInstanceServer:
    """
    App-wide local socket server.

    It is intentionally owned by bootstrap instead of MainWindow so the single
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
        app_dynamic: Any = self._app
        app_dynamic._solin_single_instance_server = self
        app_dynamic._solin_app_ipc_server_active = True
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
        app_dynamic: Any = self._app
        app_dynamic._solin_app_ipc_server_active = False

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
        text = bytes(raw.data()).decode("utf-8", errors="ignore").strip()

        self._bring_current_window_to_front()

        if not text:
            return

        paths = IpcController._valid_payload_paths(text)
        if not paths:
            return

        window = self._current_window()
        open_media_files = getattr(window, "open_media_files", None)
        if callable(open_media_files):
            open_media_files(paths)
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

        bring_to_front = getattr(window, "_bring_to_front", None)
        if callable(bring_to_front):
            bring_to_front()
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


def grant_focus_to_first_instance() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ASFW_ANY = -1
            ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
        except Exception:  # noqa: BLE001 - Win32 foreground API boundary
            _log_ignored_exception("Could not grant foreground permission to first instance")


def try_forward_to_running(paths: list[str]) -> bool:
    socket = QLocalSocket()
    socket.connectToServer(IPC_SERVER_NAME)
    if not socket.waitForConnected(IPC_TIMEOUT_MS):
        return False
    grant_focus_to_first_instance()
    payload = ("\n".join(paths) + "\n").encode("utf-8") if paths else b"\n"
    socket.write(payload)
    socket.flush()
    socket.waitForBytesWritten(2000)
    socket.disconnectFromServer()
    return True


def stop_process_ipc(app, window=None):
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


def restart_process_ipc(app, server, window=None) -> None:
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
        _log_ignored_exception("Could not restart legacy window IPC server")
