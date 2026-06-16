from __future__ import annotations

from solin.core.ingest import watched_folder_playlists
from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore


def test_watched_folder_playlist_store_delegates_manifest_operations(monkeypatch):
    calls = []
    store = WatchedFolderPlaylistStore()
    playlist = {"items": [{"id": "1"}]}
    item = {"id": "1"}

    monkeypatch.setattr(
        watched_folder_playlists,
        "scan_root",
        lambda folder: calls.append(("scan", folder)) or [{"path": folder}],
    )
    monkeypatch.setattr(
        watched_folder_playlists,
        "load_manifest_playlist",
        lambda folder: calls.append(("load", folder)) or playlist,
    )
    monkeypatch.setattr(
        watched_folder_playlists,
        "save_manifest_playlist",
        lambda folder, payload: calls.append(("save", folder, payload)),
    )
    monkeypatch.setattr(
        watched_folder_playlists,
        "remove_item_from_manifest",
        lambda folder, removed: calls.append(("remove", folder, removed)) or True,
    )
    monkeypatch.setattr(
        watched_folder_playlists,
        "get_pending_files",
        lambda folder: calls.append(("pending", folder)) or ["clip.mp4"],
    )
    monkeypatch.setattr(
        watched_folder_playlists,
        "local_file_availability_signature",
        lambda urls: calls.append(("availability", tuple(urls)))
        or (("clip.mp4", True),),
    )

    assert store.scan_root("folder") == [{"path": "folder"}]
    assert store.load_playlist("folder") is playlist
    store.save_playlist("folder", playlist)
    assert store.remove_item("folder", item) is True
    assert store.pending_files("folder") == ["clip.mp4"]
    assert store.file_availability_signature(["clip.mp4"]) == (("clip.mp4", True),)
    assert calls == [
        ("scan", "folder"),
        ("load", "folder"),
        ("save", "folder", playlist),
        ("remove", "folder", item),
        ("pending", "folder"),
        ("availability", ("clip.mp4",)),
    ]


def test_watched_folder_playlist_store_creates_sync_thread(monkeypatch):
    calls = []
    parent = object()

    class _Thread:
        def __init__(
            self,
            folder_path,
            *,
            media_lang,
            fallback_lang_code,
            parent,
        ):
            calls.append(
                (
                    folder_path,
                    media_lang,
                    fallback_lang_code,
                    parent,
                )
            )

    monkeypatch.setattr(watched_folder_playlists, "WatchedFolderSyncThread", _Thread)

    thread = WatchedFolderPlaylistStore().create_sync_thread(
        "folder",
        media_lang="E",
        fallback_lang_code="T",
        parent=parent,
    )

    assert isinstance(thread, _Thread)
    assert calls == [("folder", "E", "T", parent)]
