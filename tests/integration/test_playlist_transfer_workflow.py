from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QThread, qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QWidget

from solin.controllers.playlist_transfer_controller import (
    PlaylistTransferController,
    PlaylistTransferJob,
    PlaylistTransferProgress,
)
from solin.controllers.playlist_transfer_workflow import (
    PlaylistTransferWorkflow,
    _items_with_persisted_thumbnails,
)
from solin.core.media.thumbnail_identity import thumbnail_storage_id
from solin.core.media.thumbnail_store import ThumbnailStore
from solin.core.media.profile_store import ProfileMediaStore
from solin.core.playlists.jwl_export import (
    JwlPlaylistExportRequest,
    export_jwlplaylist_document,
)
from solin.core.playlists.native import (
    NativePlaylistExportRequest,
    export_native_playlist,
)
from solin.core.playlists.storage import PlaylistRepository
from solin.ui.dialogs.playlist_transfer import PlaylistTransferDialog


_APP = QApplication.instance() or QApplication([])


class _Notifications:
    def __init__(self) -> None:
        self.successes: list[str] = []
        self.information_messages: list[str] = []
        self.warnings: list[str] = []

    def success(self, message: str) -> None:
        self.successes.append(message)

    def information(self, message: str) -> None:
        self.information_messages.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def _drain_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.002)
    _APP.processEvents()
    assert predicate()


def test_transfer_dialog_scales_large_byte_totals_without_integer_overflow() -> None:
    dialog = PlaylistTransferDialog()
    dialog.begin("Export", "Embedding")

    assert (
        "QDialog#PlaylistTransferDialog QLabel{background:transparent;}"
        in dialog.styleSheet()
    )

    dialog.set_progress(
        stage="Embedding",
        completed=3 * 1024**3,
        total=6 * 1024**3,
    )

    assert dialog._progress.value() == 500
    dialog.finish()
    dialog.deleteLater()


def test_transfer_dialog_stylesheet_is_accepted_by_qt() -> None:
    messages: list[str] = []

    def capture_message(_mode, _context, message: str) -> None:
        messages.append(message)

    previous_handler = qInstallMessageHandler(capture_message)
    try:
        dialog = PlaylistTransferDialog()
        dialog.ensurePolished()
    finally:
        qInstallMessageHandler(previous_handler)

    assert not any("Could not parse stylesheet" in message for message in messages)
    dialog.deleteLater()


def test_jwl_export_enrichment_uses_only_source_bound_persisted_thumbnail(
    tmp_path: Path,
) -> None:
    store = ThumbnailStore(tmp_path / "thumbs")
    current_source = os.fspath(tmp_path / "current.mp4")
    stale_source = os.fspath(tmp_path / "stale.mp4")
    store.save_bytes(
        thumbnail_storage_id("media-1", current_source),
        b"current-thumbnail",
    )

    current, stale = _items_with_persisted_thumbnails(
        [
            {"id": "media-1", "url": current_source},
            {"id": "media-1", "url": stale_source},
        ],
        store.root,
        lambda: False,
    )

    assert current["thumbnail_data"] == b"current-thumbnail"
    assert "thumbnail_data" not in stale


def test_transfer_controller_commits_on_ui_thread_without_blocking() -> None:
    parent = QWidget()
    notifications = _Notifications()
    controller = PlaylistTransferController(
        parent=parent,
        notifications=notifications,
    )
    ui_thread = _APP.thread()
    observations: list[tuple[str, object]] = []

    def runner(progress, _cancellation):
        progress(PlaylistTransferProgress("Working", completed=1, total=2))
        observations.append(("worker", QThread.currentThread() == ui_thread))
        return "done"

    def completed(result):
        observations.append(("commit", (result, parent.thread() == ui_thread)))

    assert controller.submit(
        PlaylistTransferJob(
            key="thread-affinity",
            title="Transfer",
            initial_stage="Starting",
            runner=runner,
            completed=completed,
            succeeded_message="Complete",
        )
    )

    _drain_until(lambda: not controller.is_busy)

    assert observations[0] == ("worker", False)
    assert observations[-1] == ("commit", ("done", True))
    assert notifications.successes == ["Complete"]
    controller.shutdown()
    parent.deleteLater()


def test_transfer_controller_cancels_cooperatively() -> None:
    parent = QWidget()
    notifications = _Notifications()
    controller = PlaylistTransferController(
        parent=parent,
        notifications=notifications,
    )
    committed: list[object] = []

    def runner(_progress, cancellation):
        while not cancellation.is_set():
            time.sleep(0.002)
        return "cancelled"

    assert controller.submit(
        PlaylistTransferJob(
            key="cancel",
            title="Transfer",
            initial_stage="Starting",
            runner=runner,
            completed=committed.append,
        )
    )
    controller.cancel_active()
    _drain_until(lambda: not controller.is_busy)

    assert committed == []
    assert notifications.information_messages == ["Operation cancelled"]
    controller.shutdown()
    parent.deleteLater()


