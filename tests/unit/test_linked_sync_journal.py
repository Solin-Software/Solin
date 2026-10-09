from __future__ import annotations

import copy
import json
import shutil
import threading
from pathlib import Path

import pytest

from solin.core.ingest.sync.journal import (
    JournalCorrupt,
    JournalReplica,
    JournalSeedConflict,
    JournalUnavailable,
    JournalUnsupported,
    ReplicaSnapshot,
)


class Transport:
    """Separate replica folders; deliver individual files in any chosen order."""

    def __init__(self, root: Path):
        self.replicas = []
        for index in range(3):
            folder = root / f"computer-{index}"
            folder.mkdir()
            self.replicas.append(
                JournalReplica(folder, "meeting", state_dir=root / f"local-{index}")
            )

    def deliver(self, source: int, destination: int, ids: tuple[str, ...] | None = None):
        sender, receiver = self.replicas[source], self.replicas[destination]
        source_descriptors = sender.operations_dir.parent / "documents"
        destination_descriptors = receiver.operations_dir.parent / "documents"
        if source_descriptors.exists():
            shutil.copytree(source_descriptors, destination_descriptors, dirs_exist_ok=True)
        receiver.operations_dir.mkdir(parents=True, exist_ok=True)
        for path in sender.operations_dir.glob("*.json"):
            if ids is None or path.stem in ids:
                shutil.copy2(path, receiver.operations_dir / path.name)

    def converge(self):
        for source in range(3):
            for destination in range(3):
                if source != destination:
                    self.deliver(source, destination)
        snapshots = [replica.read() for replica in self.replicas]
        assert snapshots[0] == snapshots[1] == snapshots[2]
        return snapshots[0]


@pytest.fixture
def transport(tmp_path):
    return Transport(tmp_path)


def test_empty_read_does_not_create_shared_metadata(transport):
    replica = transport.replicas[0]
    assert replica.read() == ReplicaSnapshot()
    assert not replica.operations_dir.exists()


def test_seed_is_identical_and_idempotent_on_multiple_computers(transport):
    seed = {"image": {"title": "Image", "parent": "treasures"}}
    first, second, _ = transport.replicas
    assert first.read(seed) == second.read(seed)
    assert first.read(seed) == first.read(seed)
    assert len(list(first.operations_dir.glob("*.json"))) == 1
    assert transport.converge().entities == seed


def test_different_migrations_fail_closed(transport):
    first, second, _ = transport.replicas
    first.read({"image": {"parent": "treasures"}})
    second.read({"image": {"parent": "living"}})
    transport.deliver(0, 1)
    with pytest.raises(JournalCorrupt, match="Conflicting document identities"):
        second.read()


def test_read_rejects_changed_legacy_seed(transport):
    first = transport.replicas[0]
    previous = first.read({"image": {"title": "Before"}})
    with pytest.raises(JournalSeedConflict):
        first.read({"image": {"title": "After"}})
    assert first.read() == previous


def test_late_seed_does_not_override_a_user_edit(transport):
    first = transport.replicas[0]
    first.commit(first.read(), {"image": {"parent": "treasures"}})
    result = first.read({"image": {"parent": "living", "title": "Image"}})
    assert result.entities == {"image": {"parent": "treasures", "title": "Image"}}


def test_concurrent_delete_wins_over_stale_edit_and_move(transport):
    first, second, _ = transport.replicas
    baseline = first.read({"image": {"title": "Image", "parent": "treasures"}})
    transport.deliver(0, 1)
    stale = second.read()
    first.commit(baseline, {})
    second.commit(stale, {"image": {"title": "Edited", "parent": "living"}})
    result = transport.converge()
    assert result.entities == {}
    assert second.commit(stale, stale.entities).entities == {}
    assert transport.converge().entities == {}


def test_independent_fields_and_insertions_survive_concurrent_edits(transport):
    first, second, third = transport.replicas
    baseline = first.read({"image": {"title": "Image", "parent": "treasures"}})
    transport.deliver(0, 1)
    transport.deliver(0, 2)
    second_base, third_base = second.read(), third.read()
    first.commit(baseline, {"image": {"title": "Renamed", "parent": "treasures"}})
    second.commit(second_base, {"image": {"title": "Image", "parent": "living"}})
    third.commit(third_base, {**third_base.entities, "other": {"title": "New"}})
    assert transport.converge().entities == {
        "image": {"title": "Renamed", "parent": "living"},
        "other": {"title": "New"},
    }


