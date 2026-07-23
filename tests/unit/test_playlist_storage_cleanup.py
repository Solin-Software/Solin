import json
import inspect
from pathlib import Path
import threading
import time

import pytest

from solin.core.playlists import cleanup as playlist_cleanup
from solin.core.playlists import storage as playlist_storage
from solin.core.playlists.cleanup import (
    PlaylistCleanupQueue,
    ProfileMaintenanceCancelled,
    ProfileMaintenanceService,
)
from solin.core.foundation.runtime_paths import ProfilePaths
from solin.core.meetings.tree_store import MeetingTreeStore
from solin.core.media.thumbnail_store import ThumbnailStore
from solin.core.media.thumbnail_identity import thumbnail_storage_id
from solin.core.playlists.storage import (
    PendingDeletionRepository,
    PlaylistRepository,
    PlaylistStoragePaths,
)
from solin.core.foundation.resource_keys import ResourceClaim, file_resource_key
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.foundation.thread_workers import CancellationFlag


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


def test_bound_repositories_serialize_all_writers_with_maintenance_claim(tmp_path):
    lanes = ResourceLaneRegistry()
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    playlist_repository = PlaylistRepository.from_paths(
        storage_paths,
        resource_lanes=lanes,
    )
    meeting_store = MeetingTreeStore(
        tmp_path / "meeting_trees.json",
        resource_lanes=lanes,
    )
    pending_repository = PendingDeletionRepository.from_paths(
        storage_paths,
        resource_lanes=lanes,
    )
    claim = ResourceClaim(
        exclusive_key=file_resource_key(storage_paths.pending_deletions_file),
        shared_keys=(
            file_resource_key(storage_paths.playlists_file),
            file_resource_key(meeting_store.path),
        ),
    )
    claim_acquired = threading.Event()
    release_claim = threading.Event()

    def hold_maintenance_claim() -> None:
        lanes.run(
            claim,
            lambda: (claim_acquired.set(), release_claim.wait(timeout=2.0)),
        )

    maintenance = threading.Thread(target=hold_maintenance_claim)
    maintenance.start()
    assert claim_acquired.wait(timeout=1.0)

    completed: list[str] = []
    writer_started = [threading.Event() for _ in range(3)]

    def write(index: int, label: str, action) -> None:
        writer_started[index].set()
        action()
        completed.append(label)

    writers = (
        threading.Thread(
            target=write,
            args=(0, "playlist", lambda: playlist_repository.save_strict([])),
        ),
        threading.Thread(
            target=write,
            args=(
                1,
                "meeting",
                lambda: meeting_store.save(
                    "mwb:2026-07-20:T:20260700",
                    [],
                    "hash",
                ),
            )
        ),
        threading.Thread(
            target=write,
            args=(2, "pending", lambda: pending_repository.save_strict([])),
        ),
    )
    for writer in writers:
        writer.start()
    assert all(started.wait(timeout=1.0) for started in writer_started)
    time.sleep(0.05)
    assert completed == []

    release_claim.set()
    maintenance.join(timeout=2.0)
    for writer in writers:
        writer.join(timeout=2.0)

    assert sorted(completed) == ["meeting", "pending", "playlist"]


def test_profile_maintenance_only_deletes_files_from_pre_ui_inventory(tmp_path):
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="profile-1",
    )
    paths.ensure_dirs()
    orphan = paths.images_dir / "old-orphan.jpg"
    orphan.write_bytes(b"old")
    storage_paths = PlaylistStoragePaths(
        playlists_file=paths.playlists_file,
        pending_deletions_file=paths.pending_deletions_file,
    )
    lanes = ResourceLaneRegistry()
    playlists = PlaylistRepository.from_paths(storage_paths, resource_lanes=lanes)
    meetings = MeetingTreeStore(paths.meeting_trees_file, resource_lanes=lanes)
    service = ProfileMaintenanceService(
        storage_paths=storage_paths,
        playlist_repository=playlists,
        meeting_tree_store=meetings,
        profile_paths=paths,
        resource_lanes=lanes,
    )
    materialized_after_startup = paths.embedded_dir / "importing-video.mp4"
    materialized_after_startup.write_bytes(b"new import")

    service.run()

    assert not orphan.exists()
    assert materialized_after_startup.read_bytes() == b"new import"


