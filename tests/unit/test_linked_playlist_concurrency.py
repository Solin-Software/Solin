"""Delayed metadata and independent user intentions across separate replicas."""

from copy import deepcopy
from pathlib import Path
import shutil
import threading

from solin.core.ingest.sync.discovery import (
    AUXILIARY,
    DISCOVERED,
    RESOURCE,
    reconcile_discoveries,
    record_discovery_edits,
)
from solin.core.ingest.sync.journal import JournalReplica
from solin.core.playlists.linked_folder import LinkedPlaylistSync, STATE


def test_playlist_local_staging_does_not_wait_for_cloud_refresh(tmp_path, monkeypatch):
    from solin.core.ingest.watched_folder import load_manifest_playlist, stage_manifest_playlist
    from solin.core.playlists.linked_folder import playlist_sync

    (tmp_path / "image.jpg").write_bytes(b"image")
    view = load_manifest_playlist(str(tmp_path))
    service = playlist_sync(tmp_path)
    service.save(view)
    scanning, resume, staged = (threading.Event() for _ in range(3))
    failures = []
    scan = service.replica._scan

    def blocked_scan(directory):
        if directory == service.replica.operations_dir:
            scanning.set()
            assert resume.wait(5)
        return scan(directory)

    def stage():
        try:
            view["items"][0]["title"] = "Staged during cloud wait"
            stage_manifest_playlist(str(tmp_path), view)
        except (ValueError, OSError) as error:
            failures.append(error)
        finally:
            staged.set()

    monkeypatch.setattr(service.replica, "_scan", blocked_scan)
    reader = threading.Thread(target=load_manifest_playlist, args=(str(tmp_path),))
    writer = threading.Thread(target=stage)
    reader.start()
    try:
        assert scanning.wait(5)
        writer.start()
        assert staged.wait(1), "GUI staging waited on the background service lock"
    finally:
        resume.set()
        reader.join(5)
        writer.join(5)
    assert not failures
    assert load_manifest_playlist(str(tmp_path))["items"][0]["title"] == "Staged during cloud wait"


def _replicas(root: Path):
    result = []
    for index in range(3):
        folder = root / f"computer-{index}"
        folder.mkdir()
        result.append(JournalReplica(folder, "playlist", state_dir=root / f"state-{index}"))
    result[0].initialize_document()
    for replica in result[1:]:
        _deliver(result[0], replica)
    return result


def _deliver(sender, receiver):
    descriptors = sender.operations_dir.parent / "documents"
    if descriptors.exists():
        shutil.copytree(descriptors, receiver.operations_dir.parent / "documents", dirs_exist_ok=True)
    receiver.read()
    receiver.operations_dir.mkdir(parents=True, exist_ok=True)
    for path in reversed(sorted(sender.operations_dir.glob("*.json"))):
        shutil.copy2(path, receiver.operations_dir / path.name)
        receiver.read()


def _converge(replicas):
    for sender in replicas:
        for receiver in replicas:
            if sender is not receiver:
                _deliver(sender, receiver)
                _deliver(sender, receiver)
    snapshots = [replica.read() for replica in replicas]
    assert snapshots[0] == snapshots[1] == snapshots[2]
    return reconcile_discoveries(snapshots[0].entities)


def test_concurrent_different_fallback_fields_survive_late_explicit_placement(tmp_path):
    first, second, third = replicas = _replicas(tmp_path)
    fallback = {"automatic": {
        RESOURCE: "image.jpg", DISCOVERED: True,
        "title": "Image", "parent": "default", "caption": "Original caption",
    }}
    first_base = first.commit(first.read(), fallback)
    _deliver(first, second)
    second_base = second.read()
    desired = deepcopy(first_base.entities)
    desired["automatic"]["title"] = "Renamed by first computer"
    first.commit(first_base, record_discovery_edits(first_base.entities, desired))
    desired = deepcopy(second_base.entities)
    desired["automatic"]["parent"] = "chosen by second computer"
    desired["automatic"].pop("caption")
    second.commit(second_base, record_discovery_edits(second_base.entities, desired))
    third.commit(third.read(), {"explicit": {
        RESOURCE: "image.jpg", "title": "Image", "parent": "original placement",
        "caption": "Original caption",
    }})
    visible = _converge(replicas)
    assert set(visible) == {"explicit"}
    assert visible["explicit"]["title"] == "Renamed by first computer"
    assert visible["explicit"]["parent"] == "chosen by second computer"
    assert "caption" not in visible["explicit"]


def test_concurrent_fallback_delete_suppresses_late_claim_and_survives_reopen(tmp_path):
    first, second, third = replicas = _replicas(tmp_path)
    fallback = {"automatic": {RESOURCE: "image.jpg", DISCOVERED: True, "title": "Image"}}
    baseline = first.commit(first.read(), fallback)
    _deliver(first, second)
    stale = second.read()
    first.commit(baseline, record_discovery_edits(baseline.entities, {}))
    changed = deepcopy(stale.entities)
    changed["automatic"]["title"] = "Concurrent rename"
    second.commit(stale, record_discovery_edits(stale.entities, changed))
    third.commit(third.read(), {"explicit": {RESOURCE: "image.jpg", "title": "Image"}})
    assert all(node.get(AUXILIARY) for node in _converge(replicas).values())
    for index, replica in enumerate(replicas):
        reopened = JournalReplica(replica.folder, "playlist", state_dir=tmp_path / f"state-{index}")
        for _ in range(3):
            assert all(node.get(AUXILIARY) for node in reconcile_discoveries(reopened.read().entities).values())


