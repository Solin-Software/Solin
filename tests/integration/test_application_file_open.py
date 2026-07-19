from __future__ import annotations

import os

from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication

from solin.bootstrap.file_open import (
    ApplicationFileOpenRouter,
    dispatch_pending_files,
)


_APP = QApplication.instance() or QApplication([])


class _Window:
    def __init__(self) -> None:
        self.opened: list[list[str]] = []

    def open_media_files(self, paths: list[str]) -> None:
        self.opened.append(paths)


def test_file_open_event_is_queued_until_profile_window_exists(tmp_path) -> None:
    playlist = tmp_path / "portable.solinplaylist"
    playlist.write_bytes(b"package")
    pending: list[str] = []
    window_ref: list[object | None] = [None]
    router = ApplicationFileOpenRouter(_APP, window_ref, pending)

    try:
        assert router.eventFilter(_APP, QFileOpenEvent(os.fspath(playlist)))
        assert pending == [os.fspath(playlist)]

        window = _Window()
        window_ref[0] = window
        dispatch_pending_files(window, pending)

        assert pending == []
        assert window.opened == [[os.fspath(playlist)]]
    finally:
        router.close()


def test_startup_event_is_deduplicated_but_file_can_be_reopened(tmp_path) -> None:
    playlist = tmp_path / "portable.solinplaylist"
    playlist.write_bytes(b"package")
    path = os.fspath(playlist)
    pending = [path]
    window = _Window()
    router = ApplicationFileOpenRouter(_APP, [window], pending)

    try:
        assert router.eventFilter(_APP, QFileOpenEvent(path))
        _APP.processEvents()
        assert window.opened == []

        dispatch_pending_files(window, pending)
        assert window.opened == [[path]]

        assert router.eventFilter(_APP, QFileOpenEvent(path))
        _APP.processEvents()
        assert window.opened == [[path], [path]]
    finally:
        router.close()
