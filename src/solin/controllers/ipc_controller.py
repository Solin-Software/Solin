from __future__ import annotations

import os
import json
import re
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from shiboken6 import isValid

from ..core.foundation.constants import IPC_SERVER_NAME

MAX_REQUEST_BYTES = 1_048_576
MAX_RECENT_REQUESTS = 1024


def encode_open_request(paths: list[str], request_id: str) -> bytes:
    payload = (
        json.dumps({"id": request_id, "paths": paths}, ensure_ascii=True).encode("ascii") + b"\n"
    )
    if len(payload) > MAX_REQUEST_BYTES:
        raise ValueError("The file-open request is too large.")
    return payload


class IpcController:
    """Receives file-open requests from secondary app instances."""

    def __init__(
        self,
        parent: QObject,
        *,
        bring_to_front: Callable[[], None],
        open_media_files: Callable[[list[str]], None],
    ) -> None:
        self._parent = parent
        self._bring_to_front = bring_to_front
        self._open_media_files = open_media_files
        self._server: QLocalServer | None = None
        self._buffers: dict[QLocalSocket, bytearray] = {}
        self._accepted: dict[str, None] = {}
        self._in_flight: set[str] = set()

    @property
    def accepted_request_ids(self) -> list[str]:
        return list(self._accepted)

    def restore_request_ids(self, request_ids: list[str]) -> None:
        self._accepted = dict.fromkeys(request_ids[-MAX_RECENT_REQUESTS:])

    def start(self) -> None:
        if self._server is not None and self._server.isListening():
            return
        self._server = QLocalServer(self._parent)
        self._server.newConnection.connect(self._on_connection)
        if not self._server.listen(IPC_SERVER_NAME):
            raise RuntimeError(self._server.errorString())

    def close(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        server.close()
        for connection in tuple(self._buffers):
            connection.abort()
        self._buffers.clear()

    @Slot()
    def _on_connection(self) -> None:
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        self.attach_connection(conn)

    def attach_connection(self, conn: QLocalSocket) -> None:
        conn.readyRead.connect(lambda: self.receive(conn))
        conn.disconnected.connect(lambda: self.discard_connection(conn))
        conn.disconnected.connect(conn.deleteLater)
        timer = QTimer(conn)
        timer.setSingleShot(True)
        timer.timeout.connect(conn.disconnectFromServer)
        timer.start(3000)
        if conn.bytesAvailable():
            self.receive(conn)

    def discard_connection(self, conn: QLocalSocket) -> None:
        self._buffers.pop(conn, None)

    def receive(self, conn: QLocalSocket) -> None:
        raw = conn.readAll()
        if raw.isEmpty():
            return
        buffer = self._buffers.setdefault(conn, bytearray())
        buffer.extend(bytes(raw.data()))
        if len(buffer) > MAX_REQUEST_BYTES:
            self.discard_connection(conn)
            conn.abort()
            return
        if b"\n" not in buffer:
            return
        self.discard_connection(conn)
        try:
            request = json.loads(buffer)
            if not isinstance(request, dict):
                raise ValueError("Invalid file-open request")
            identifier = request["id"]
            paths = request["paths"]
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[0-9a-f]{32}", identifier)
                or not isinstance(paths, list)
                or not all(isinstance(path, str) for path in paths)
            ):
                raise ValueError("Invalid file-open request")
        except (ValueError, KeyError, TypeError, UnicodeError):
            conn.abort()
            return
        if identifier in self._in_flight:
            # Delivery may enter a modal event loop. A retry must wait for the
            # original result rather than dispatching the same import again.
            conn.disconnectFromServer()
            return
        if identifier not in self._accepted:
            self._in_flight.add(identifier)
            try:
                valid_paths = self._valid_payload_paths("\n".join(paths))
                if valid_paths:
                    self._open_media_files(valid_paths)
                self._bring_to_front()
                self._accepted[identifier] = None
                if len(self._accepted) > MAX_RECENT_REQUESTS:
                    del self._accepted[next(iter(self._accepted))]
            finally:
                self._in_flight.discard(identifier)
        # ACK proves application acceptance of the complete frame. Retried
        # identities are remembered across a handoff, independently of ACK loss.
        # A modal delivery may outlive this connection and its Qt object. The
        # accepted identity lets the next retry receive its ACK safely.
        if isValid(conn):
            conn.write(identifier.encode("ascii") + b"\n")
            conn.flush()
            conn.disconnectFromServer()

    @staticmethod
    def _valid_payload_paths(text: str) -> list[str]:
        if not text:
            return []
        return [
            path
            for path in (line.strip() for line in text.split("\n"))
            if path and (path.startswith(("http://", "https://")) or os.path.isfile(path))
        ]
