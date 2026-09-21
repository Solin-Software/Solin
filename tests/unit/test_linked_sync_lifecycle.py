"""Document lifecycle and delayed identity delivery between isolated replicas."""

from pathlib import Path
import shutil

import pytest

from solin.core.ingest.sync.journal import (
    JournalCorrupt,
    JournalPublicationRejected,
    JournalReplica,
    ReplicaSnapshot,
)


def _replicas(tmp_path):
    result = []
    for index in range(2):
        folder = tmp_path / f"computer-{index}"
        folder.mkdir()
        result.append(JournalReplica(folder, "playlist", state_dir=tmp_path / f"state-{index}"))
    return result


def _deliver(first, second, *, descriptors=True, operations=True):
    for name, enabled in (("documents", descriptors), ("operations", operations)):
        source = first.operations_dir.parent / name
        if enabled and source.exists():
            shutil.copytree(source, second.operations_dir.parent / name, dirs_exist_ok=True)


def test_discovery_alone_never_initializes_shared_document(tmp_path):
    first, _ = _replicas(tmp_path)
    initial = first.read()
    observed = first.commit(initial, {"automatic": {"resource": "image.jpg"}})
    assert observed.document_id is None
    assert first.pending_count == 1
    assert not (first.folder / ".solin_sync").exists()
    reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    assert reopened.read() == observed


def test_initialization_adopts_provisional_edits_and_old_rendered_baseline(tmp_path):
    first, _ = _replicas(tmp_path)
    provisional = first.commit(first.read(), {"automatic": {"title": "Original"}})
    identity = first.initialize_document()
    edited = first.commit(provisional, {"automatic": {"title": "Edited from provisional view"}})
    assert edited.document_id == identity
    assert edited.entities["automatic"]["title"] == "Edited from provisional view"
    assert first.pending_count == 0


def test_late_descriptor_adopts_provisional_without_deleting_unseen_remote_nodes(tmp_path):
    first, second = _replicas(tmp_path)
    first.initialize_document()
    authoritative = first.commit(first.read(), {"remote": {"title": "Remote"}})
    provisional = second.commit(second.read(), {"automatic": {"title": "Local fallback"}})
    _deliver(first, second, descriptors=False)
    assert second.read() == provisional
    _deliver(first, second, operations=False)
    received = second.read()
    assert received.document_id == authoritative.document_id
    assert set(received.entities) == {"remote", "automatic"}
    staged = second.stage(provisional, {"automatic": {"title": "Local changed"}})
    assert "remote" not in staged.entities
    assert set(second.read().entities) == {"remote", "automatic"}
    _deliver(second, first)
    assert first.read() == second.read()


def test_adopted_aliases_survive_restart(tmp_path):
    first, _ = _replicas(tmp_path)
    provisional = first.stage(first.read(), {"image": {"title": "Old"}})
    first.initialize_document()
    first.read()
    reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    edited = reopened.stage(provisional, {"image": {"title": "New"}})
    assert reopened.read() == edited


def test_raw_intent_survives_identity_adoption(tmp_path):
    first, _ = _replicas(tmp_path)
    source = first.state_dir / "intents" / "local.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"source":"external-path"}')
    first.initialize_document()
    assert (first.state_dir / "intents" / source.name).read_bytes() == source.read_bytes()


def test_concurrent_explicit_initializations_require_resolution(tmp_path):
    first, second = _replicas(tmp_path)
    assert first.initialize_document() != second.initialize_document()
    _deliver(first, second)
    with pytest.raises(JournalCorrupt, match="Conflicting document identities"):
        second.read()
    second.reset_document()
    _deliver(second, first)
    assert first.read() == second.read()


def test_required_document_selects_marker_epoch_among_unrelated_heads(tmp_path):
    first, second = _replicas(tmp_path)
    selected = first.initialize_document()
    expected = first.commit(first.read(), {"selected": {"title": "Selected"}})
    second.initialize_document()
    second.commit(second.read(), {"other": {"title": "Other"}})
    _deliver(second, first)

    with pytest.raises(JournalCorrupt, match="Conflicting document identities"):
        first.read()
    first.require_document(selected)

    assert first.read() == expected


