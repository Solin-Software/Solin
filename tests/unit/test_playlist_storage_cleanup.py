import json
import inspect
from pathlib import Path

from solin.core.playlists import cleanup as playlist_cleanup
from solin.core.playlists import storage as playlist_storage
from solin.core.playlists.cleanup import PlaylistCleanupQueue
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.meetings.tree_store import MeetingTreeStore
from solin.core.media.thumbnail_store import ThumbnailStore
from solin.core.playlists.storage import (
    PendingDeletionRepository,
    PlaylistRepository,
    PlaylistStoragePaths,
)
from solin.core.playlists.thumbnails import playlist_thumb_path


def test_playlist_storage_roundtrips_playlists(tmp_path):
    playlists_file = tmp_path / "playlists.json"
    storage_paths = PlaylistStoragePaths(
        playlists_file=playlists_file,
        pending_deletions_file=tmp_path / "pending.json",
    )

    repository = PlaylistRepository.from_paths(storage_paths)
    repository.save([{"id": "p1", "name": "Playlist", "items": []}])

    assert repository.load() == [{"id": "p1", "name": "Playlist", "items": []}]


def test_playlist_storage_publishes_successful_changes(tmp_path):
    repository = PlaylistRepository(tmp_path / "playlists.json")
    notifications: list[str] = []
    unsubscribe = repository.subscribe(lambda: notifications.append("changed"))

    repository.save([{"id": "p1", "name": "First", "items": []}])
    unsubscribe()
    repository.save([{"id": "p2", "name": "Second", "items": []}])

    assert notifications == ["changed"]


def test_meeting_tree_store_publishes_successful_changes(tmp_path):
    store = MeetingTreeStore(tmp_path / "meeting_trees.json")
    notifications: list[str] = []
    unsubscribe = store.subscribe(lambda: notifications.append("changed"))

    store.save("mwb:2026-07-13:T:20260700", [], "hash")
    unsubscribe()
    store.save("wt:2026-07-13:T:20260700", [], "hash")

    assert notifications == ["changed"]


