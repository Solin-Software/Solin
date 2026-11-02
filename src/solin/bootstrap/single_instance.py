from __future__ import annotations

import hashlib
import logging
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QLockFile, QStandardPaths
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from solin.controllers.ipc_controller import IpcController, encode_open_request
from solin.core.foundation.constants import IPC_SERVER_NAME, IPC_TIMEOUT_MS

log = logging.getLogger(__name__)
_FORWARD_TIMEOUT_SECONDS = 15.0


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

    def __init__(
        self,
        app: QApplication,
        window_ref: list,
        pending_files: list[str],
        *,
        server_name: str = IPC_SERVER_NAME,
    ):
        self._app = app
        self._window_ref = window_ref
        self._pending_files = pending_files
        self._server_name = server_name
        self._server: QLocalServer | None = None
        self._connections: set[QLocalSocket] = set()
        self._lock: QLockFile | None = None
        self._reserved_until_exit = False
        self._receiver = IpcController(
            app,
            bring_to_front=self._bring_current_window_to_front,
            open_media_files=self._deliver_files,
        )

    @property
    def accepted_request_ids(self) -> list[str]:
        return self._receiver.accepted_request_ids

    def restore_request_ids(self, identifiers: list[str]) -> None:
        self._receiver.restore_request_ids(identifiers)

    def start(self) -> bool:
        if self._server is not None and self._server.isListening():
            return True
        if self._reserved_until_exit:
            return False
        lock = self._lock
        if lock is None:
            directory = Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.RuntimeLocation,
                )
            )
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError:
                log.exception("Could not create the single-instance lock directory")
                return False
            name_hash = hashlib.sha256(self._server_name.encode("utf-8")).hexdigest()
            lock = QLockFile(str(directory / f"solin-{name_hash}.lock"))
            # A running owner cannot be displaced just because its lock is old.
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                return False

        server = QLocalServer(self._app)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        server.newConnection.connect(self._on_connection)

        if not server.listen(self._server_name):
            probe = QLocalSocket()
            probe.connectToServer(self._server_name)
            connected = probe.waitForConnected(IPC_TIMEOUT_MS)
            error = probe.error()
            probe.abort()
            if connected or error not in (
                QLocalSocket.LocalSocketError.ServerNotFoundError,
                QLocalSocket.LocalSocketError.ConnectionRefusedError,
            ):
                server.deleteLater()
                lock.unlock()
                self._lock = None
                return False
            # Only remove an unreachable stale endpoint, never a live owner.
            QLocalServer.removeServer(self._server_name)
            if not server.listen(self._server_name):
                server.deleteLater()
                lock.unlock()
                self._lock = None
                return False

        self._lock = lock
        self._server = server
        app_dynamic: Any = self._app
        app_dynamic._solin_single_instance_server = self
        app_dynamic._solin_app_ipc_server_active = True
        return True

    def close(self) -> None:
        self.pause()
        app_dynamic: Any = self._app
        if not self._reserved_until_exit and self._lock is not None:
            self._lock.unlock()
            self._lock = None
        if getattr(app_dynamic, "_solin_single_instance_server", None) is self:
            if not self._reserved_until_exit:
                app_dynamic._solin_single_instance_server = None
            app_dynamic._solin_app_ipc_server_active = False

    def pause(self) -> None:
        """Stop admission and drain complete frames before synchronous shutdown."""
        server = self._server
        if server is None:
            return
        self._server = None
        try:
            server.close()
        finally:
            for connection in tuple(self._connections):
                try:
                    if connection.bytesAvailable():
                        self._receiver.receive(connection)
                except Exception:  # noqa: BLE001 - isolate accepted request failures during shutdown
                    log.exception("Could not drain a pending file-open request")
                finally:
                    self._receiver.discard_connection(connection)
                    connection.abort()
            self._connections.clear()
            server.deleteLater()

    def reserve_until_exit(self) -> None:
        """Keep startup admission locked while a departing process drains workers."""
        self._reserved_until_exit = True

    def _on_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                return
            self._connections.add(conn)
            conn.disconnected.connect(lambda connection=conn: self._connections.discard(connection))
            self._receiver.attach_connection(conn)

    def _deliver_files(self, paths: list[str]) -> None:
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
        if not getattr(window, "accepts_external_activation", True):
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


def try_forward_to_running(
    paths: list[str],
    *,
    server_name: str = IPC_SERVER_NAME,
    request_id: str | None = None,
    wait_for_owner: bool = False,
) -> bool:
    identifier = request_id or uuid4().hex
    payload = encode_open_request(paths, identifier)
    expected = identifier.encode("ascii") + b"\n"
    deadline = time.monotonic() + _FORWARD_TIMEOUT_SECONDS
    retry = wait_for_owner
    while True:
        socket = QLocalSocket()
        try:
            socket.connectToServer(server_name)
            connected = socket.waitForConnected(IPC_TIMEOUT_MS)
            if connected:
                retry = True
                grant_focus_to_first_instance()
                if socket.write(payload) == len(payload):
                    socket.flush()
                    while socket.bytesToWrite() and time.monotonic() < deadline:
                        remaining = max(0, int((deadline - time.monotonic()) * 1000))
                        if not socket.waitForBytesWritten(min(remaining, 2000)):
                            break
                    reply = bytearray()
                    while time.monotonic() < deadline:
                        reply.extend(bytes(socket.readAll().data()))
                        if reply == expected:
                            return True
                        if not expected.startswith(reply):
                            break
                        remaining = max(0, int((deadline - time.monotonic()) * 1000))
                        if not socket.waitForReadyRead(min(remaining, 2000)):
                            reply.extend(bytes(socket.readAll().data()))
                            if reply == expected:
                                return True
                            break
        finally:
            socket.abort()
        if not retry or time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