def test_editing_explicit_parent_does_not_discard_concurrent_fallback_title(tmp_path):
    first, second, third = replicas = _replicas(tmp_path)
    before = first.commit(first.read(), {"automatic": {
        RESOURCE: "image.jpg", DISCOVERED: True, "title": "Original", "parent": "default",
    }})
    edited = deepcopy(before.entities)
    edited["automatic"]["title"] = "First rename"
    before = first.commit(before, record_discovery_edits(before.entities, edited))
    _deliver(first, second)
    third.commit(third.read(), {"explicit": {RESOURCE: "image.jpg", "title": "Original", "parent": "initial"}})
    _deliver(third, second)
    base = second.read()
    projected = reconcile_discoveries(base.entities)
    projected["explicit"]["parent"] = "User moved"
    recorded = record_discovery_edits(base.entities, projected)
    # Changing placement must not clear independent title intentions: another
    # terminal may still be editing that field on the provisional occurrence.
    second.commit(base, recorded)
    edited = deepcopy(before.entities)
    edited["automatic"]["title"] = "Second rename"
    first.commit(before, record_discovery_edits(before.entities, edited))
    visible = _converge(replicas)
    assert visible["explicit"]["title"] == "Second rename"
    assert visible["explicit"]["parent"] == "User moved"


def test_absorbed_fallback_refresh_does_not_publish_an_edit(tmp_path):
    first, second, _third = _replicas(tmp_path)
    before = first.commit(first.read(), {"automatic": {
        RESOURCE: "image.jpg", DISCOVERED: True, "title": "Original", "caption": None,
    }})
    desired = deepcopy(before.entities)
    desired["automatic"]["title"] = "User title"
    desired["automatic"].pop("caption")
    first.commit(before, record_discovery_edits(before.entities, desired))
    second.commit(second.read(), {"explicit": {
        RESOURCE: "image.jpg", "title": "Original", "caption": "Provider caption",
    }})
    _deliver(second, first)
    before = first.read()
    visible = reconcile_discoveries(before.entities)
    assert "caption" not in visible["explicit"]
    for _ in range(3):
        assert first.commit(before, record_discovery_edits(before.entities, visible)) == before


def _playlist_view(service, snapshot):
    return {**service.manifest(snapshot)["playlist"], STATE: snapshot.to_dict()}


def test_playlist_deleted_group_preserves_concurrent_nested_insertion(tmp_path):
    replicas = _replicas(tmp_path)
    services = [LinkedPlaylistSync(replica.folder, state_dir=tmp_path / f"adapter-{index}")
                for index, replica in enumerate(replicas)]
    first, second, third = services
    baseline = first.save({"sections": [
        {"id": "parent", "title": "Parent"},
        {"id": "sub", "title": "Subsection", "parent_id": "parent"},
    ], "items": [{"id": "old", "url": "old.jpg", "type": "image", "section_id": "sub"}]})
    _deliver(first.replica, second.replica)
    stale = second.read()
    removal = _playlist_view(first, baseline)
    removal["sections"] = [removal["sections"][0]]
    removal["items"] = []
    first.save(removal)
    addition = _playlist_view(second, stale)
    addition["items"].append({"id": "new", "url": "new.jpg", "type": "image", "section_id": "sub"})
    second.save(addition)
    _converge([service.replica for service in services])
    for service in (first, second, third):
        view = service.manifest(service.read())["playlist"]
        assert [item["id"] for item in view["items"]] == ["new"]
        assert view["items"][0]["section_id"] == "parent"


def test_playlist_video_repetitions_survive_remote_delete_and_image_deduplicates(tmp_path):
    replicas = _replicas(tmp_path)
    services = [LinkedPlaylistSync(replica.folder, state_dir=tmp_path / f"adapter-{index}")
                for index, replica in enumerate(replicas)]
    first, second, third = services
    initial = first.save({"items": [
        {"id": "video-a", "url": "video.mp4", "type": "video"},
        {"id": "video-b", "url": "video.mp4", "type": "video"},
    ]})
    _deliver(first.replica, second.replica)
    stale = second.read()
    desired = _playlist_view(first, initial)
    desired["items"].pop(0)
    first.save(desired)
    desired = _playlist_view(second, stale)
    desired["items"][0]["title"] = "Concurrent edit"
    desired["items"].append({"id": "image-a", "url": "image.jpg", "type": "image"})
    second.save(desired)
    third.save({"items": [{"id": "image-b", "url": "IMAGE.JPG", "type": "image"}]})
    _converge([service.replica for service in services])
    for service in services:
        view = service.manifest(service.read())["playlist"]
        assert {item["id"] for item in view["items"]} == {"video-b", "image-a"}