def test_concurrent_same_field_is_deterministic_and_later_edit_wins(transport):
    first, second, _ = transport.replicas
    baseline = first.read({"image": {"title": "Image"}})
    transport.deliver(0, 1)
    stale = second.read()
    first.commit(baseline, {"image": {"title": "First"}})
    second.commit(stale, {"image": {"title": "Second"}})
    converged = transport.converge()
    assert converged.entities["image"]["title"] in {"First", "Second"}
    first.commit(converged, {"image": {"title": "Resolved"}})
    assert transport.converge().entities["image"]["title"] == "Resolved"


def test_dependent_edit_waits_for_delayed_baseline(transport):
    first, second, _ = transport.replicas
    baseline = first.read({"image": {"title": "Original"}})
    edited = first.commit(baseline, {"image": {"title": "Changed"}})
    edit_id = tuple(set(edited.operation_ids) - set(baseline.operation_ids))
    transport.deliver(0, 1, edit_id)
    assert second.read().entities == {}
    assert second.waiting_count == 1
    transport.deliver(0, 1, baseline.operation_ids)
    assert second.read() == edited
    assert second.waiting_count == 0


def test_duplicate_and_reversed_delivery_replays_once(transport):
    first, second, _ = transport.replicas
    snapshots = [first.read({"image": {"title": "0"}})]
    for index in range(1, 6):
        snapshots.append(first.commit(snapshots[-1], {"image": {"title": str(index)}}))
    previous_ids = set()
    sequence = []
    for snapshot in snapshots:
        sequence.extend(set(snapshot.operation_ids) - previous_ids)
        previous_ids.update(snapshot.operation_ids)
    for operation_id in reversed(sequence):
        transport.deliver(0, 1, (operation_id,))
        transport.deliver(0, 1, (operation_id,))
        second.read()
    assert second.read() == snapshots[-1]
    assert len(second.read().entities) == 1


def test_removed_field_does_not_destroy_concurrent_other_field(transport):
    first, second, _ = transport.replicas
    baseline = first.read({"image": {"title": "Image", "caption": "Caption"}})
    transport.deliver(0, 1)
    stale = second.read()
    first.commit(baseline, {"image": {"title": "Image"}})
    second.commit(stale, {"image": {"title": "New", "caption": "Caption"}})
    assert transport.converge().entities == {"image": {"title": "New"}}


def test_explicit_restore_must_observe_the_delete(transport):
    first, second, _ = transport.replicas
    original = {"image": {"title": "Image"}}
    baseline = first.read(original)
    transport.deliver(0, 1)
    stale = second.read()
    deleted = first.commit(baseline, {})
    second.commit(stale, original, restore_ids={"image"})
    assert transport.converge().entities == {}
    restored = first.commit(first.read(), original, restore_ids={"image"})
    assert restored.entities == original
    assert set(deleted.operation_ids) <= set(restored.operation_ids)
    assert transport.converge().entities == original


def test_fresh_identity_can_be_added_after_delete(transport):
    first = transport.replicas[0]
    baseline = first.read({"image": {"resource": "picture.jpg"}})
    deleted = first.commit(baseline, {})
    result = first.commit(deleted, {"new-image": {"resource": "picture.jpg"}})
    assert result.entities == {"new-image": {"resource": "picture.jpg"}}


def test_entity_creation_context_preserves_original_causal_evidence(transport):
    first = transport.replicas[0]
    initial = first.read({"media": {"signature": "old"}, "metadata": {"signature": "old"}})
    deleted = first.commit(initial, {"deletion": {"occurrence_id": "media"}, "metadata": {"signature": "old"}})
    current = first.commit(deleted, {"deletion": {"occurrence_id": "media"}, "metadata": {"signature": "new"}})
    contexts = first.entity_creation_contexts(current, {"deletion"})
    assert contexts["deletion"] == [initial]
    assert first.read() == current