def test_publication_guard_preserves_rejected_edit_in_durable_outbox(tmp_path):
    folder = tmp_path / "computer"
    folder.mkdir()
    active = True

    def require_active(_document_id: str) -> None:
        if not active:
            raise OSError("activation marker is absent")

    replica = JournalReplica(
        folder,
        "meeting",
        state_dir=tmp_path / "state",
        publication_guard=require_active,
    )
    replica.initialize_document()
    base = replica.read()
    active = False

    with pytest.raises(JournalPublicationRejected, match="marker is absent"):
        replica.commit(base, {"image": {"title": "Pending"}})

    assert len(list(replica.outbox_dir.glob("*.json"))) == 1


def test_reset_filters_old_operations_and_rejects_stale_snapshot(tmp_path):
    first, second = _replicas(tmp_path)
    first.initialize_document()
    old = first.commit(first.read(), {"old": {"title": "Old document"}})
    old_state = first.state_dir
    _deliver(first, second)
    second.read()
    replacement = first.reset_document()
    assert replacement != old.document_id
    assert first.has_previous_document
    assert first.read().entities == {}
    assert any((old_state / "operations").glob("*.json"))
    with pytest.raises(JournalCorrupt, match="replaced"):
        first.commit(old, {"old": {"title": "Stale edit"}})
    _deliver(first, second, operations=False)
    assert second.read().entities == {}
    assert second.document_id == replacement
    assert second.has_previous_document


def test_missing_descriptor_does_not_reset_known_document(tmp_path):
    first, _ = _replicas(tmp_path)
    first.initialize_document()
    before = first.commit(first.read(), {"image": {"title": "Original"}})
    for path in (first.operations_dir.parent / "documents").glob("*.json"):
        path.unlink()
    assert first.read() == before


def test_explicit_retirement_and_same_path_recreation_do_not_mix_history(tmp_path):
    first, _ = _replicas(tmp_path)
    first.initialize_document()
    old = first.commit(first.read(), {"old": {"title": "Old"}})
    first.retire_binding()
    shutil.rmtree(first.folder)
    first.folder.mkdir()
    reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    assert reopened.read().entities == {}
    assert reopened.document_id is None
    assert reopened.initialize_document() != old.document_id
    assert reopened.has_previous_document
    with pytest.raises(JournalCorrupt, match="replaced"):
        reopened.stage(old, old.entities)


def test_rename_preserves_identity_and_unpublished_outbox(tmp_path):
    first, _ = _replicas(tmp_path)
    first.initialize_document()
    staged = first.stage(first.read(), {"image": {"title": "Pending"}})
    previous = first.folder
    destination = previous.with_name("renamed")
    previous.rename(destination)
    first.rebind_folder(destination)
    reopened = JournalReplica(destination, "playlist", state_dir=tmp_path / "state-0")
    assert reopened.read() == staged
    previous.mkdir()
    unrelated = JournalReplica(previous, "playlist", state_dir=tmp_path / "state-0")
    assert unrelated.read() == ReplicaSnapshot()


def test_rebinding_current_folder_is_idempotent(tmp_path):
    first, _ = _replicas(tmp_path)
    first.initialize_document()
    staged = first.stage(first.read(), {"image": {"title": "Pending"}})
    first.rebind_folder(first.folder)
    reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    assert reopened.read() == staged


def test_cold_stage_never_inspects_cloud_folder(tmp_path, monkeypatch):
    first, _ = _replicas(tmp_path)
    baseline = first.read({"image": {"title": "Original"}})
    stat = Path.stat

    def local_only(path, *args, **kwargs):
        if path == first.folder or path.is_relative_to(first.folder):
            pytest.fail(f"Cold local staging inspected cloud path: {path}")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", local_only)
        reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
        staged = reopened.stage(baseline, {"image": {"title": "Edited offline"}})
    assert reopened.read() == staged


def test_interrupted_adoption_replays_durably_after_restart(tmp_path, monkeypatch):
    import solin.core.ingest.sync.journal as journal

    first, _ = _replicas(tmp_path)
    original = first.stage(first.read(), {"image": {"title": "Original"}})
    latest = first.stage(original, {"image": {"title": "Edited"}})
    write = journal.write_bytes_atomic

    def interrupt_aliases(path, payload):
        if path.name == "aliases.json":
            raise OSError("Interrupted before adoption checkpoint")
        write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(journal, "write_bytes_atomic", interrupt_aliases)
        with pytest.raises(OSError, match="Interrupted"):
            first.initialize_document()
    reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    assert reopened.read().entities == latest.entities
    edited = reopened.stage(latest, {"image": {"title": "After restart"}})
    assert reopened.read() == edited
    assert len(list(reopened.operations_dir.glob("*.json"))) == 3


