"""Cross-replica regressions for the linked playlist protocol."""

from __future__ import annotations

import copy
from pathlib import Path
import shutil

import pytest

from solin.core.ingest.manifest import MANIFEST_REPOSITORY
from solin.core.ingest.watched_folder import (
    load_manifest_playlist,
    remove_item_from_manifest,
    save_manifest_playlist,
    stage_manifest_playlist,
    WatchedFolderWatcher,
)


def test_stale_playlist_edit_cannot_resurrect_deleted_occurrence(tmp_path):
    image = tmp_path / "slide.jpg"
    image.write_bytes(b"image")
    save_manifest_playlist(str(tmp_path), {"items": [
        {"id": "slide", "url": str(image), "type": "image", "title": "Slide"},
    ]})
    stale = copy.deepcopy(load_manifest_playlist(str(tmp_path)))
    remove_item_from_manifest(str(tmp_path), stale["items"][0])
    stale["sections"] = [{"id": "new-section", "title": "Other edit"}]
    save_manifest_playlist(str(tmp_path), stale)
    loaded = load_manifest_playlist(str(tmp_path))
    assert loaded["items"] == []
    assert [section["id"] for section in loaded["sections"]] == ["new-section"]


def test_metadata_before_source_preserves_referenced_converted_output(tmp_path):
    cache = tmp_path / ".solin_cache"
    cache.mkdir()
    page = cache / "page.jpg"
    page.write_bytes(b"page")

    def seed(manifest):
        manifest["processed"] = {"source.pdf": {"type": "pdf", "outputs": [page.name]}}
        manifest["playlist"] = {"items": [
            {"id": "page", "url": ".solin_cache/page.jpg", "type": "image"},
        ]}

    MANIFEST_REPOSITORY.update(tmp_path, seed)
    loaded = load_manifest_playlist(str(tmp_path))
    assert len(loaded["items"]) == 1
    assert page.read_bytes() == b"page"