def test_content_scoped_deletion_and_replacement_converge_with_stale_delivery(transport):
    from solin.core.ingest.sync.discovery import (
        CONTENT, DISCOVERED, RESOURCE, reconcile_discoveries, record_discovery_edits,
    )
    from solin.core.ingest.sync.resources import automatic_occurrence_id

    first, second, _ = transport.replicas
    old = {"size": 8, "sha256": "a" * 64}
    new = {"size": 8, "sha256": "b" * 64}
    old_id = automatic_occurrence_id("photo.jpg", content=old)
    new_id = automatic_occurrence_id("photo.jpg", content=new)
    initial = first.read({old_id: {RESOURCE: "photo.jpg", CONTENT: old, DISCOVERED: True}})
    transport.deliver(0, 1)
    stale = second.read()
    first.commit(initial, record_discovery_edits(initial.entities, {}))
    second.commit(stale, {
        **stale.entities, new_id: {RESOURCE: "PHOTO.JPG", CONTENT: new, DISCOVERED: True},
    })
    merged = transport.converge()
    assert old_id not in reconcile_discoveries(merged.entities)
    assert new_id in reconcile_discoveries(merged.entities)
    second.commit(merged, record_discovery_edits(merged.entities, {
        key: node for key, node in reconcile_discoveries(merged.entities).items() if key != new_id
    }))
    merged = transport.converge()
    assert not any(node.get(RESOURCE) for node in reconcile_discoveries(merged.entities).values())


def test_offline_commit_and_restart_preserve_outbox(transport, tmp_path):
    first = transport.replicas[0]
    baseline = first.read({"image": {"title": "Original"}})
    parked = tmp_path / "disconnected"
    first.folder.rename(parked)
    staged = first.commit(baseline, {"image": {"title": "Offline edit"}})
    assert staged.entities["image"]["title"] == "Offline edit"
    assert first.pending_count == 1
    restarted = JournalReplica(first.folder, "meeting", state_dir=tmp_path / "local-0")
    with pytest.raises(JournalUnavailable):
        restarted.read()
    parked.rename(first.folder)
    assert restarted.read() == staged
    assert restarted.pending_count == 0
    assert not list(restarted.outbox_dir.glob("*.json"))
    transport.deliver(0, 1)
    assert transport.replicas[1].read() == staged


def test_stage_is_durable_without_reading_or_publishing_transport(transport, monkeypatch, tmp_path):
    first = transport.replicas[0]
    baseline = first.read({"image": {"title": "Original"}})
    before = set(first.operations_dir.glob("*.json"))
    staged = first.stage(baseline, {"image": {"title": "Staged"}})
    assert set(first.operations_dir.glob("*.json")) == before
    restarted = JournalReplica(first.folder, "meeting", state_dir=tmp_path / "local-0")
    assert restarted.read() == staged


def test_stage_does_not_make_unrendered_remote_nodes_part_of_gui_base(transport):
    first, second, _ = transport.replicas
    rendered = first.read({"image": {"title": "Original"}})
    transport.deliver(0, 1)
    second_base = second.read()
    second.commit(second_base, {**second_base.entities, "remote": {"title": "Inserted"}})
    transport.deliver(1, 0)
    # Background refresh archived an edit that the GUI has not rendered yet.
    first.read()
    staged = first.stage(rendered, {"image": {"title": "Local"}})
    assert "remote" not in staged.entities
    again = first.stage(staged, {"image": {"title": "Local again"}})
    merged = first.commit(again, again.entities)
    assert merged.entities == {"image": {"title": "Local again"}, "remote": {"title": "Inserted"}}


def test_warm_stage_does_not_scan_any_history(transport, monkeypatch):
    first = transport.replicas[0]
    base = first.read({"image": {"title": "Original"}})

    def unexpected_scan(directory):
        pytest.fail(f"Warm GUI staging scanned {directory}")

    monkeypatch.setattr(first, "_scan", unexpected_scan)
    staged = first.stage(base, {"image": {"title": "First"}})
    staged = first.stage(staged, {"image": {"title": "Second"}})
    assert staged.entities["image"]["title"] == "Second"