def test_native_workflow_imports_persists_and_opens_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"portable-media")
    package = tmp_path / "filename-must-not-become-the-playlist-name.solinplaylist"
    export_native_playlist(
        NativePlaylistExportRequest(
            playlist={
                "id": "source-playlist",
                "name": "Portable",
                "items": [
                    {
                        "id": "source-item",
                        "title": "Media",
                        "url": os.fspath(source),
                        "type": "video",
                    }
                ],
            },
            output_path=package,
            media_cache_dir=tmp_path / "export-cache",
            thumbnail_cache_dir=tmp_path / "export-thumbs",
        )
    )

    parent = QWidget()
    notifications = _Notifications()
    playlists: list[dict] = []
    repository = PlaylistRepository(tmp_path / "profile" / "playlists.json")
    embedded_dir = tmp_path / "profile" / "embedded"
    images_dir = tmp_path / "profile" / "images"
    profile_paths = SimpleNamespace(
        embedded_dir=embedded_dir,
        thumb_cache_dir=tmp_path / "profile-cache" / "thumbs",
    )
    opened: list[str] = []
    refreshed: list[bool] = []
    workflow = PlaylistTransferWorkflow(
        parent=parent,
        notifications=notifications,
        language_manager=SimpleNamespace(api_code="E"),
        playlists=playlists,
        playlist_repository=repository,
        profile_paths=profile_paths,
        profile_media_store=ProfileMediaStore(embedded_dir, images_dir),
        media_cache_manager=SimpleNamespace(media_cache_dir=tmp_path / "cache"),
        refresh_playlists=lambda: refreshed.append(True),
        open_playlist=opened.append,
    )

    workflow.import_playlists([os.fspath(package)], "solin", open_after=True)
    _drain_until(lambda: not workflow.is_busy)

    assert len(playlists) == 1
    assert playlists[0]["name"] == "Portable"
    assert playlists[0]["id"] != "source-playlist"
    assert Path(playlists[0]["items"][0]["url"]).read_bytes() == b"portable-media"
    assert repository.load_strict() == playlists
    assert refreshed == [True]
    assert opened == [playlists[0]["id"]]
    assert notifications.successes == ["1 playlist imported"]
    workflow.shutdown()
    parent.deleteLater()


def test_native_workflow_rejects_existing_playlist_name_before_extracting_media(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"portable-media")
    package = tmp_path / "different-filename.solinplaylist"
    export_native_playlist(
        NativePlaylistExportRequest(
            playlist={
                "id": "source-playlist",
                "name": " ＭＥＥＴＩＮＧ ",
                "items": [
                    {
                        "id": "source-item",
                        "title": "Media",
                        "url": os.fspath(source),
                        "type": "video",
                    }
                ],
            },
            output_path=package,
            media_cache_dir=tmp_path / "export-cache",
            thumbnail_cache_dir=tmp_path / "export-thumbs",
        )
    )

    parent = QWidget()
    notifications = _Notifications()
    playlists = [{"id": "existing", "name": "Meeting", "items": []}]
    repository = PlaylistRepository(tmp_path / "profile" / "playlists.json")
    repository.save_strict(playlists)
    embedded_dir = tmp_path / "profile" / "embedded"
    workflow = PlaylistTransferWorkflow(
        parent=parent,
        notifications=notifications,
        language_manager=SimpleNamespace(api_code="E"),
        playlists=playlists,
        playlist_repository=repository,
        profile_paths=SimpleNamespace(
            embedded_dir=embedded_dir,
            thumb_cache_dir=tmp_path / "profile-cache" / "thumbs",
        ),
        profile_media_store=ProfileMediaStore(
            embedded_dir,
            tmp_path / "profile" / "images",
        ),
        media_cache_manager=SimpleNamespace(media_cache_dir=tmp_path / "cache"),
        refresh_playlists=lambda: None,
        open_playlist=lambda _playlist_id: None,
    )

    workflow.import_playlists([os.fspath(package)], "solin")
    _drain_until(lambda: not workflow.is_busy)

    assert playlists == [{"id": "existing", "name": "Meeting", "items": []}]
    assert repository.load_strict() == playlists
    assert not embedded_dir.exists() or not any(embedded_dir.rglob("*"))
    assert notifications.successes == []
    workflow.shutdown()
    parent.deleteLater()


def test_jwl_workflow_also_rejects_an_existing_playlist_name(tmp_path: Path) -> None:
    # The established JWL import contract deliberately derives its name from
    # the file stem; native Solin packages use manifest.playlist.name instead.
    package = tmp_path / "ＭＥＥＴＩＮＧ.jwlplaylist"
    export_jwlplaylist_document(
        JwlPlaylistExportRequest(
            name=" ＭＥＥＴＩＮＧ ",
            items=[],
            output_path=package,
            media_cache_dir=tmp_path / "export-cache",
        )
    )

    parent = QWidget()
    notifications = _Notifications()
    playlists = [{"id": "existing", "name": "Meeting", "items": []}]
    repository = PlaylistRepository(tmp_path / "profile" / "playlists.json")
    repository.save_strict(playlists)
    embedded_dir = tmp_path / "profile" / "embedded"
    workflow = PlaylistTransferWorkflow(
        parent=parent,
        notifications=notifications,
        language_manager=SimpleNamespace(api_code="E"),
        playlists=playlists,
        playlist_repository=repository,
        profile_paths=SimpleNamespace(
            embedded_dir=embedded_dir,
            thumb_cache_dir=tmp_path / "profile-cache" / "thumbs",
        ),
        profile_media_store=ProfileMediaStore(
            embedded_dir,
            tmp_path / "profile" / "images",
        ),
        media_cache_manager=SimpleNamespace(media_cache_dir=tmp_path / "cache"),
        refresh_playlists=lambda: None,
        open_playlist=lambda _playlist_id: None,
    )

    workflow.import_playlists([os.fspath(package)], "jwl")
    _drain_until(lambda: not workflow.is_busy)

    assert playlists == [{"id": "existing", "name": "Meeting", "items": []}]
    assert repository.load_strict() == playlists
    assert notifications.successes == []
    workflow.shutdown()
    parent.deleteLater()
