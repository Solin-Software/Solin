from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..core.foundation.constants import IPC_SERVER_NAME


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

    def start(self) -> None:
        # Remove a stale socket left by a previous run (Linux/macOS).
        QLocalServer.removeServer(IPC_SERVER_NAME)
        self._server = QLocalServer(self._parent)
        self._server.newConnection.connect(self._on_connection)
        self._server.listen(IPC_SERVER_NAME)

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
        QLocalServer.removeServer(IPC_SERVER_NAME)

    @Slot()
    def _on_connection(self) -> None:
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda: self._on_data(conn))
        QTimer.singleShot(3000, lambda: conn.disconnectFromServer())

    def _on_data(self, conn: QLocalSocket) -> None:
        raw = conn.readAll()
        if raw.isEmpty():
            return

        self._bring_to_front()
        paths = self._valid_payload_paths(raw.data().decode("utf-8").strip())
        if paths:
            self._open_media_files(paths)

    @staticmethod
    def _valid_payload_paths(text: str) -> list[str]:
        if not text:
            return []
        return [
            path
            for path in (line.strip() for line in text.split("\n"))
            if path
            and (
                path.startswith(("http://", "https://"))
                or os.path.isfile(path)
            )
        ]