def test_local_stage_does_not_wait_for_cloud_read_lock(transport, monkeypatch):
    first = transport.replicas[0]
    base = first.read({"image": {"title": "Original"}})
    scanning = threading.Event()
    resume = threading.Event()
    staged = threading.Event()
    failures = []
    scan = first._scan

    def blocked_scan(directory):
        if directory == first.operations_dir:
            scanning.set()
            assert resume.wait(5)
        return scan(directory)

    def stage():
        try:
            first.stage(base, {"image": {"title": "Local edit"}})
        except (OSError, ValueError) as error:
            failures.append(error)
        finally:
            staged.set()

    monkeypatch.setattr(first, "_scan", blocked_scan)
    reader = threading.Thread(target=first.read)
    writer = threading.Thread(target=stage)
    reader.start()
    try:
        assert scanning.wait(5)
        writer.start()
        assert staged.wait(1), "Local durable staging waited for cloud I/O"
    finally:
        resume.set()
        reader.join(5)
        writer.join(5)
    assert not failures
    assert first.read().entities["image"]["title"] == "Local edit"


def test_fast_stage_still_rejects_reuse_of_a_deleted_identity(transport):
    first = transport.replicas[0]
    original = {"image": {"title": "Original"}}
    base = first.read(original)
    deleted = first.stage(base, {})
    stale_add = first.stage(deleted, original)
    assert stale_add.entities == {}
    restored = first.stage(stale_add, original, restore_ids={"image"})
    assert restored.entities == original
    assert first.read() == restored


def test_publication_failure_retains_durable_intent(transport, monkeypatch):
    import solin.core.ingest.sync.journal as journal

    first = transport.replicas[0]
    baseline = first.read({"image": {"title": "Original"}})
    publish = journal._publish

    def locked(path, operation):
        if path.parent == first.operations_dir:
            raise PermissionError("Cloud provider has locked the directory")
        publish(path, operation)

    monkeypatch.setattr(journal, "_publish", locked)
    edited = first.commit(baseline, {"image": {"title": "Pending"}})
    assert edited.entities["image"]["title"] == "Pending"
    assert first.pending_count == 1
    monkeypatch.setattr(journal, "_publish", publish)
    assert first.read() == edited
    assert first.pending_count == 0


def test_other_local_instance_can_publish_same_outbox_during_flush(transport, tmp_path, monkeypatch):
    import solin.core.ingest.sync.journal as journal

    first = transport.replicas[0]
    base = first.read({"image": {"title": "Original"}})
    staged = first.stage(base, {"image": {"title": "Accepted"}})
    second = JournalReplica(first.folder, "meeting", state_dir=tmp_path / "local-0")
    publish = journal._publish
    interrupted = False

    def concurrent_publication(path, operation):
        nonlocal interrupted
        if path.parent == first.operations_dir and not interrupted:
            interrupted = True
            second.read()
        publish(path, operation)

    monkeypatch.setattr(journal, "_publish", concurrent_publication)
    assert first.read() == staged
    assert second.read() == staged
    assert first.pending_count == second.pending_count == 0


def test_stage_accepted_during_cloud_flush_survives_restart(transport, tmp_path, monkeypatch):
    import solin.core.ingest.sync.journal as journal

    first = transport.replicas[0]
    base = first.read({"image": {"title": "Original"}})
    staged = first.stage(base, {"image": {"title": "First edit"}})
    publish = journal._publish
    newest = []

    def concurrent_stage(path, operation):
        if path.parent == first.operations_dir and not newest:
            newest.append(first.stage(staged, {"image": {"title": "Second edit"}}))
        publish(path, operation)

    monkeypatch.setattr(journal, "_publish", concurrent_stage)
    first.read()
    assert first.pending_count == 1
    restarted = JournalReplica(first.folder, "meeting", state_dir=tmp_path / "local-0")
    assert restarted.read() == newest[0]
    assert restarted.pending_count == 0


