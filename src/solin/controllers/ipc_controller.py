from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ..core.foundation.constants import IPC_SERVER_NAME

if TYPE_CHECKING:
    from solin.main_window import MainWindow


class IpcController:
    """Receives file-open requests from secondary app instances."""

    def __init__(self, window: MainWindow) -> None:
        self._window = window
        self._server: QLocalServer | None = None

    def start(self) -> None:
        # Remove um socket residual de uma execução anterior (Linux/macOS).
        QLocalServer.removeServer(IPC_SERVER_NAME)
        self._server = QLocalServer(self._window)
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

        self._window._bring_to_front()
        paths = self._valid_payload_paths(raw.data().decode("utf-8").strip())
        if paths:
            self._window.open_media_files(paths)

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
