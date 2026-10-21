"""Explicit meeting-folder deletion and recovery without replaying destruction."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import threading
import json

import pytest

from solin.core.foundation.thread_workers import CancellationFlag
from solin.core.ingest.sync.activation import ActivationStore
from solin.core.ingest.sync.journal import JournalError
from solin.core.ingest.watched_folder_files import WatchedFolderFileStore
from solin.core.media.operations import MediaOperationCancelled
from solin.core.meetings.folder_lifecycle import (
    MeetingFolderDeletion,
    MeetingFolderDeletionError,
)
from solin.core.meetings.linked_folder_sync import (
    MeetingLinkedFolderSync,
    MeetingSyncError,
    MeetingSyncIdentity,
)
from solin.core.meetings.tree_types import iter_nodes


@pytest.fixture
def deletion(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    folder = root / "2026-05-28 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    lifecycle = MeetingFolderDeletion(root, folder, identity, tmp_path / "local-state")
    return lifecycle, MeetingLinkedFolderSync(lambda _: 3), root, folder


def media(node_id, path, **fields):
    return {"id": node_id, "type": "media", "media_ref": {"file_path": str(path)}, **fields}


def remove(lifecycle, service, nodes=(), imports=None, store=None, cancellation=None):
    return lifecycle.delete(
        store or WatchedFolderFileStore(),
        service,
        nodes=list(nodes),
        imports=imports or {},
        cancellation=cancellation or CancellationFlag(),
    )


def ids(nodes):
    return {node["id"] for node in iter_nodes(nodes)}


def test_explicit_delete_prunes_sources_and_converted_outputs_preserving_canonical(
    deletion, tmp_path
):
    lifecycle, service, _, folder = deletion
    for name in ("clip.mp4", "slides.pdf", "list.jwlplaylist"):
        (folder / name).write_bytes(b"source")
    external = tmp_path / "page.jpg"
    external.write_bytes(b"converted")
    nodes = [
        {
            "id": "section",
            "type": "section",
            "meeting_generated": True,
            "children": [
                media("official", folder / "official.mp4", meeting_generated=True),
                media("direct", folder / "clip.mp4"),
                media("page", external),
                {"id": "playlist", "type": "subsection", "children": [media("child", external)]},
                media("unrelated", external),
            ],
        }
    ]
    imports = {
        "pdf": {"path": str(folder / "slides.pdf"), "node_ids": ["page"]},
        "list": {"path": str(folder / "list.jwlplaylist"), "node_ids": ["playlist"]},
        "outside": {"path": str(external), "node_ids": ["unrelated"]},
    }
    before = deepcopy((nodes, imports))

    result = remove(lifecycle, service, nodes, imports)

    assert not folder.exists()
    assert result.folder_removed and result.pending and not result.error
    assert ids(result.nodes) == {"section", "official", "unrelated"}
    assert result.removed_node_ids == frozenset({"direct", "page", "playlist", "child"})
    assert set(result.imports) == {"outside"}
    assert external.exists()
    assert (nodes, imports) == before
    assert lifecycle.pending
    reopened = MeetingFolderDeletion(
        lifecycle.root, folder, lifecycle.identity, lifecycle.state_dir
    )
    recovered = reopened.reconcile(nodes, imports)
    assert recovered.nodes == result.nodes
    assert recovered.imports == result.imports
    assert reopened.pending
    reopened.acknowledge()
    assert not reopened.pending
    assert not lifecycle.pending


def test_partial_removal_preserves_surviving_sources_and_exposes_error(deletion, tmp_path):
    lifecycle, service, _, folder = deletion
    gone, surviving = folder / "gone.pdf", folder / "surviving.pdf"
    gone.touch()
    surviving.touch()
    nodes = [media("gone-page", tmp_path / "one.jpg"), media("kept-page", tmp_path / "two.jpg")]
    imports = {
        "gone": {"path": str(gone), "node_ids": ["gone-page"]},
        "kept": {"path": str(surviving), "node_ids": ["kept-page"]},
    }

    class PartialStore(WatchedFolderFileStore):
        def delete_folder(self, folder):
            gone.unlink()
            raise PermissionError("provider locked remaining source")

    result = remove(lifecycle, service, nodes, imports, store=PartialStore())

    assert result.error and not result.folder_removed
    assert ids(result.nodes) == {"kept-page"}
    assert set(result.imports) == {"kept"}
    assert lifecycle.pending and surviving.exists()
    recovered = lifecycle.reconcile(nodes, imports)
    assert recovered.error and recovered.nodes == result.nodes
    assert surviving.exists()  # Recovery must never retry deletion.


def test_reopen_after_crash_between_removal_and_snapshot_save(deletion):
    lifecycle, service, _, folder = deletion
    path = folder / "clip.mp4"
    path.touch()
    nodes = [media("clip", path)]

    class Crash(BaseException):
        pass

    class CrashingStore(WatchedFolderFileStore):
        def delete_folder(self, target):
            super().delete_folder(target)
            raise Crash()

    with pytest.raises(Crash):
        remove(lifecycle, service, nodes, store=CrashingStore())
    reopened = MeetingFolderDeletion(
        lifecycle.root, folder, lifecycle.identity, lifecycle.state_dir
    )
    result = reopened.reconcile(nodes, {})
    assert result.folder_removed and not result.nodes and result.pending
    assert not result.error


@pytest.mark.parametrize("marker", ["active", "incomplete"])
def test_active_or_pending_activation_prevents_deletion(deletion, marker):
    lifecycle, service, _, folder = deletion
    store = ActivationStore(folder, "meeting")
    if marker == "active":
        store.publish("a" * 64, service._activation_subject(lifecycle.identity))
    else:
        store.path.parent.mkdir(parents=True)
        store.path.write_text("{", encoding="utf-8")
    with pytest.raises(MeetingFolderDeletionError):
        remove(lifecycle, service)
    assert folder.is_dir() and not lifecycle.pending


def test_in_progress_local_activation_prevents_deletion(deletion):
    lifecycle, service, _, folder = deletion
    entered, release = threading.Event(), threading.Event()

    def activate():
        with service._activation_publication(folder):
            entered.set()
            assert release.wait(5)

    thread = threading.Thread(target=activate)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(MeetingFolderDeletionError):
            remove(lifecycle, service)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and folder.is_dir()


def test_unavailable_root_never_infers_absence_or_prunes_recovery(deletion):
    lifecycle, service, root, folder = deletion
    nodes = [media("clip", folder / "clip.mp4")]
    remove(lifecycle, service, nodes)
    offline = root.with_name("offline")
    root.rename(offline)
    with pytest.raises(MeetingFolderDeletionError):
        remove(lifecycle, service, nodes)
    recovered = lifecycle.reconcile(nodes, {})
    assert recovered.nodes == nodes and recovered.error and recovered.pending
    assert not recovered.folder_removed


@pytest.mark.parametrize("name", ["2026-06-04 MW", "2026-05-28 WE", "Playlist"])
def test_classifier_identity_is_required(deletion, name):
    lifecycle, service, root, _ = deletion
    wrong = root / name
    wrong.mkdir()
    invalid = MeetingFolderDeletion(root, wrong, lifecycle.identity, lifecycle.state_dir)
    with pytest.raises(MeetingFolderDeletionError):
        remove(invalid, service)
    assert wrong.is_dir()


@pytest.mark.parametrize("target", ["root", "outside", "state"])
def test_target_and_intent_storage_are_bounded(deletion, tmp_path, target):
    lifecycle, service, root, folder = deletion
    target_folder = root if target == "root" else folder
    if target == "outside":
        target_folder = tmp_path / folder.name
        target_folder.mkdir()
    state = folder / "state" if target == "state" else lifecycle.state_dir
    invalid = MeetingFolderDeletion(root, target_folder, lifecycle.identity, state)
    with pytest.raises(MeetingFolderDeletionError):
        remove(invalid, service)
    assert target_folder.is_dir()


def test_cancelled_deletion_has_no_durable_intent_or_filesystem_effect(deletion):
    lifecycle, service, _, folder = deletion
    cancellation = CancellationFlag()
    cancellation.set()
    with pytest.raises(MediaOperationCancelled):
        remove(lifecycle, service, cancellation=cancellation)
    assert folder.is_dir() and not lifecycle.pending


def test_intent_must_be_durable_before_file_store_runs(deletion, monkeypatch):
    lifecycle, service, _, folder = deletion
    from solin.core.meetings import folder_lifecycle

    def fail_write(*_args, **_kwargs):
        raise OSError("local disk full")

    monkeypatch.setattr(folder_lifecycle, "write_bytes_atomic", fail_write)
    with pytest.raises(MeetingFolderDeletionError):
        remove(lifecycle, service)
    assert folder.is_dir() and not lifecycle.pending


def test_pending_lookup_does_not_require_shared_root(deletion):
    lifecycle, service, root, folder = deletion
    assert MeetingFolderDeletion.pending_for(lifecycle.state_dir, root, lifecycle.identity) is None
    remove(lifecycle, service)
    root.rename(root.with_name("offline"))
    pending = MeetingFolderDeletion.pending_for(lifecycle.state_dir, root, lifecycle.identity)
    assert pending is not None and pending.folder == folder and pending.pending
    unrelated = MeetingSyncIdentity("different:tree", "mwb", lifecycle.identity.monday)
    assert MeetingFolderDeletion.pending_for(lifecycle.state_dir, root, unrelated) is None


def test_canonical_children_survive_imported_container_removal(deletion, tmp_path):
    lifecycle, service, _, folder = deletion
    source = folder / "list.jwlplaylist"
    source.touch()
    nodes = [
        {
            "id": "imported",
            "type": "subsection",
            "children": [
                media("imported-child", tmp_path / "page.jpg"),
                media("canonical-child", folder / "official.mp4", meeting_generated=True),
            ],
        },
        {"id": "manual-structure", "type": "subsection", "children": []},
    ]
    imports = {"list": {"path": str(source), "node_ids": ["imported"]}}

    result = remove(lifecycle, service, nodes, imports)

    assert ids(result.nodes) == {"imported", "canonical-child", "manual-structure"}
    assert result.removed_node_ids == frozenset({"imported-child"})
    assert not result.imports


def test_local_activation_cannot_start_while_delete_is_running(deletion):
    lifecycle, service, _, folder = deletion
    errors = []

    class CheckingStore(WatchedFolderFileStore):
        def delete_folder(self, target):
            def activate():
                try:
                    with service._activation_publication(folder):
                        errors.append("unexpected activation")
                except MeetingSyncError as exc:
                    errors.append(exc)

            thread = threading.Thread(target=activate)
            thread.start()
            thread.join(5)
            assert not thread.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
            super().delete_folder(target)

    result = remove(lifecycle, service, store=CheckingStore())
    assert result.folder_removed and not result.error


def test_activation_is_rechecked_after_intent_fsync(deletion, monkeypatch):
    lifecycle, service, _, folder = deletion
    from solin.core.meetings import folder_lifecycle

    write = folder_lifecycle.write_bytes_atomic

    def activate_after_write(path, payload):
        write(path, payload)
        ActivationStore(folder, "meeting").publish(
            "a" * 64,
            service._activation_subject(lifecycle.identity),
        )

    monkeypatch.setattr(folder_lifecycle, "write_bytes_atomic", activate_after_write)
    with pytest.raises(MeetingFolderDeletionError):
        remove(lifecycle, service)
    assert folder.is_dir()


@pytest.mark.parametrize("fail", [False, True])
def test_binding_retired_before_removal_and_restored_on_failure(deletion, fail):
    lifecycle, service, _, folder = deletion
    record = service.save_tree(
        folder,
        lifecycle.identity,
        nodes=[],
        deleted_source_keys=set(),
        linked_folder_files={},
        meeting_folder_imports={},
        enable=True,
    )
    document_id = record.snapshot.document_id
    assert document_id is not None
    ActivationStore(folder, "meeting").remove(document_id)
    replica = service._replica(folder)

    class CheckingStore(WatchedFolderFileStore):
        def delete_folder(self, target):
            assert replica.document_id is None
            if fail:
                raise PermissionError("locked")
            super().delete_folder(target)

    result = remove(lifecycle, service, store=CheckingStore())
    if fail:
        assert result.error and replica.document_id == document_id
    else:
        assert not result.error and replica.document_id is None
        assert document_id in replica._lifecycle.binding["retired"]


@pytest.mark.parametrize("method", ["is_symlink", "is_junction"])
@pytest.mark.parametrize("target", ["root", "folder"])
def test_link_or_junction_target_is_rejected(deletion, monkeypatch, target, method):
    lifecycle, service, root, folder = deletion
    redirected = root if target == "root" else folder
    original = getattr(Path, method)
    monkeypatch.setattr(Path, method, lambda path: path == redirected or original(path))
    with pytest.raises(MeetingFolderDeletionError):
        remove(lifecycle, service)
    assert folder.is_dir() and not lifecycle.pending


@pytest.mark.parametrize("method", ["is_symlink", "is_junction"])
def test_partial_recovery_does_not_follow_redirected_source_parent(deletion, monkeypatch, method):
    lifecycle, service, _, folder = deletion
    nested = folder / "nested"
    nested.mkdir()
    source = nested / "clip.mp4"
    source.touch()
    nodes = [media("clip", source)]

    class PartialStore(WatchedFolderFileStore):
        def delete_folder(self, target):
            source.unlink()
            raise PermissionError("partial")

    result = remove(lifecycle, service, nodes, store=PartialStore())
    assert not result.nodes
    original = getattr(Path, method)
    monkeypatch.setattr(Path, method, lambda path: path == nested or original(path))

    recovered = lifecycle.reconcile(nodes, {})

    assert recovered.error and recovered.nodes == nodes
    assert recovered.folder_exists is None and recovered.pending


def test_missing_folder_with_available_root_needs_no_destructive_retry(deletion):
    lifecycle, service, _, folder = deletion
    folder.rmdir()
    nodes = [media("already-gone", folder / "clip.mp4")]
    result = remove(lifecycle, service, nodes)
    assert not result.nodes and result.folder_removed and not result.error


def test_partial_error_is_exposed_even_when_folder_was_completely_removed(deletion):
    lifecycle, service, _, _ = deletion

    class ErrorAfterRemoval(WatchedFolderFileStore):
        def delete_folder(self, target):
            super().delete_folder(target)
            raise OSError("provider reported a failure")

    result = remove(lifecycle, service, store=ErrorAfterRemoval())
    assert result.folder_removed and result.error and result.pending


def test_cancellation_after_durable_intent_keeps_recovery_without_removal(deletion, monkeypatch):
    lifecycle, service, _, folder = deletion
    from solin.core.meetings import folder_lifecycle

    cancellation = CancellationFlag()
    write = folder_lifecycle.write_bytes_atomic

    def cancel_after_write(path, payload):
        write(path, payload)
        cancellation.set()

    monkeypatch.setattr(folder_lifecycle, "write_bytes_atomic", cancel_after_write)
    with pytest.raises(MediaOperationCancelled):
        remove(lifecycle, service, cancellation=cancellation)
    assert folder.is_dir() and lifecycle.pending
    recovered = lifecycle.reconcile([], {})
    assert recovered.folder_exists and recovered.error


def test_reconcile_without_an_intent_preserves_tree_and_does_not_probe_root(deletion, monkeypatch):
    lifecycle, _, _, folder = deletion
    nodes = [media("clip", folder / "clip.mp4")]

    def fail_probe(*_args):
        pytest.fail("Recovery without intent must not probe the shared folder")

    monkeypatch.setattr(lifecycle, "_check_root", fail_probe)
    result = lifecycle.reconcile(nodes, {})
    assert result.nodes == nodes and not result.pending and not result.error
    assert result.folder_exists is None


def test_shared_output_is_preserved_if_an_associated_source_survives(deletion, tmp_path):
    lifecycle, service, _, folder = deletion
    gone, kept = folder / "one.pdf", folder / "two.pdf"
    gone.touch()
    kept.touch()
    nodes = [media("shared-page", tmp_path / "page.jpg")]
    imports = {
        "gone": {"path": str(gone), "node_ids": ["shared-page"]},
        "kept": {"path": str(kept), "node_ids": ["shared-page"]},
    }

    class PartialStore(WatchedFolderFileStore):
        def delete_folder(self, target):
            gone.unlink()
            raise PermissionError("locked")

    result = remove(lifecycle, service, nodes, imports, store=PartialStore())
    assert result.nodes == nodes and not result.removed_node_ids
    assert set(result.imports) == {"kept"} and result.error


def test_empty_exception_message_never_reports_success(deletion):
    lifecycle, service, _, _ = deletion

    class FailedStore(WatchedFolderFileStore):
        def delete_folder(self, target):
            raise PermissionError()

    result = remove(lifecycle, service, store=FailedStore())
    assert result.error and result.folder_exists and result.pending


@pytest.mark.parametrize("partial", [False, True])
def test_late_save_cannot_reactivate_or_recreate_deleted_folder(deletion, partial):
    lifecycle, service, root, folder = deletion
    record = service.save_tree(
        folder,
        lifecycle.identity,
        nodes=[],
        deleted_source_keys=set(),
        linked_folder_files={},
        meeting_folder_imports={},
        enable=True,
    )
    assert record.snapshot.document_id is not None
    ActivationStore(folder, "meeting").remove(record.snapshot.document_id)

    class RemovalStore(WatchedFolderFileStore):
        def delete_folder(self, target):
            if partial:
                raise PermissionError("locked")
            super().delete_folder(target)

    result = remove(lifecycle, service, store=RemovalStore())
    assert bool(result.error) == partial
    with pytest.raises(MeetingSyncError):
        service.save_tree(
            folder,
            lifecycle.identity,
            nodes=[],
            deleted_source_keys=set(),
            linked_folder_files={},
            meeting_folder_imports={},
            base_snapshot=record.snapshot,
        )
    assert service.load_tree(str(root), lifecycle.identity, cleanup_inactive=False) is None
    assert not ActivationStore(folder, "meeting").path.exists()
    assert folder.exists() == partial
    if not partial:
        with pytest.raises((MeetingSyncError, JournalError)):
            service.save_tree(
                folder,
                lifecycle.identity,
                nodes=[],
                deleted_source_keys=set(),
                linked_folder_files={},
                meeting_folder_imports={},
                base_snapshot=record.snapshot,
                stage_only=True,
            )
        assert not folder.exists()


def test_cancellation_after_removal_begins_finishes_without_losing_recovery(deletion):
    lifecycle, service, _, folder = deletion
    cancellation = CancellationFlag()
    nodes = [media("clip", folder / "clip.mp4")]

    class CancelDuringRemoval(WatchedFolderFileStore):
        def delete_folder(self, target):
            cancellation.set()
            super().delete_folder(target)

    result = remove(
        lifecycle, service, nodes, store=CancelDuringRemoval(), cancellation=cancellation
    )
    assert result.folder_removed and not result.error and result.pending
    assert not result.nodes
    assert lifecycle.reconcile(nodes, {}).nodes == []


def test_unavailable_root_during_removal_preserves_tree_until_recovery(deletion):
    lifecycle, service, root, folder = deletion
    path = folder / "clip.mp4"
    path.touch()
    nodes = [media("clip", path)]
    offline = root.with_name("offline")

    class OfflineStore(WatchedFolderFileStore):
        def delete_folder(self, target):
            path.unlink()
            root.rename(offline)
            raise OSError("root went offline")

    result = remove(lifecycle, service, nodes, store=OfflineStore())
    assert result.folder_exists is None and result.nodes == nodes
    assert result.error and result.pending and not root.exists()
    offline.rename(root)
    recovered = lifecycle.reconcile(nodes, {})
    assert not recovered.nodes and recovered.folder_exists and recovered.error


def test_unreadable_root_is_not_treated_as_missing(deletion, monkeypatch):
    lifecycle, service, root, folder = deletion
    remove(lifecycle, service)
    from solin.core.meetings import folder_lifecycle

    original = folder_lifecycle.os.scandir

    def unreadable(path):
        if Path(path) == root:
            raise PermissionError("root cannot be read")
        return original(path)

    monkeypatch.setattr(folder_lifecycle.os, "scandir", unreadable)
    nodes = [media("clip", folder / "clip.mp4")]
    recovered = lifecycle.reconcile(nodes, {})
    assert recovered.nodes == nodes and recovered.folder_exists is None and recovered.error
    assert recovered.pending


@pytest.mark.parametrize("corruption", ["json", "identity", "escape"])
def test_invalid_durable_intent_cannot_prune_or_be_acknowledged(deletion, corruption):
    lifecycle, service, root, folder = deletion
    remove(lifecycle, service, [media("clip", folder / "clip.mp4")])
    path = next(lifecycle.state_dir.glob("*.json"))
    if corruption == "json":
        path.write_text("{", encoding="utf-8")
    else:
        value = json.loads(path.read_bytes())
        if corruption == "identity":
            value["identity"]["tree_key"] = "another-meeting"
        else:
            value["sources"] = {
                str(folder / ".." / ".." / "outside.mp4"): {
                    "node_ids": ["clip"],
                    "import_keys": [],
                },
            }
        path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(MeetingFolderDeletionError):
        MeetingFolderDeletion.pending_for(lifecycle.state_dir, root, lifecycle.identity)
    with pytest.raises(MeetingFolderDeletionError):
        lifecycle.reconcile([], {})
    with pytest.raises(MeetingFolderDeletionError):
        lifecycle.acknowledge()
    assert path.is_file()


def test_recovery_prunes_only_captured_node_identities(deletion):
    lifecycle, service, _, folder = deletion
    original = media("original", folder / "clip.mp4")
    remove(lifecycle, service, [original])
    replacement = media("replacement", folder / "clip.mp4")
    result = lifecycle.reconcile([original, replacement], {})
    assert result.nodes == [replacement]
    assert result.removed_node_ids == frozenset({"original"})


def test_folder_recreated_during_recovery_does_not_report_success(deletion, monkeypatch):
    lifecycle, service, _, folder = deletion
    nodes = [media("clip", folder / "clip.mp4")]
    remove(lifecycle, service, nodes)
    validate = lifecycle._validate_location

    def recreate_after_probe(intent):
        exists = validate(intent)
        if not exists:
            folder.mkdir()
            (folder / "clip.mp4").touch()
        return exists

    monkeypatch.setattr(lifecycle, "_validate_location", recreate_after_probe)
    recovered = lifecycle.reconcile(nodes, {})
    assert recovered.nodes == nodes and recovered.error
    assert recovered.folder_exists is None and recovered.pending