def test_outbox_moving_to_archive_during_scan_preserves_snapshot(transport, tmp_path, monkeypatch):
    first = transport.replicas[0]
    base = first.read({"image": {"title": "Original"}})
    staged = first.stage(base, {"image": {"title": "Accepted"}})
    operation_id = next(iter(set(staged.operation_ids) - set(base.operation_ids)))
    path = first.outbox_dir / f"{operation_id}.json"
    second = JournalReplica(first.folder, "meeting", state_dir=tmp_path / "local-0")
    stat = Path.stat
    interrupted = False

    def concurrent_archive(target, *args, **kwargs):
        nonlocal interrupted
        if target == path and not interrupted:
            interrupted = True
            second.read()
        return stat(target, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", concurrent_archive)
    assert first.read() == staged


def test_cold_stage_fails_closed_on_corrupt_local_history(transport, tmp_path):
    first = transport.replicas[0]
    baseline = first.read({"image": {"title": "Original"}})
    path = next(first.archive_dir.glob("*.json"))
    path.write_bytes(b'{"version":')
    restarted = JournalReplica(first.folder, "meeting", state_dir=tmp_path / "local-0")
    with pytest.raises(JournalCorrupt):
        restarted.stage(baseline, {"image": {"title": "Must not be accepted"}})
    assert not list(restarted.outbox_dir.glob("*.json"))


@pytest.mark.parametrize(
    "contents,error", [(b'{"version":', JournalCorrupt), (b'{"version":99}', JournalUnsupported)]
)
def test_invalid_remote_operation_preserves_last_valid_local_history(transport, contents, error):
    first = transport.replicas[0]
    baseline = first.read({"image": {"title": "Original"}})
    bad = first.operations_dir / "incomplete.json"
    bad.write_bytes(contents)
    with pytest.raises(error):
        first.read()
    bad.unlink()
    assert first.read() == baseline


def test_tampered_operation_cannot_change_materialization(transport):
    first = transport.replicas[0]
    first.read({"image": {"title": "Original"}})
    path = next(first.operations_dir.glob("*.json"))
    operation = json.loads(path.read_bytes())
    operation["entities"]["image"]["title"] = "Tampered"
    path.write_text(json.dumps(operation), encoding="utf-8")
    with pytest.raises(JournalCorrupt):
        first.read()


def test_snapshot_serialization_is_independent_and_validated(transport):
    first = transport.replicas[0]
    snapshot = first.read({"image": {"nested": {"value": "original"}}})
    data = snapshot.to_dict()
    assert ReplicaSnapshot.from_dict(data) == snapshot
    data["entities"]["image"]["nested"]["value"] = "changed"
    assert snapshot.entities["image"]["nested"]["value"] == "original"
    data["token"] = "invalid"
    with pytest.raises(JournalCorrupt):
        ReplicaSnapshot.from_dict(data)


def test_noop_refresh_or_commit_never_publishes_new_operations(transport):
    first = transport.replicas[0]
    snapshot = first.read({"image": {"title": "Original"}})
    before = {path.name: path.stat().st_mtime_ns for path in first.operations_dir.glob("*.json")}
    for _ in range(5):
        assert first.read() == snapshot
        assert first.commit(snapshot, copy.deepcopy(snapshot.entities)) == snapshot
    assert {
        path.name: path.stat().st_mtime_ns for path in first.operations_dir.glob("*.json")
    } == before


def test_idle_refresh_does_not_read_cached_operation_bytes(transport, monkeypatch):
    first = transport.replicas[0]
    snapshot = first.read({"image": {"title": "Original"}})
    first.read()
    first.read()
    original_read = Path.read_bytes

    def count_operation_reads(path):
        if path.suffix == ".json":
            pytest.fail(f"Cached operation was reread: {path}")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", count_operation_reads)
    assert first.read() == snapshot


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_large_tree_single_field_edit_preserves_nodes_and_small_operation(transport, count):
    first = transport.replicas[0]
    records = {str(index): {"title": f"Image {index}", "parent": "group"} for index in range(count)}
    base = first.read(records)
    desired = copy.deepcopy(base.entities)
    desired[str(count - 1)]["title"] = "Edited"
    staged = first.stage(base, desired)
    operation_id = next(iter(set(staged.operation_ids) - set(base.operation_ids)))
    path = first.outbox_dir / f"{operation_id}.json"
    assert path.stat().st_size < 1024
    assert first.read().entities == desired


@pytest.mark.parametrize("namespace", ["../escape", "a/b", "a\\b", "", ".", "..", "x:y"])
def test_invalid_namespace_is_rejected(tmp_path, namespace):
    with pytest.raises(ValueError):
        JournalReplica(tmp_path, namespace, state_dir=tmp_path / "state")


def test_nonfinite_payload_is_rejected_before_publication(transport):
    first = transport.replicas[0]
    with pytest.raises(JournalCorrupt):
        first.commit(first.read(), {"image": {"value": float("nan")}})
    assert not first.operations_dir.exists()
