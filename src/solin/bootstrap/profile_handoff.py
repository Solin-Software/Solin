"""Local startup gate separating the lifetimes of two profile processes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
from math import ceil
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
_STATE = b"state "
_PREPARED = b"prepared\n"
_RELEASED = b"ready\n"
_MAX_MESSAGE_BYTES = 1_048_576
# Leave room for Darwin's per-user temporary path without reducing token entropy.
_CHANNEL_PREFIX = "solin"


@dataclass(frozen=True, slots=True)
class ProfileHandoffState:
    pending_files: list[str]
    accepted_request_ids: list[str]


def _state_frames(pending_files: list[str], accepted_request_ids: list[str]) -> Iterator[bytes]:
    """Bound each transport frame, allowing multiple accepted requests per handoff."""
    suffix = b',"accepted":' + json.dumps(accepted_request_ids).encode("ascii") + b"}\n"
    prefix = _STATE + b'{"files":['
    batch: list[bytes] = []
    size = len(prefix) + 1 + len(suffix)
    if size > _MAX_MESSAGE_BYTES:
        raise ValueError("Request history exceeds handoff frame capacity.")
    for path in pending_files:
        encoded = json.dumps(path, ensure_ascii=True).encode("ascii")
        if len(prefix) + len(encoded) + 1 + len(suffix) > _MAX_MESSAGE_BYTES:
            raise ValueError("A pending file exceeds handoff frame capacity.")
        extra = len(encoded) + bool(batch)
        if size + extra > _MAX_MESSAGE_BYTES:
            yield prefix + b",".join(batch) + b"]" + suffix
            batch = []
            size = len(prefix) + 1 + len(suffix)
            extra = len(encoded)
        batch.append(encoded)
        size += extra
    yield prefix + b",".join(batch) + b"]" + suffix


@contextmanager
def _parent_exit_guard(pid: int) -> Iterator[Callable[[float], bool]]:
    """Pin process identity and observe exit independently of launcher reaping."""
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
    elif sys.platform == "linux":
        import select

        descriptor = os.pidfd_open(pid)
        try:
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)

            def wait(deadline: float) -> bool:
                timeout = max(0, ceil((deadline - time.monotonic()) * 1000))
                return any(
                    observed == descriptor and events & select.POLLIN
                    for observed, events in poller.poll(timeout)
                )

            yield wait
        finally:
            os.close(descriptor)
    elif sys.platform == "darwin":
        import select

        with closing(select.kqueue()) as queue:
            notification = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            queue.control([notification], 0, 0)
            exited = False

            def wait(deadline: float) -> bool:
                nonlocal exited
                if not exited:
                    events = queue.control(None, 1, max(0, deadline - time.monotonic()))
                    exited = any(
                        event.ident == pid
                        and event.filter == select.KQ_FILTER_PROC
                        and event.fflags & select.KQ_NOTE_EXIT
                        and not event.flags & select.KQ_EV_ERROR
                        for event in events
                    )
                return exited

            yield wait
    else:
        raise OSError("Process termination observation is not supported on this platform.")


class ProfileHandoff(QObject):
    """Release a waiting successor only after the departing runtime is shut down."""

    ready = Signal()
    prepared = Signal()
    failed = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._on_connection)
        self._socket: QLocalSocket | None = None
        self._buffer = bytearray()
        self._waiting = False
        self._preparing = False
        self._prepared = False
        self._frames: Iterator[bytes] = iter(())
        self._finished = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._fail("Replacement process did not acknowledge handoff."))

    def start(self) -> str:
        name = f"{_CHANNEL_PREFIX}-{os.getpid()}-{uuid4().hex}"
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
        expected = _PREPARED if self._preparing else _WAITING
        if not expected.startswith(self._buffer):
            self._fail("Invalid replacement process handshake.")
        elif self._buffer == expected:
            self._buffer.clear()
            self._timer.stop()
            if self._preparing:
                self._send_next_frame()
            elif not self._waiting:
                self._waiting = True
                self.ready.emit()
            else:
                self._fail("Unexpected replacement process handshake.")

    def _on_disconnected(self) -> None:
        if not self._finished:
            self._fail("Replacement process disconnected before handoff.")

    def _fail(self, message: str) -> None:
        if self._finished:
            return
        self.close()
        self.failed.emit(message)

    def prepare(self, pending_files: list[str], accepted_request_ids: list[str]) -> None:
        """Transfer bounded batches; emit prepared only after every batch is acknowledged."""
        socket = self._socket
        if (
            self._finished or not self._waiting or self._preparing
            or socket is None
            or socket.state() != QLocalSocket.LocalSocketState.ConnectedState
        ):
            self._fail("Replacement process is not ready to receive pending files.")
            return
        self._preparing = True
        self._prepared = False
        self._frames = _state_frames(pending_files, accepted_request_ids)
        self._send_next_frame()

    def _send_next_frame(self) -> None:
        try:
            payload = next(self._frames)
        except StopIteration:
            self._preparing = False
            self._prepared = True
            self.prepared.emit()
            return
        except ValueError as exc:
            self._fail(str(exc))
            return
        self._timer.start(_CONNECT_TIMEOUT_MS)
        if self._socket is None or self._socket.write(payload) != len(payload):
            self._fail("Could not transfer pending file requests.")

    def release(self) -> bool:
        socket = self._socket
        if (
            self._finished
            or not self._prepared
            or socket is None
            or socket.state() != QLocalSocket.LocalSocketState.ConnectedState
        ):
            return False
        self._finished = True
        self._timer.stop()
        payload = _RELEASED
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
    match = re.fullmatch(rf"{_CHANNEL_PREFIX}-([1-9][0-9]*)-[0-9a-f]{{32}}", name)
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
        prepared: ProfileHandoffState | None = None
        seen_files: set[str] = set()
        seen_ids: set[str] = set()
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_MS / 1000
        while True:
            reply.extend(bytes(socket.readAll().data()))
            if len(reply) > _MAX_MESSAGE_BYTES:
                return None
            if b"\n" in reply:
                if reply == _RELEASED:
                    return (prepared, deadline) if prepared is not None else None
                if not reply.startswith(_STATE) or not reply.endswith(b"\n"):
                    return None
                try:
                    state = json.loads(reply[len(_STATE) : -1])
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
                    if prepared is None:
                        prepared = ProfileHandoffState([], [])
                    for path in files:
                        if path not in seen_files:
                            seen_files.add(path)
                            prepared.pending_files.append(path)
                    for identifier in accepted:
                        if identifier not in seen_ids:
                            seen_ids.add(identifier)
                            prepared.accepted_request_ids.append(identifier)
                    if len(prepared.accepted_request_ids) > MAX_RECENT_REQUESTS:
                        return None
                    if socket.write(_PREPARED) != len(_PREPARED):
                        return None
                    if socket.bytesToWrite() and not socket.waitForBytesWritten(2000):
                        return None
                    reply.clear()
                    continue
                return None
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if remaining == 0 or socket.state() == QLocalSocket.LocalSocketState.UnconnectedState:
                return None
            socket.waitForReadyRead(remaining)
    finally:
        socket.abort()
