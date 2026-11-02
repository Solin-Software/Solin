"""Route operating-system open-document events into the active Solin window."""

from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QEvent, QObject, QTimer


class ApplicationFileOpenRouter(QObject):
    """Queue macOS ``QFileOpenEvent`` paths until a profile window is ready."""

    def __init__(self, app, window_ref: list, pending_files: list[str]) -> None:
        super().__init__()
        self._app = app
        self._window_ref = window_ref
        self._pending_files = pending_files
        self._closed = False
        app.installEventFilter(self)

    def close(self) -> None:
        self._closed = True
        self._app.removeEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        del watched
        if event.type() != QEvent.Type.FileOpen:
            return False

        file_event: Any = event
        path = os.fspath(file_event.file()).strip()
        if not path:
            url = file_event.url()
            path = os.fspath(url.toLocalFile()).strip() if url.isLocalFile() else ""
        if path and os.path.isfile(path):
            self._route(path)
        return True

    def _route(self, path: str) -> None:
        if path in self._pending_files:
            return
        window = self._window_ref[0] if self._window_ref else None
        open_media_files = getattr(window, "open_media_files", None)
        # Capture before deferring delivery so a transition can snapshot the file
        # even when its callback has not run yet.
        self._pending_files.append(path)
        if callable(open_media_files):
            QTimer.singleShot(0, self, lambda: self._dispatch(window))

    def _dispatch(self, window) -> None:
        if not self._closed:
            dispatch_pending_files(window, self._pending_files)


def dispatch_pending_files(window, pending_files: list[str]) -> None:
    """Drain startup paths once, allowing the same file to be reopened later."""
    if not pending_files:
        return
    paths = list(pending_files)
    pending_files.clear()
    window.open_media_files(paths)


__all__ = ["ApplicationFileOpenRouter", "dispatch_pending_files"]
