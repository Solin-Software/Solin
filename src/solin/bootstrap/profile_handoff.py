"""Local startup gate separating the lifetimes of two profile processes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import re
import sys
import time
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from solin.controllers.ipc_controller import MAX_RECENT_REQUESTS

_CONNECT_TIMEOUT_MS = 15_000
_SHUTDOWN_TIMEOUT_MS = 120_000
_WAITING = b"waiting\n"
_RELEASED = b"ready "
_MAX_MESSAGE_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class ProfileHandoffState:
    pending_files: list[str]
    accepted_request_ids: list[str]


@contextmanager
def _parent_exit_guard(pid: int) -> Iterator[Callable[[float], bool]]:
    """Pin Windows process identity; never infer approval from a timeout."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            raise OSError(ctypes.get_last_error(), "Cannot observe the departing process.")
        try:
            yield (
                lambda deadline: (
                    kernel.WaitForSingleObject(
                        handle,
                        max(0, int((deadline - time.monotonic()) * 1000)),
                    )
                    == 0
                )
            )
        finally:
            kernel.CloseHandle(handle)
    else:

        def wait(deadline: float) -> bool:
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                except PermissionError:
                    return False
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            return False

        yield wait


class ProfileHandoff(QObject):
    """Release a waiting successor only after the departing runtime is shut down."""

    ready = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_connection)
        self._socket: QLocalSocket | None = None
        self._buffer = bytearray()
        self._waiting = False
        self._finished = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._fail("Replacement process did not connect."))

    def start(self) -> str:
        name = f"solin-profile-handoff-{os.getpid()}-{uuid4().hex}"
        if not self._server.listen(name):
            raise RuntimeError(self._server.errorString())
        self._timer.start(_CONNECT_TIMEOUT_MS)
        return name

    def _on_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            if self._socket is not None or self._finished:
                socket.abort()
                socket.deleteLater()
                continue
            self._socket = socket
            socket.readyRead.connect(self._on_read)
            socket.disconnected.connect(self._on_disconnected)
            self._on_read()

    def _on_read(self) -> None:
        if self._socket is None or self._finished:
            return
        self._buffer.extend(bytes(self._socket.readAll().data()))
        if not _WAITING.startswith(self._buffer):
            self._fail("Invalid replacement process handshake.")
        elif self._buffer == _WAITING and not self._waiting:
            self._waiting = True
            self._timer.stop()
            self.ready.emit()

    def _on_disconnected(self) -> None:
        if not self._finished:
            self._fail("Replacement process disconnected before handoff.")

    def _fail(self, message: str) -> None:
        if self._finished:
            return
        self.close()
        self.failed.emit(message)

    def release(self, pending_files: list[str], accepted_request_ids: list[str]) -> bool:
        socket = self._socket
        if (
            self._finished
            or not self._waiting
            or socket is None
            or socket.state() != QLocalSocket.LocalSocketState.ConnectedState
        ):
            return False
        self._finished = True
        self._timer.stop()
        payload = (
            _RELEASED
            + json.dumps(
                {"files": pending_files, "accepted": accepted_request_ids},
                ensure_ascii=True,
            ).encode("ascii")
            + b"\n"
        )
        if len(payload) > _MAX_MESSAGE_BYTES:
            return False
        written = socket.write(payload) == len(payload)
        deadline = time.monotonic() + 2
        while written and socket.bytesToWrite():
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            written = remaining > 0 and socket.waitForBytesWritten(remaining)
        socket.disconnectFromServer()
        self._server.close()
        return written

    def close(self) -> None:
        self._finished = True
        self._timer.stop()
        self._server.close()
        if self._socket is not None:
            self._socket.abort()


def wait_for_profile_handoff(name: str) -> ProfileHandoffState | None:
    """Block child bootstrap before it reads or mutates profile state; fail closed."""
    match = re.fullmatch(r"solin-profile-handoff-([1-9][0-9]*)-[0-9a-f]{32}", name)
    if match is None:
        return None
    pid = int(match[1])
    if pid == os.getpid():
        return None
    try:
        with _parent_exit_guard(pid) as wait_for_exit:
            files = _wait_for_release(name)
            if files is not None and wait_for_exit(files[1]):
                return files[0]
    except (OSError, OverflowError):
        return None
    return None


def _wait_for_release(name: str) -> tuple[ProfileHandoffState, float] | None:
    socket = QLocalSocket()
    try:
        socket.connectToServer(name)
        if not socket.waitForConnected(_CONNECT_TIMEOUT_MS):
            return None
        if socket.write(_WAITING) != len(_WAITING):
            return None
        if socket.bytesToWrite() and not socket.waitForBytesWritten(2000):
            return None
        reply = bytearray()
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_MS / 1000
        while True:
            reply.extend(bytes(socket.readAll().data()))
            if len(reply) > _MAX_MESSAGE_BYTES:
                return None
            if b"\n" in reply:
                if not reply.startswith(_RELEASED) or not reply.endswith(b"\n"):
                    return None
                try:
                    state = json.loads(reply[len(_RELEASED) : -1])
                except (ValueError, UnicodeError):
                    return None
                if not isinstance(state, dict):
                    return None
                files, accepted = state.get("files"), state.get("accepted")
                if (
                    isinstance(files, list)
                    and all(isinstance(path, str) for path in files)
                    and isinstance(accepted, list)
                    and len(accepted) <= MAX_RECENT_REQUESTS
                    and all(
                        isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier)
                        for identifier in accepted
                    )
                ):
                    return ProfileHandoffState(files, accepted), deadline
                return None
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if remaining == 0 or socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                return None
            socket.waitForReadyRead(remaining)
    finally:
        socket.abort()