def test_failed_physical_removal_does_not_reimport_item(tmp_path, monkeypatch):
    image = tmp_path / "slide.jpg"
    image.write_bytes(b"image")
    save_manifest_playlist(str(tmp_path), {"items": [
        {"id": "slide", "url": str(image), "type": "image"},
    ]})
    item = load_manifest_playlist(str(tmp_path))["items"][0]
    original = Path.unlink

    def locked(path, *args, **kwargs):
        if path == image:
            raise PermissionError("cloud provider holds the file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    remove_item_from_manifest(str(tmp_path), item)
    assert load_manifest_playlist(str(tmp_path))["items"] == []


def test_local_staging_preserves_successive_edits_including_revert(tmp_path):
    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    save_manifest_playlist(str(tmp_path), {"items": [
        {"id": "image", "url": str(image), "title": "Original", "type": "image"},
    ]})
    view = load_manifest_playlist(str(tmp_path))
    view["items"][0]["title"] = "Edited"
    stage_manifest_playlist(str(tmp_path), view)
    first = copy.deepcopy(view)
    save_manifest_playlist(str(tmp_path), first)
    view["items"][0]["title"] = "Original"
    stage_manifest_playlist(str(tmp_path), view)
    save_manifest_playlist(str(tmp_path), view)
    assert load_manifest_playlist(str(tmp_path))["items"][0]["title"] == "Original"


def test_external_copy_does_not_prevent_staging_other_fields(tmp_path):
    folder = tmp_path / "linked"
    folder.mkdir()
    (folder / "image.jpg").write_bytes(b"image")
    external = tmp_path / "external.jpg"
    external.write_bytes(b"external")
    view = load_manifest_playlist(str(folder))
    original = view["items"][0]["title"]
    view["items"][0]["title"] = "Edited"
    view["items"].append({"id": "external", "url": str(external), "type": "image"})
    stage_manifest_playlist(str(folder), view)
    save_manifest_playlist(str(folder), copy.deepcopy(view))
    view["items"][0]["title"] = original
    stage_manifest_playlist(str(folder), view)
    save_manifest_playlist(str(folder), copy.deepcopy(view))
    loaded = load_manifest_playlist(str(folder))
    assert loaded["items"][0]["title"] == original
    assert len(loaded["items"]) == 2


def test_staged_edits_replay_after_adapter_restart(tmp_path):
    from solin.core.playlists import linked_folder

    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    view = load_manifest_playlist(str(tmp_path))
    view["items"][0]["title"] = "Durable title"
    stage_manifest_playlist(str(tmp_path), view)
    linked_folder._SERVICES.clear()
    reloaded = load_manifest_playlist(str(tmp_path))
    assert reloaded["items"][0]["title"] == "Durable title"


def test_journal_arrival_wakes_the_owning_linked_folder(tmp_path):
    folder = tmp_path / "playlist"
    operations = folder / ".solin_sync" / "playlist" / "operations"
    operations.mkdir(parents=True)
    watcher = WatchedFolderWatcher()
    watcher.set_root(str(tmp_path))
    events = []
    watcher.subfolder_changed.connect(events.append)
    assert str(operations) in watcher._watcher.directories()
    watcher._on_dir_changed(str(operations))
    assert events == [str(folder)]


def test_late_recovery_bytes_wake_the_owning_linked_folder(tmp_path):
    folder = tmp_path / "playlist"
    archive = folder / ".solin_sync" / "resources" / ("a" * 64)
    archive.mkdir(parents=True)
    watcher = WatchedFolderWatcher()
    watcher.set_root(str(tmp_path))
    events = []
    watcher.subfolder_changed.connect(events.append)
    assert str(archive) in watcher._watcher.directories()
    watcher._on_dir_changed(str(archive))
    assert events == [str(folder)]


def test_refresh_without_changes_does_not_publish_operations(tmp_path):
    (tmp_path / "image.jpg").write_bytes(b"image")
    assert not load_manifest_playlist(str(tmp_path))["__sync_pending"]
    operations = tmp_path / ".solin_sync" / "playlist" / "operations"
    before = {path.name: path.stat().st_mtime_ns for path in operations.glob("*.json")}
    for _ in range(4):
        load_manifest_playlist(str(tmp_path))
    after = {path.name: path.stat().st_mtime_ns for path in operations.glob("*.json")}
    assert after == before


def test_retrying_same_save_does_not_delete_unseen_remote_insert(tmp_path):
    """The worker's causal baseline must describe the view it actually saved."""
    from solin.core.playlists.linked_folder import LinkedPlaylistSync, STATE

    first = LinkedPlaylistSync(tmp_path, state_dir=tmp_path / "state-a")
    second = LinkedPlaylistSync(tmp_path, state_dir=tmp_path / "state-b")
    initial = first.save({"items": []})
    stale = {"items": [], STATE: initial.to_dict()}
    second.save({"items": [{"id": "remote", "url": "https://example.org/v.mp4",
                             "type": "video"}], STATE: second.read().to_dict()})
    stale["sections"] = [{"id": "local", "title": "Local section"}]

    first.save(stale)
    first.save(stale)

    view = first.manifest(first.read())["playlist"]
    assert [item["id"] for item in view["items"]] == ["remote"]


def test_blocked_publication_keeps_retryable_request_until_cloud_accepts(tmp_path, monkeypatch):
    from solin.core.ingest.sync import journal
    from solin.core.ingest.manifest import ManifestWriteError
    from solin.core.playlists.linked_folder import playlist_sync

    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    view = load_manifest_playlist(str(tmp_path))
    view["items"][0]["title"] = "Pending"
    stage_manifest_playlist(str(tmp_path), view)
    original = journal._publish

    def blocked(path, operation):
        if path.is_relative_to(tmp_path / ".solin_sync"):
            raise PermissionError("provider locked journal")
        return original(path, operation)

    monkeypatch.setattr(journal, "_publish", blocked)
    with pytest.raises(ManifestWriteError) as error:
        save_manifest_playlist(str(tmp_path), view)
    assert error.value.retryable
    assert playlist_sync(tmp_path).replica.pending_count > 0

    monkeypatch.setattr(journal, "_publish", original)
    save_manifest_playlist(str(tmp_path), view)
    assert playlist_sync(tmp_path).replica.pending_count == 0
    assert load_manifest_playlist(str(tmp_path))["items"][0]["title"] == "Pending"


def test_source_update_uses_full_resource_and_removes_only_obsolete_generated_pages(tmp_path):
    from solin.core.playlists.linked_folder import playlist_sync

    cache = tmp_path / ".solin_cache"
    cache.mkdir()
    for path in (cache / "p1.jpg", cache / "p2.jpg", tmp_path / "p1.jpg"):
        path.write_bytes(path.name.encode())
    service = playlist_sync(tmp_path)
    with service.lock:
        service.update_processed(service.read(), "source.pdf", {"outputs": ["p1.jpg", "p2.jpg"]})
    before = load_manifest_playlist(str(tmp_path))
    page = next(item for item in before["items"] if item["url"] == str(cache / "p1.jpg"))
    page["title"] = "Custom page"
    save_manifest_playlist(str(tmp_path), before)
    (cache / "new1.jpg").write_bytes(b"new page")
    with service.lock:
        service.update_processed(service.read(), "source.pdf", {"outputs": ["new1.jpg"]})

    after = load_manifest_playlist(str(tmp_path))
    assert {item["url"] for item in after["items"]} == {str(cache / "new1.jpg"), str(tmp_path / "p1.jpg")}
    updated = next(item for item in after["items"] if item["id"] == page["id"])
    assert updated["title"] == "Custom page"


def test_restart_rejects_incompatible_legacy_baseline(tmp_path):
    from solin.core.ingest.sync.journal import JournalSeedConflict
    from solin.core.playlists.linked_folder import LinkedPlaylistSync

    MANIFEST_REPOSITORY.update(tmp_path, lambda value: value.update({
        "playlist": {"items": [{"id": "original", "type": "image", "url": "a.jpg"}]},
    }))
    first = LinkedPlaylistSync(tmp_path, state_dir=tmp_path / "state-a")
    first.read()
    MANIFEST_REPOSITORY.update(tmp_path, lambda value: value.update({
        "playlist": {"items": [{"id": "incompatible", "type": "image", "url": "b.jpg"}]},
    }))
    other = LinkedPlaylistSync(tmp_path, state_dir=tmp_path / "state-b")
    with pytest.raises(JournalSeedConflict):
        other.read()


def test_deleted_page_does_not_return_when_source_is_reconverted(tmp_path):
    from solin.core.playlists.linked_folder import playlist_sync

    cache = tmp_path / ".solin_cache"
    cache.mkdir()
    (cache / "old.jpg").write_bytes(b"old page")
    service = playlist_sync(tmp_path)
    service.update_processed(service.read(), "source.pdf", {"outputs": ["old.jpg"]})
    item = load_manifest_playlist(str(tmp_path))["items"][0]
    remove_item_from_manifest(str(tmp_path), item)
    (cache / "new.jpg").write_bytes(b"new page")
    service.update_processed(service.read(), "source.pdf", {"outputs": ["new.jpg"]})
    assert load_manifest_playlist(str(tmp_path))["items"] == []


def test_rename_preserves_pending_intent_and_document_after_restart(tmp_path):
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
    from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
    from solin.core.playlists import linked_folder

    folder = tmp_path / "original"
    folder.mkdir()
    (folder / "image.jpg").write_bytes(b"image")
    view = load_manifest_playlist(str(folder))
    save_manifest_playlist(str(folder), view)
    document = linked_folder.playlist_sync(folder).replica.document_id
    view["items"][0]["title"] = "Pending rename"
    stage_manifest_playlist(str(folder), view)
    destination = WatchedFolderPlaylistStore().rename_folder(str(folder), "renamed", WatchedFolderFileStore())
    linked_folder._SERVICES.clear()

    loaded = load_manifest_playlist(destination)
    assert loaded["items"][0]["title"] == "Pending rename"
    assert loaded["items"][0]["url"] == str(Path(destination) / "image.jpg")
    assert linked_folder.playlist_sync(destination).replica.document_id == document


def test_deleted_folder_recreated_at_same_path_does_not_restore_old_history(tmp_path):
    from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
    from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
    from solin.core.playlists import linked_folder

    folder = tmp_path / "playlist"
    folder.mkdir()
    (folder / "old.jpg").write_bytes(b"old")
    save_manifest_playlist(str(folder), load_manifest_playlist(str(folder)))
    old_document = linked_folder.playlist_sync(folder).replica.document_id
    WatchedFolderPlaylistStore().delete_folder(str(folder), WatchedFolderFileStore())
    folder.mkdir()
    (folder / "new.jpg").write_bytes(b"new")
    linked_folder._SERVICES.clear()
    view = load_manifest_playlist(str(folder))
    save_manifest_playlist(str(folder), view)
    assert [Path(item["url"]).name for item in view["items"]] == ["new.jpg"]
    assert linked_folder.playlist_sync(folder).replica.document_id != old_document


def test_reset_preserves_tree_and_rejects_delayed_previous_document_edit(tmp_path):
    from solin.core.ingest.watched_folder_playlists import WatchedFolderPlaylistStore
    from solin.core.ingest.sync.journal import JournalCorrupt

    (tmp_path / "image.jpg").write_bytes(b"image")
    current = load_manifest_playlist(str(tmp_path))
    save_manifest_playlist(str(tmp_path), current)
    stale = copy.deepcopy(current)
    WatchedFolderPlaylistStore().reset_sync(str(tmp_path))
    stale["items"][0]["title"] = "Old terminal"
    with pytest.raises(JournalCorrupt, match="replaced"):
        save_manifest_playlist(str(tmp_path), stale)
    assert load_manifest_playlist(str(tmp_path))["items"][0]["title"] == "image"


def test_provisional_edit_survives_late_document_and_explicit_organization(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    for folder in (left, right):
        (folder / "image.jpg").write_bytes(b"image")
    provisional = load_manifest_playlist(str(right))
    provisional["items"][0]["title"] = "Edited before metadata"
    stage_manifest_playlist(str(right), provisional)
    save_manifest_playlist(str(left), {
        "sections": [{"id": "treasures", "title": "Treasures"}],
        "items": [{"id": "explicit", "url": str(left / "image.jpg"), "type": "image",
                   "title": "Image", "section_id": "treasures"}],
    })
    shutil.copytree(left / ".solin_sync", right / ".solin_sync", dirs_exist_ok=True)
    loaded = load_manifest_playlist(str(right))
    assert len(loaded["items"]) == 1
    assert loaded["items"][0]["id"] == "explicit"
    assert loaded["items"][0]["title"] == "Edited before metadata"
    assert loaded["items"][0]["section_id"] == "treasures"