def test_profile_maintenance_preserves_inventory_path_replaced_by_live_work(tmp_path):
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="profile-1",
    )
    paths.ensure_dirs()
    replaced = paths.embedded_dir / "asset.mp4"
    replaced.write_bytes(b"stale")
    storage_paths = PlaylistStoragePaths(
        playlists_file=paths.playlists_file,
        pending_deletions_file=paths.pending_deletions_file,
    )
    lanes = ResourceLaneRegistry()
    playlists = PlaylistRepository.from_paths(storage_paths, resource_lanes=lanes)
    meetings = MeetingTreeStore(paths.meeting_trees_file, resource_lanes=lanes)
    service = ProfileMaintenanceService(
        storage_paths=storage_paths,
        playlist_repository=playlists,
        meeting_tree_store=meetings,
        profile_paths=paths,
        resource_lanes=lanes,
    )
    replaced.write_bytes(b"replacement with a distinct generation")

    service.run()

    assert replaced.read_bytes() == b"replacement with a distinct generation"


def test_profile_maintenance_cancellation_prevents_any_orphan_delete(tmp_path):
    paths = ProfilePaths.from_roots(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        profile_id="profile-1",
    )
    paths.ensure_dirs()
    orphan = paths.embedded_dir / "orphan.mp4"
    orphan.write_bytes(b"must survive cancellation")
    storage_paths = PlaylistStoragePaths(
        playlists_file=paths.playlists_file,
        pending_deletions_file=paths.pending_deletions_file,
    )
    lanes = ResourceLaneRegistry()
    service = ProfileMaintenanceService(
        storage_paths=storage_paths,
        playlist_repository=PlaylistRepository.from_paths(
            storage_paths,
            resource_lanes=lanes,
        ),
        meeting_tree_store=MeetingTreeStore(
            paths.meeting_trees_file,
            resource_lanes=lanes,
        ),
        profile_paths=paths,
        resource_lanes=lanes,
    )
    cancellation = CancellationFlag()
    cancellation.set()

    with pytest.raises(ProfileMaintenanceCancelled):
        service.run(cancellation)

    assert orphan.read_bytes() == b"must survive cancellation"


def test_cancellation_waits_for_active_side_effect_and_closes_the_gate():
    cancellation = CancellationFlag()
    action_started = threading.Event()
    release_action = threading.Event()
    cancel_finished = threading.Event()

    def blocking_action() -> None:
        action_started.set()
        release_action.wait(timeout=2)

    action_thread = threading.Thread(
        target=lambda: cancellation.run_if_active(blocking_action)
    )
    action_thread.start()
    assert action_started.wait(timeout=1)
    cancel_thread = threading.Thread(
        target=lambda: (cancellation.set(), cancel_finished.set())
    )
    cancel_thread.start()

    assert not cancel_finished.wait(timeout=0.05)
    release_action.set()
    action_thread.join(timeout=2)
    cancel_thread.join(timeout=2)

    assert cancel_finished.is_set()
    assert cancellation.run_if_active(lambda: None) is False


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
    store = ThumbnailStore(thumb_dir)
    storage_id = thumbnail_storage_id(item["id"], item["url"])
    thumb_path = store.save_bytes(
        storage_id,
        b"thumb",
        source_signature="5:10",
    )
    signature_path = store.source_signature_path(storage_id)
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
    assert signature_path.exists()

    repository.save([{"id": "p1", "items": []}])
    queue.enqueue_items([item])
    queue.flush()

    assert media_path.exists()
    assert not thumb_path.exists()
    assert not signature_path.exists()


def test_cleanup_queue_fails_closed_when_playlist_storage_is_corrupt(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    thumb_dir = tmp_path / "thumbs"
    thumb_path = ThumbnailStore(thumb_dir).path("item-1")
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


def test_flush_thumbs_keeps_only_current_source_bound_identity(tmp_path):
    storage_paths = PlaylistStoragePaths(
        playlists_file=tmp_path / "playlists.json",
        pending_deletions_file=tmp_path / "pending.json",
    )
    item = {"id": "item-1", "url": str(tmp_path / "current.mp4")}
    PlaylistRepository.from_paths(storage_paths).save(
        [{"id": "p1", "items": [item]}]
    )
    store = ThumbnailStore(tmp_path / "thumbs")
    current = store.save_bytes(
        thumbnail_storage_id(item["id"], item["url"]),
        b"current",
        source_signature="7:10",
    )
    stale = store.save_bytes(
        thumbnail_storage_id(item["id"], str(tmp_path / "old.mp4")),
        b"stale",
        source_signature="8:20",
    )
    current_signature = current.with_suffix(".jpg.source")
    stale_signature = stale.with_suffix(".jpg.source")

    playlist_cleanup.flush_thumbs_dir(storage_paths, store.root)

    assert current.exists()
    assert current_signature.exists()
    assert not stale.exists()
    assert not stale_signature.exists()


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
