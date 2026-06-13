import inspect
from pathlib import Path

from solin.core.playlists import cleanup as playlist_cleanup
from solin.core.playlists import storage as playlist_storage
from solin.core.playlists.storage import (
    PlaylistStoragePaths,
    load_playlists,
    load_pending_deletions,
    save_playlists,
    save_pending_deletions,
)


def test_playlist_storage_roundtrips_playlists(tmp_path):
    playlists_file = tmp_path / "playlists.json"
    storage_paths = PlaylistStoragePaths(
        playlists_file=playlists_file,
        pending_deletions_file=tmp_path / "pending.json",
    )

    save_playlists([{"id": "p1", "name": "Playlist", "items": []}], storage_paths)

    assert load_playlists(storage_paths) == [{"id": "p1", "name": "Playlist", "items": []}]


def test_playlist_storage_roundtrips_pending_deletions(tmp_path):
    pending_file = tmp_path / "pending.json"
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=pending_file,
    )

    save_pending_deletions(["locked.mp4"], storage_paths)

    assert load_pending_deletions(storage_paths) == ["locked.mp4"]


def test_try_remove_file_queues_after_retries(monkeypatch, tmp_path):
    saved = []
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )

    monkeypatch.setattr(playlist_cleanup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        playlist_cleanup.os,
        "remove",
        lambda _path: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(
        playlist_cleanup,
        "load_pending_deletions",
        lambda _paths: [],
    )
    monkeypatch.setattr(
        playlist_cleanup,
        "save_pending_deletions",
        lambda pending, _paths: saved.append(list(pending)),
    )

    assert (
        playlist_cleanup.try_remove_file(
            "locked.mp4",
            storage_paths,
            retries=1,
            delay=0,
        )
        is False
    )
    assert saved == [["locked.mp4"]]


def test_playlist_storage_has_no_implicit_current_context():
    for function in (
        load_playlists,
        load_pending_deletions,
        save_playlists,
        save_pending_deletions,
    ):
        paths = inspect.signature(function).parameters["paths"]
        assert paths.default is inspect.Parameter.empty

    source = playlist_storage.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "ProfileManager" not in text
    assert "from_legacy_globals" not in text
    assert "def current(" not in text
