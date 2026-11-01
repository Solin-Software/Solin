from __future__ import annotations

import pytest

from solin.core.foundation.resource_keys import child_folder_resource_claim
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.ingest import watched_folder_playlists
from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore


def test_watched_folder_playlist_store_delegates_manifest_operations(monkeypatch):
    calls = []
    store = WatchedFolderPlaylistStore()
    playlist = {"items": [{"id": "1"}]}
    item = {"id": "1"}
    remaining = [{"id": "2"}]

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
        lambda folder, removed, *, remaining_items: calls.append(
            ("remove", folder, removed, remaining_items)
        )
        or True,
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
    assert store.remove_item("folder", item, remaining_items=remaining) is True
    assert store.pending_files("folder") == ["clip.mp4"]
    assert store.file_availability_signature(["clip.mp4"]) == (("clip.mp4", True),)
    assert calls == [
        ("scan", "folder"),
        ("load", "folder"),
        ("save", "folder", playlist),
        ("remove", "folder", item, tuple(remaining)),
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
            resource_lanes,
            resource_claim,
            parent,
        ):
            calls.append(
                (
                    folder_path,
                    media_lang,
                    fallback_lang_code,
                    resource_lanes,
                    resource_claim,
                    parent,
                )
            )

    monkeypatch.setattr(watched_folder_playlists, "WatchedFolderSyncThread", _Thread)

    resource_lanes = ResourceLaneRegistry()
    thread = WatchedFolderPlaylistStore(resource_lanes).create_sync_thread(
        "folder",
        media_lang="E",
        fallback_lang_code="T",
        parent=parent,
    )

    assert isinstance(thread, _Thread)
    assert calls == [
        (
            "folder",
            "E",
            "T",
            resource_lanes,
            child_folder_resource_claim("folder"),
            parent,
        )
    ]


def test_load_all_playlists_keeps_catalog_on_the_shared_registry(monkeypatch):
    calls = []
    root = "watched"
    store = WatchedFolderPlaylistStore(ResourceLaneRegistry())
    monkeypatch.setattr(
        watched_folder_playlists,
        "scan_root",
        lambda folder: calls.append(("scan", folder))
        or [{"path": "watched/one"}, {"path": "watched/two"}],
    )
    monkeypatch.setattr(
        watched_folder_playlists,
        "load_manifest_playlist",
        lambda folder: calls.append(("load", folder)) or {"path": folder},
    )

    assert store.load_all_playlists(root) == [
        {"path": "watched/one"},
        {"path": "watched/two"},
    ]
    assert calls == [
        ("scan", root),
        ("load", "watched/one"),
        ("load", "watched/two"),
    ]


def test_resource_registry_cannot_change_after_store_use(monkeypatch):
    store = WatchedFolderPlaylistStore()
    monkeypatch.setattr(watched_folder_playlists, "scan_root", lambda _folder: [])
    store.scan_root("watched")

    with pytest.raises(RuntimeError):
        store.bind_resource_lanes(ResourceLaneRegistry())


def test_linked_playlist_rename_cannot_reclassify_as_meeting(tmp_path):
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore

    folder = tmp_path / "Playlist"
    folder.mkdir()
    with pytest.raises(ValueError):
        WatchedFolderPlaylistStore().rename_folder(
            str(folder), "2026-10-21 MW", WatchedFolderFileStore()
        )

    assert folder.is_dir()


def test_linked_playlist_creation_stays_empty_even_after_discovery(tmp_path, monkeypatch):
    from pathlib import Path
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore

    store = WatchedFolderPlaylistStore()
    notifications = []
    store.subscribe(lambda: notifications.append(True))
    # Creation must not construct a replica, publish a manifest, or enable sync.
    with monkeypatch.context() as patch:
        patch.setattr(
            watched_folder_playlists, "playlist_sync",
            lambda *_args: pytest.fail("Creation must not initialize playlist sync"),
        )
        result = Path(store.create_folder(str(tmp_path), "Reunia\u0303o", WatchedFolderFileStore()))

    assert result.name == "Reunião"
    assert result.is_dir()
    assert list(result.iterdir()) == []
    assert notifications == [True]
    entries = store.scan_root(str(tmp_path))
    assert len(entries) == 1
    assert entries[0]["name"] == "Reunião"
    assert entries[0]["item_count"] == 0
    assert list(result.iterdir()) == []


@pytest.mark.parametrize("name", ["2026-10-21 MW", "2026-10-25 we", "2026-10-25\u2003WE", "../escape", "A/B", "COM¹.txt", ".solin_sync", " name", "name."])
def test_linked_playlist_creation_validates_before_any_mutation(tmp_path, name):
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore

    store = WatchedFolderPlaylistStore()
    notifications = []
    store.subscribe(lambda: notifications.append(True))

    with pytest.raises(ValueError):
        store.create_folder(str(tmp_path), name, WatchedFolderFileStore())

    assert list(tmp_path.iterdir()) == []
    assert notifications == []


def test_linked_folder_creation_requires_existing_accessible_root(tmp_path):
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore

    root = tmp_path / "unavailable"
    with pytest.raises(FileNotFoundError):
        WatchedFolderPlaylistStore().create_folder(str(root), "Playlist", WatchedFolderFileStore())

    assert not root.exists()


def test_concurrent_portable_collisions_are_serialized_at_root(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from solin.core.foundation.folder_names import FolderNameError
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore

    store = WatchedFolderPlaylistStore()
    files = WatchedFolderFileStore()

    def create(name):
        try:
            return store.create_folder(str(tmp_path), name, files)
        except FolderNameError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create, "Playlist")
        second = executor.submit(create, "PLAYLIST")
        outcomes = [first.result(timeout=5), second.result(timeout=5)]

    assert sum(result is not None for result in outcomes) == 1
    assert len(list(tmp_path.iterdir())) == 1


def test_rename_failure_can_restore_an_existing_legacy_name(tmp_path, monkeypatch):
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
    from solin.core.playlists.linked_folder import playlist_sync

    folder = tmp_path / ".legacy"
    folder.mkdir()
    service = playlist_sync(folder)

    def fail_rebind(_destination):
        raise PermissionError("binding unavailable")

    monkeypatch.setattr(service, "rebind_folder", fail_rebind)
    with pytest.raises(PermissionError, match="binding unavailable"):
        WatchedFolderPlaylistStore().rename_folder(str(folder), "Visible", WatchedFolderFileStore())

    assert folder.is_dir()
    assert not (tmp_path / "Visible").exists()