def test_playlist_storage_roundtrips_pending_deletions(tmp_path):
    pending_file = tmp_path / "pending.json"
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=pending_file,
    )

    repository = PendingDeletionRepository.from_paths(storage_paths)
    repository.save(["locked.mp4"])

    assert repository.load() == ["locked.mp4"]


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
    monkeypatch.setattr(PendingDeletionRepository, "load", lambda _self: [])
    monkeypatch.setattr(
        PendingDeletionRepository,
        "save",
        lambda _self, pending: saved.append(list(pending)),
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


def test_cleanup_queue_revalidates_current_playlist_references(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    embedded_dir = tmp_path / "embedded"
    thumb_dir = tmp_path / "thumbs"
    embedded_dir.mkdir()
    media_path = embedded_dir / "clip.mp4"
    media_path.write_bytes(b"media")
    item = {"id": "item-1", "url": str(media_path)}
    thumb_path = playlist_thumb_path(
        item["id"],
        thumb_cache_dir=str(thumb_dir),
    )
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(b"thumb")
    queue = PlaylistCleanupQueue(
        storage_paths,
        ThumbnailStore(thumb_dir),
    )

    queue.enqueue_items([item])
    repository = PlaylistRepository.from_paths(storage_paths)
    repository.save([{"id": "p1", "items": [item]}])
    queue.flush()

    assert media_path.exists()
    assert thumb_path.exists()

    repository.save([{"id": "p1", "items": []}])
    queue.enqueue_items([item])
    queue.flush()

    assert media_path.exists()
    assert not thumb_path.exists()


def test_cleanup_queue_fails_closed_when_playlist_storage_is_corrupt(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    thumb_dir = tmp_path / "thumbs"
    thumb_path = playlist_thumb_path(
        "item-1",
        thumb_cache_dir=str(thumb_dir),
    )
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_path.write_bytes(b"thumb")
    storage_paths.playlists_file.write_text("{broken", encoding="utf-8")
    queue = PlaylistCleanupQueue(storage_paths, ThumbnailStore(thumb_dir))

    queue.enqueue_items([{"id": "item-1"}])
    queue.flush()

    assert thumb_path.exists()
    assert queue.pending_count == 1


def test_flush_images_dir_fails_closed_when_playlist_storage_is_corrupt(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    profile_paths = ProfilePaths.from_roots(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        profile_id="main",
    )
    profile_paths.images_dir.mkdir(parents=True)
    stale_image = profile_paths.images_dir / "stale.jpg"
    stale_image.write_bytes(b"image")
    storage_paths.playlists_file.write_text("{broken", encoding="utf-8")

    playlist_cleanup.flush_images_dir(
        storage_paths,
        MeetingTreeStore(profile_paths.meeting_trees_file),
        profile_paths,
    )

    assert stale_image.exists()


def test_flush_embedded_dir_fails_closed_when_meeting_tree_is_corrupt(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    PlaylistRepository.from_paths(storage_paths).save([])
    profile_paths = ProfilePaths.from_roots(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        profile_id="main",
    )
    profile_paths.embedded_dir.mkdir(parents=True)
    stale_media = profile_paths.embedded_dir / "stale.mp4"
    stale_media.write_bytes(b"media")
    profile_paths.meeting_trees_file.parent.mkdir(parents=True, exist_ok=True)
    profile_paths.meeting_trees_file.write_text("{broken", encoding="utf-8")

    playlist_cleanup.flush_embedded_dir(
        storage_paths,
        MeetingTreeStore(profile_paths.meeting_trees_file),
        profile_paths,
    )

    assert stale_media.exists()


def test_flush_embedded_dir_fails_closed_when_meeting_tree_shape_is_invalid(
    tmp_path,
):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    PlaylistRepository.from_paths(storage_paths).save([])
    profile_paths = ProfilePaths.from_roots(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        profile_id="main",
    )
    profile_paths.embedded_dir.mkdir(parents=True)
    stale_media = profile_paths.embedded_dir / "stale.mp4"
    stale_media.write_bytes(b"media")
    profile_paths.meeting_trees_file.parent.mkdir(parents=True, exist_ok=True)
    profile_paths.meeting_trees_file.write_text(
        json.dumps({"trees": {"week": {"nodes": "not-a-list"}}}),
        encoding="utf-8",
    )

    playlist_cleanup.flush_embedded_dir(
        storage_paths,
        MeetingTreeStore(profile_paths.meeting_trees_file),
        profile_paths,
    )

    assert stale_media.exists()


def test_flush_images_dir_fails_closed_when_playlist_shape_is_invalid(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    profile_paths = ProfilePaths.from_roots(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        profile_id="main",
    )
    profile_paths.images_dir.mkdir(parents=True)
    stale_image = profile_paths.images_dir / "stale.jpg"
    stale_image.write_bytes(b"image")
    storage_paths.playlists_file.write_text(
        json.dumps({"playlists": [{"id": "p1"}]}),
        encoding="utf-8",
    )

    playlist_cleanup.flush_images_dir(
        storage_paths,
        MeetingTreeStore(profile_paths.meeting_trees_file),
        profile_paths,
    )

    assert stale_image.exists()


def test_flush_thumbs_dir_fails_closed_when_playlist_storage_is_corrupt(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    thumb_dir = tmp_path / "thumbs"
    stale_thumb = thumb_dir / "stale.jpg"
    stale_thumb.parent.mkdir(parents=True)
    stale_thumb.write_bytes(b"thumb")
    storage_paths.playlists_file.write_text("{broken", encoding="utf-8")

    playlist_cleanup.flush_thumbs_dir(storage_paths, thumb_dir)

    assert stale_thumb.exists()


def test_flush_thumbs_dir_fails_closed_when_playlist_item_shape_is_invalid(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    thumb_dir = tmp_path / "thumbs"
    stale_thumb = thumb_dir / "stale.jpg"
    stale_thumb.parent.mkdir(parents=True)
    stale_thumb.write_bytes(b"thumb")
    storage_paths.playlists_file.write_text(
        json.dumps({"playlists": [{"id": "p1", "items": [{"id": "item-1"}]}]}),
        encoding="utf-8",
    )

    playlist_cleanup.flush_thumbs_dir(storage_paths, thumb_dir)

    assert stale_thumb.exists()


def test_flush_pdf_pages_fails_closed_when_playlist_storage_is_corrupt(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    pages_dir = tmp_path / "pdf_pages"
    stale_pages = pages_dir / "stale"
    stale_pages.mkdir(parents=True)
    (stale_pages / "page_1.jpg").write_bytes(b"page")
    storage_paths.playlists_file.write_text("{broken", encoding="utf-8")

    playlist_cleanup.flush_pdf_pages(
        storage_paths,
        MeetingTreeStore(tmp_path / "meeting_trees.json"),
        pages_dir,
    )

    assert stale_pages.exists()


def test_flush_pending_deletions_fails_closed_when_playlist_storage_is_corrupt(
    tmp_path,
):
    media_path = tmp_path / "locked.mp4"
    media_path.write_bytes(b"media")
    pending_file = tmp_path / "pending.json"
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=pending_file,
    )
    PendingDeletionRepository.from_paths(storage_paths).save([str(media_path)])
    storage_paths.playlists_file.write_text("{broken", encoding="utf-8")

    playlist_cleanup.flush_pending_deletions(
        storage_paths,
        MeetingTreeStore(tmp_path / "meeting_trees.json"),
    )

    assert media_path.exists()
    assert PendingDeletionRepository.from_paths(storage_paths).load() == [
        str(media_path)
    ]


def test_flush_pending_deletions_fails_closed_when_queue_shape_is_invalid(
    tmp_path,
):
    media_path = tmp_path / "victim.mp4"
    media_path.write_bytes(b"media")
    pending_file = tmp_path / "pending.json"
    invalid_pending = {"pending": {str(media_path): True}}
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=pending_file,
    )
    PlaylistRepository.from_paths(storage_paths).save([])
    pending_file.write_text(json.dumps(invalid_pending), encoding="utf-8")

    playlist_cleanup.flush_pending_deletions(
        storage_paths,
        MeetingTreeStore(tmp_path / "meeting_trees.json"),
    )

    assert media_path.exists()
    assert json.loads(pending_file.read_text(encoding="utf-8")) == invalid_pending


def test_try_remove_file_does_not_overwrite_invalid_pending_queue(
    monkeypatch,
    tmp_path,
):
    invalid_pending = {"pending": {"locked.mp4": True}}
    pending_file = tmp_path / "pending.json"
    pending_file.write_text(json.dumps(invalid_pending), encoding="utf-8")
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=pending_file,
    )

    monkeypatch.setattr(playlist_cleanup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(
        playlist_cleanup.os,
        "remove",
        lambda _path: (_ for _ in ()).throw(OSError()),
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
    assert json.loads(pending_file.read_text(encoding="utf-8")) == invalid_pending


def test_playlist_repositories_have_no_implicit_current_context():
    for repository_class in (PlaylistRepository, PendingDeletionRepository):
        path = inspect.signature(repository_class).parameters["path"]
        assert path.default is inspect.Parameter.empty

    source = playlist_storage.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    assert "ProfileManager" not in text
    assert "from_legacy_globals" not in text
    assert "def current(" not in text
    assert "def load_playlists(" not in text
    assert "def save_playlists(" not in text
