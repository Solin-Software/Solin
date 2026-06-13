from solin.widgets.playlist import cleanup as playlist_cleanup
from solin.widgets.playlist.storage import (
    _load_playlists,
    _save_playlists,
    load_pending_deletions,
    save_pending_deletions,
)


def test_playlist_storage_roundtrips_playlists(tmp_path, monkeypatch):
    playlists_file = tmp_path / "playlists.json"
    monkeypatch.setattr("solin.widgets.playlist.storage._paths.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "solin.widgets.playlist.storage._paths.PLAYLISTS_FILE",
        str(playlists_file),
    )

    _save_playlists([{"id": "p1", "name": "Playlist", "items": []}])

    assert _load_playlists() == [{"id": "p1", "name": "Playlist", "items": []}]


def test_playlist_storage_roundtrips_pending_deletions(tmp_path, monkeypatch):
    pending_file = tmp_path / "pending.json"
    monkeypatch.setattr("solin.widgets.playlist.storage._paths.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "solin.widgets.playlist.storage._paths.PENDING_DEL_FILE",
        str(pending_file),
    )

    save_pending_deletions(["locked.mp4"])

    assert load_pending_deletions() == ["locked.mp4"]


def test_try_remove_file_queues_after_retries(monkeypatch):
    saved = []

    monkeypatch.setattr(playlist_cleanup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        playlist_cleanup.os,
        "remove",
        lambda _path: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(playlist_cleanup, "load_pending_deletions", lambda: [])
    monkeypatch.setattr(
        playlist_cleanup,
        "save_pending_deletions",
        lambda pending: saved.append(list(pending)),
    )

    assert playlist_cleanup._try_remove_file("locked.mp4", retries=1, delay=0) is False
    assert saved == [["locked.mp4"]]