def test_local_snapshot_preserves_pending_edits_during_identity_conflict(tmp_path, monkeypatch):
    first, second = _replicas(tmp_path)
    first.initialize_document()
    original = first.commit(first.read(), {"image": {"title": "Original"}})
    pending = first.stage(original, {"image": {"title": "Unpublished"}})
    second.initialize_document()
    _deliver(second, first)
    with pytest.raises(JournalCorrupt, match="Conflicting document identities"):
        first.read()
    stat = Path.stat

    def local_only(path, *args, **kwargs):
        if path == first.folder or path.is_relative_to(first.folder):
            pytest.fail(f"Local baseline read inspected cloud path: {path}")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", local_only)
        local = first.read_local()
    assert local == pending
    assert first.reset_document() != pending.document_id
    replacement = first.commit(first.read(), local.entities)
    _deliver(first, second)
    assert second.read() == replacement


def test_local_snapshot_rejects_corrupted_archive(tmp_path):
    first, _ = _replicas(tmp_path)
    first.read({"image": {"title": "Original"}})
    path = next(first.archive_dir.glob("*.json"))
    path.write_bytes(b'{"version":')
    with pytest.raises(JournalCorrupt):
        first.read_local()


def test_two_local_bindings_preserve_each_provisional_alias_history(tmp_path):
    first, second = _replicas(tmp_path)
    second = JournalReplica(second.folder, "playlist", state_dir=tmp_path / "state-0")
    first_provisional = first.stage(first.read(), {"first": {"title": "First"}})
    second_provisional = second.stage(second.read(), {"second": {"title": "Second"}})
    first.initialize_document()
    first.read()
    _deliver(first, second)
    second.read()
    reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    staged = reopened.stage(first_provisional, {"first": {"title": "Updated"}})
    assert staged.entities == {"first": {"title": "Updated"}}
    second.stage(second_provisional, {"second": {"title": "Updated too"}})
    assert reopened.read().entities == {
        "first": {"title": "Updated"}, "second": {"title": "Updated too"}
    }


def test_local_reset_rejects_old_snapshot_in_another_open_instance(tmp_path):
    first, _ = _replicas(tmp_path)
    old = first.read({"image": {"title": "Original"}})
    other = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    other.read()
    first.reset_document()
    with pytest.raises(JournalCorrupt, match="replaced"):
        other.stage(old, {"image": {"title": "Stale edit"}})


def test_unsuccessful_folder_delete_restores_binding_and_pending_organization(tmp_path):
    first, _ = _replicas(tmp_path)
    original = first.read({"image": {"title": "Original", "parent": "treasures"}})
    pending = first.stage(original, {"image": {"title": "Edited", "parent": "treasures"}})
    with pytest.raises(PermissionError, match="locked"):
        with first.retiring_binding():
            assert first.document_id is None
            shutil.rmtree(first.operations_dir.parent)
            raise PermissionError("Remaining media is locked")
    assert first.read_local() == pending
    reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    assert reopened.read() == pending


def test_successful_folder_delete_retires_binding_durably(tmp_path):
    first, _ = _replicas(tmp_path)
    first.read({"image": {"title": "Old"}})
    with first.retiring_binding():
        shutil.rmtree(first.folder)
    first.folder.mkdir()
    reopened = JournalReplica(first.folder, "playlist", state_dir=tmp_path / "state-0")
    assert reopened.read() == ReplicaSnapshot()


def test_rebinding_rolls_back_both_bindings_after_dependent_failure(tmp_path):
    first, _ = _replicas(tmp_path)
    original = first.read({"image": {"title": "Original"}})
    pending = first.stage(original, {"image": {"title": "Pending"}})
    previous_folder, previous_state = first.folder, first.state_dir
    destination = previous_folder.with_name("renamed")
    previous_folder.rename(destination)
    with pytest.raises(PermissionError, match="intent"):
        with first.rebinding_folder(destination):
            assert first.folder == destination
            raise PermissionError("Pending intent rewrite failed")
    destination.rename(previous_folder)
    assert first.folder == previous_folder
    assert first.state_dir == previous_state
    assert first.read_local() == pending
    restarted = JournalReplica(previous_folder, "playlist", state_dir=tmp_path / "state-0")
    assert restarted.read() == pending
    destination.mkdir()
    unrelated = JournalReplica(destination, "playlist", state_dir=tmp_path / "state-0")
    assert unrelated.read() == ReplicaSnapshot()
