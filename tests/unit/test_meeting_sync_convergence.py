"""Regressions for meeting replica deletion and cloud delivery ordering."""
from datetime import date
from pathlib import Path
import shutil
import pytest

from solin.core.ingest.meeting_folder_sources import scan_meeting_folder_sources
from solin.core.meetings.linked_folder_sync import (
    MeetingLinkedFolderSync,
    MeetingSyncError,
    MeetingSyncIdentity,
)


def test_identical_source_has_portable_signature(tmp_path):
    left = tmp_path / "left" / "2026-05-25 MW"
    right = tmp_path / "right" / left.name
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / "photo.jpg").write_bytes(b"same image")
    shutil.copy2(left / "photo.jpg", right / "photo.jpg")
    a = scan_meeting_folder_sources(left.parent)[0]["sources"][0]
    b = scan_meeting_folder_sources(right.parent)[0]["sources"][0]
    assert a["signature"] == b["signature"]


def test_stale_terminal_does_not_restore_deleted_manual_node(tmp_path):
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    service = MeetingLinkedFolderSync(lambda _: 0)
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    args = dict(deleted_source_keys=set(), linked_folder_files={}, meeting_folder_imports={})
    initial = service.save_tree(
        folder,
        identity,
        nodes=[{"id": "manual", "type": "media"}],
        expected_revision=0,
        enable=True,
        **args,
    )
    service.save_tree(folder, identity, nodes=[], expected_revision=initial.revision, base_snapshot=initial.snapshot, **args)
    stale = service.save_tree(folder, identity, nodes=initial.nodes, expected_revision=initial.revision, base_snapshot=initial.snapshot, **args)
    assert stale.nodes == []


def _save(service, folder, nodes, base=None):
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    return service.save_tree(
        folder, identity, nodes=nodes, deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={},
        base_snapshot=base.snapshot if base else None,
        enable=base is None,
    )


def _media(node_id, url=""):
    return {"id": node_id, "type": "media", "title": node_id, "media_ref": {"file_path": url}, "children": []}


def _section(node_id, children):
    return {"id": node_id, "type": "section", "children": children}


def test_nested_concurrent_inserts_and_move_have_one_parent(tmp_path):
    from solin.core.meetings.tree_types import iter_nodes
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    base = _save(service, folder, [_section("a", [_section("sub", [_media("old")])]), _section("b", [])])
    _save(service, folder, [_section("a", [_section("sub", [_media("left")])]), _section("b", [_media("old")])], base)
    merged = _save(service, folder, [_section("a", [_section("sub", [_media("old"), _media("right")])]), _section("b", [])], base)
    all_ids = [node["id"] for node in iter_nodes(merged.nodes)]
    assert len(all_ids) == len(set(all_ids))
    assert {"left", "right", "old"}.issubset(all_ids)
    assert merged.nodes[1]["children"][0]["id"] == "old"


def test_late_explicit_placement_absorbs_discovery(tmp_path):
    from solin.core.ingest.sync.discovery import DISCOVERED
    from solin.core.meetings.tree_types import iter_nodes
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    media = folder / "photo.jpg"
    media.write_bytes(b"image")
    empty = _save(service, folder, [_section("treasures", []), _section("lac", [])])
    automatic = {**_media("automatic", str(media)), DISCOVERED: True}
    observed = _save(service, folder, [_section("treasures", []), _section("lac", [automatic])], empty)
    explicit = _media("explicit", str(media))
    result = _save(service, folder, [_section("treasures", [explicit]), _section("lac", [])], empty)
    assert [node["id"] for node in iter_nodes(result.nodes) if node["type"] == "media"] == ["explicit"]
    assert result.nodes[0]["children"][0]["id"] == "explicit"
    assert observed.nodes[1]["children"][0]["id"] == "automatic"
    again = _save(service, folder, result.nodes, result)
    assert again.snapshot.token == result.snapshot.token


def test_edited_fallback_position_survives_late_initial_placement(tmp_path):
    from solin.core.ingest.sync.discovery import DISCOVERED
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    media = folder / "photo.jpg"
    media.write_bytes(b"image")
    empty = _save(service, folder, [_section("treasures", []), _section("lac", [])])
    automatic = {**_media("automatic", str(media)), DISCOVERED: True}
    observed = _save(service, folder, [_section("treasures", []), _section("lac", [automatic])], empty)
    automatic["title"] = "User title"
    _save(service, folder, [_section("treasures", [automatic]), _section("lac", [])], observed)
    result = _save(service, folder, [_section("treasures", []), _section("lac", [_media("explicit", str(media))])], empty)
    assert result.nodes[0]["children"][0]["id"] == "explicit"
    assert result.nodes[0]["children"][0]["title"] == "User title"
    assert result.nodes[1]["children"] == []


def test_import_completion_records_only_nodes_actually_committed(tmp_path):
    from types import SimpleNamespace
    from solin.widgets.meetings.tree_controller import MeetingTreeController
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"image")
    callbacks = []
    recorded = []
    controller = SimpleNamespace(
        _tree_key="meeting", _meeting_folder_imports={}, _sync_enabled=True,
        _meeting_folder_pending_sources={"source"}, _linked_folder_files={},
        _url_for_node=lambda node: node["media_ref"]["file_path"],
        _insert_nodes=lambda _list, _index, _nodes, *, on_completed: callbacks.append(on_completed),
        _record_meeting_folder_import=lambda _source, ids: recorded.append(ids),
    )
    MeetingTreeController._insert_meeting_folder_nodes(
        controller, {"source_key": "source", "path": str(source), "folder_path": str(tmp_path)},
        [_media("new", str(source))], "root", 0,
    )
    assert recorded == []
    callbacks[0]([])
    assert recorded == [[]]


def test_cancelled_import_does_not_mark_source_processed(tmp_path):
    from types import SimpleNamespace
    from solin.widgets.meetings.tree_controller import MeetingTreeController
    callbacks = []
    recorded = []
    controller = SimpleNamespace(
        _tree_key="meeting", _meeting_folder_imports={}, _sync_enabled=True,
        _meeting_folder_pending_sources={"source"}, _linked_folder_files={},
        _url_for_node=lambda node: "",
        _insert_nodes=lambda _list, _index, _nodes, *, on_completed: callbacks.append(on_completed),
        _record_meeting_folder_import=lambda _source, ids: recorded.append(ids),
    )
    MeetingTreeController._insert_meeting_folder_nodes(controller, {"source_key": "source"}, [_media("new")], "root", 0)
    callbacks[0](None)
    assert recorded == []
    assert controller._meeting_folder_pending_sources == set()


def test_tree_noncontainer_parent_falls_back_and_cycles_converge():
    from solin.core.ingest.sync.tree import flatten_nodes, rebuild_nodes, PARENT, ANCESTORS
    from solin.core.meetings.tree_types import iter_nodes
    entities = flatten_nodes([_section("a", [_section("b", [])]), _media("video"), _media("photo")])
    entities["photo"][PARENT] = "video"
    entities["photo"][ANCESTORS] = ["video", "a"]
    entities["a"][PARENT] = "b"
    tree = rebuild_nodes(entities)
    reverse = rebuild_nodes(dict(reversed(list(entities.items()))))
    assert tree == reverse
    assert [node["id"] for node in iter_nodes(tree)].count("photo") == 1
    assert next(node for node in iter_nodes(tree) if node["id"] == "video")["children"] == []
    assert next(node for node in iter_nodes(tree) if node["id"] == "a")["children"][-1]["id"] == "photo"


def test_tree_rejects_duplicate_ids_and_unknown_node_type():
    import pytest
    from solin.core.ingest.sync.tree import flatten_nodes, rebuild_nodes
    with pytest.raises(ValueError, match="Duplicate"):
        flatten_nodes([_media("same"), _section("group", [_media("same")])])
    with pytest.raises(ValueError, match="type"):
        rebuild_nodes({"bad": {"id": "bad"}})


def test_tied_concurrent_positions_do_not_emit_refresh_edits():
    from solin.core.ingest.sync.tree import flatten_nodes, rebuild_nodes, POSITION
    entities = flatten_nodes([_media("a"), _media("b")])
    entities["b"][POSITION] = entities["a"][POSITION]
    assert flatten_nodes(rebuild_nodes(entities), entities) == entities
    desired = [_media("a"), _media("inserted"), _media("b")]
    assert [node["id"] for node in rebuild_nodes(flatten_nodes(desired, entities))] == ["a", "inserted", "b"]


def test_disabling_sync_removes_shared_state_and_rejects_stale_save(tmp_path):
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    cache = folder / ".solin_cache"
    cache.mkdir()
    image = cache / "page.jpg"
    image.write_bytes(b"page")
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    initial = _save(service, folder, [_media("page", str(image))])
    service.deactivate_tree(folder, identity)
    assert not cache.exists()
    assert not (folder / ".solin_sync").exists()
    assert service.load_tree(str(tmp_path), identity) is None
    stale = initial.nodes
    stale[0]["title"] = "Offline title"
    with pytest.raises(MeetingSyncError, match="not active"):
        _save(service, folder, stale, initial)


def test_activation_is_the_only_authority_even_when_old_operations_arrive(tmp_path):
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    active = _save(service, folder, [_media("photo")])
    sync_copy = tmp_path / "delayed-sync"
    shutil.copytree(folder / ".solin_sync", sync_copy)

    service.deactivate_tree(folder, MeetingSyncIdentity(
        "mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25),
    ))
    shutil.copytree(sync_copy, folder / ".solin_sync")

    assert service.load_tree(str(tmp_path), MeetingSyncIdentity(
        "mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25),
    )) is None
    with pytest.raises(MeetingSyncError, match="not active"):
        _save(service, folder, active.nodes, active)


def test_failed_marker_removal_keeps_sync_active_and_restores_publication(tmp_path, monkeypatch):
    from solin.core.ingest.sync.activation import ActivationStore

    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    initial = _save(service, folder, [_media("photo")])

    with monkeypatch.context() as patch:
        patch.setattr(
            ActivationStore,
            "remove",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("locked")),
        )
        with pytest.raises(MeetingSyncError, match="locked"):
            service.deactivate_tree(folder, identity)

    assert service.activation_path(folder).is_file()
    saved = _save(service, folder, [_media("photo"), _media("still-active")], initial)
    assert {node["id"] for node in saved.nodes} == {"photo", "still-active"}


def test_deactivation_succeeds_when_marker_arrives_before_journal(tmp_path):
    from solin.core.ingest.sync.activation import ActivationStore
    from solin.core.meetings.linked_folder_sync import MeetingSyncPending

    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    document_id = "a" * 64
    ActivationStore(folder, "meeting").publish(
        document_id,
        {
            "tree_key": identity.tree_key,
            "pub_type": identity.pub_type,
            "monday": identity.monday.isoformat(),
            "meeting_tag": identity.meeting_tag,
        },
    )

    with pytest.raises(MeetingSyncPending, match="descriptor"):
        service.load_tree(str(tmp_path), identity)

    service.deactivate_tree(folder, identity)

    assert not (folder / ".solin_sync").exists()
    assert service.load_tree(str(tmp_path), identity) is None


def test_cleanup_failure_never_reactivates_and_is_retried(tmp_path, monkeypatch):
    import solin.core.meetings.linked_folder_sync as linked_sync

    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    initial = _save(service, folder, [_media("photo")])
    original_rmtree = linked_sync.shutil.rmtree

    with monkeypatch.context() as patch:
        patch.setattr(
            linked_sync.shutil,
            "rmtree",
            lambda path: (_ for _ in ()).throw(PermissionError(f"locked: {path}")),
        )
        result = service.deactivate_tree(folder, identity)

    assert result.cleanup_errors
    assert not service.activation_path(folder).exists()
    with pytest.raises(MeetingSyncError, match="not active"):
        _save(service, folder, initial.nodes, initial)

    monkeypatch.setattr(linked_sync.shutil, "rmtree", original_rmtree)
    assert service.load_tree(str(tmp_path), identity) is None
    assert not (folder / ".solin_sync").exists()


def test_received_deactivation_detaches_cache_before_internal_cleanup(tmp_path):
    from solin.core.ingest.sync.activation import ActivationStore

    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    cache = folder / ".solin_cache"
    cache.mkdir(parents=True)
    image = cache / "official.jpg"
    image.write_bytes(b"image")
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    initial = _save(service, folder, [_media("official", str(image))])
    activation = ActivationStore(folder, "meeting").read()
    assert activation is not None
    ActivationStore(folder, "meeting").remove(activation.document_id)

    assert service.load_tree(str(tmp_path), identity, cleanup_inactive=False) is None
    assert service._replica(folder).document_id == initial.snapshot.document_id
    assert image.is_file()
    detached = service.detach_cache_references(
        initial.nodes,
        folder,
        tmp_path / "durable",
    )
    cleanup = service.cleanup_inactive_tree(folder)

    detached_path = Path(detached[0]["media_ref"]["file_path"])
    assert cleanup.cleanup_errors == ()
    assert service._replica(folder).document_id is None
    assert detached_path.read_bytes() == b"image"
    assert not cache.exists()
    assert not (folder / ".solin_sync").exists()


def test_delayed_legacy_manifest_cannot_reactivate_a_known_document(tmp_path):
    import json
    from solin.core.ingest.sync.activation import ActivationStore

    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    initial = _save(service, folder, [_media("old")])
    activation = ActivationStore(folder, "meeting").read()
    assert activation is not None
    ActivationStore(folder, "meeting").remove(activation.document_id)
    (folder / "_solin_manifest.json").write_text(
        json.dumps({
            "version": 1,
            "meeting_tree": {
                "tree_key": identity.tree_key,
                "pub_type": identity.pub_type,
                "monday": identity.monday.isoformat(),
                "meeting_tag": identity.meeting_tag,
                "nodes": initial.nodes,
            },
        }),
        encoding="utf-8",
    )

    restarted = MeetingLinkedFolderSync(lambda _: 0)
    assert restarted.load_tree(str(tmp_path), identity, cleanup_inactive=False) is None
    assert not restarted.activation_path(folder).exists()


def test_reactivation_uses_new_document_and_ignores_delayed_old_operations(tmp_path):
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    previous = _save(service, folder, [_media("old")])
    delayed = tmp_path / "delayed"
    shutil.copytree(folder / ".solin_sync", delayed)

    service.deactivate_tree(folder, identity)
    current = _save(service, folder, [_media("new")])
    assert current.snapshot.document_id != previous.snapshot.document_id
    for directory in ("documents", "operations"):
        shutil.copytree(
            delayed / "meeting" / directory,
            folder / ".solin_sync" / "meeting" / directory,
            dirs_exist_ok=True,
        )

    loaded = service.load_tree(str(tmp_path), identity)
    assert loaded is not None
    assert [node["id"] for node in loaded.nodes] == ["new"]


def test_pending_cleanup_never_removes_a_concurrent_reactivation(tmp_path):
    from solin.core.ingest.sync.activation import ActivationStore

    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    first = _save(service, folder, [_media("old")])
    activation = ActivationStore(folder, "meeting").read()
    assert activation is not None
    ActivationStore(folder, "meeting").remove(activation.document_id)
    assert service.load_tree(str(tmp_path), identity, cleanup_inactive=False) is None

    replacement = _save(service, folder, [_media("new")])
    with pytest.raises(MeetingSyncError, match="activated"):
        service.cleanup_inactive_tree(folder)

    assert replacement.snapshot.document_id != first.snapshot.document_id
    loaded = service.load_tree(str(tmp_path), identity)
    assert loaded is not None
    assert [node["id"] for node in loaded.nodes] == ["new"]


def test_unavailable_folder_preserves_known_causal_state_and_sync_intent():
    from types import SimpleNamespace
    from solin.core.ingest.sync.journal import ReplicaSnapshot
    from solin.widgets.meetings.tree_controller import MeetingTreeController, _MeetingSyncDiscovery
    signals = []
    timer = []
    snapshot = ReplicaSnapshot({"photo": _media("photo")})
    controller = SimpleNamespace(
        _sync_enabled=True, _sync_folder="linked", _sync_revision=7,
        _sync_snapshot=snapshot,
        syncStateChanged=SimpleNamespace(emit=lambda: signals.append(True)),
        _sync_retry_timer=SimpleNamespace(start=lambda delay: timer.append(delay)),
    )
    MeetingTreeController._apply_sync_discovery(controller, _MeetingSyncDiscovery(False))
    assert controller._sync_enabled
    assert controller._sync_folder == "linked"
    assert controller._sync_snapshot is snapshot
    assert controller._sync_transport_pending
    assert timer


@pytest.mark.parametrize("restart", [False, True])
def test_local_stage_does_not_touch_cloud_paths(tmp_path, monkeypatch, restart):
    from pathlib import Path
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    image = folder / "photo.jpg"
    image.write_bytes(b"photo")
    base = _save(service, folder, [_media("photo", str(image))])
    if restart:
        service = MeetingLinkedFolderSync(lambda _: 0)
    stat = Path.stat
    resolve = Path.resolve

    def assert_local_stat(path, *args, **kwargs):
        assert not path.is_relative_to(folder), f"GUI accessed cloud path {path}"
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", assert_local_stat)

    def assert_local_resolve(path, *args, **kwargs):
        assert not path.is_relative_to(folder), f"GUI resolved cloud path {path}"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", assert_local_resolve)
    nodes = base.nodes
    nodes[0]["title"] = "User edit"
    nodes[0]["thumbnail_local_path"] = str(folder / ".solin_cache" / "thumb.jpg")
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    staged = service.save_tree(
        folder, identity, nodes=nodes, deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={},
        base_snapshot=base.snapshot, stage_only=True,
    )
    assert staged.nodes[0]["title"] == "User edit"


def test_corrupt_advisory_snapshot_does_not_block_journal_or_get_reimported(tmp_path):
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    base = _save(service, folder, [_media("photo")])
    advisory = folder / ".solin_sync" / "meeting" / "snapshot.json"
    advisory.write_text("{broken", encoding="utf-8")
    legacy = folder / "_solin_manifest.json"
    legacy.write_text("{also broken", encoding="utf-8")
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    loaded = service.load_tree(str(tmp_path), identity)
    assert loaded.snapshot.token == base.snapshot.token
    saved = _save(service, folder, loaded.nodes, loaded)
    assert saved.snapshot.token == loaded.snapshot.token
    import json
    assert json.loads(advisory.read_text(encoding="utf-8"))["journal_token"] == saved.snapshot.token


def test_resource_removal_retries_on_load_without_reimport(tmp_path, monkeypatch):
    import solin.core.meetings.linked_folder_sync as module
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    image = folder / "photo.jpg"
    image.write_bytes(b"photo")
    base = _save(service, folder, [_media("photo", str(image))])
    retire = module.retire_file

    def locked(*_args, **_kwargs):
        raise PermissionError("cloud lock")

    monkeypatch.setattr(module, "retire_file", locked)
    deleted = _save(service, folder, [], base)
    assert deleted.nodes == []
    assert deleted.resource_error
    assert image.exists()
    monkeypatch.setattr(module, "retire_file", retire)
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    loaded = service.load_tree(str(tmp_path), identity)
    assert loaded.nodes == []
    assert not loaded.resource_error
    assert not image.exists()
    assert loaded.snapshot.token == deleted.snapshot.token


def test_pending_publish_keeps_retry_for_background_meeting(tmp_path):
    import os
    from dataclasses import replace
    from types import SimpleNamespace
    from solin.widgets.meetings.tree_controller import MeetingTreeController, _PendingSyncSave
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    initial = _save(service, folder, [_media("initial")])
    remote = _save(service, folder, [_media("initial"), _media("remote")], initial)
    pending = replace(remote, pending_count=1)
    identity = MeetingSyncIdentity(initial.tree_key, initial.pub_type, initial.monday)
    request = _PendingSyncSave(
        folder=folder, identity=identity, nodes=initial.nodes,
        deleted_source_keys=set(), linked_folder_files={}, meeting_folder_imports={},
        canonical_reset_generation=0, hidden_canonical_media={}, expected_revision=initial.revision,
        next_attempt_at=0, snapshot=initial.snapshot,
    )
    key = os.path.normcase(os.path.abspath(folder))
    controller = SimpleNamespace(
        _pending_sync_saves={key: request}, _sync_folder=str(tmp_path / "another-meeting"),
        _sync_identity=None, _arm_sync_save_timer=lambda: None,
        syncStateChanged=SimpleNamespace(emit=lambda: None),
    )
    MeetingTreeController._on_sync_save_completed(controller, key, request.generation, pending, None)
    assert key in controller._pending_sync_saves
    assert request.snapshot.token == remote.snapshot.token
    assert request.nodes == remote.nodes
    retried = service.save_tree(
        folder, identity, nodes=request.nodes, deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={}, base_snapshot=request.snapshot,
    )
    assert retried.snapshot.token == remote.snapshot.token
    MeetingTreeController._on_sync_save_completed(controller, key, request.generation, retried, None)
    assert not controller._pending_sync_saves


def test_disabled_publication_retry_does_not_replace_detached_local_edits(tmp_path):
    import os
    from types import SimpleNamespace
    from solin.widgets.meetings.tree_controller import MeetingTreeController, _PendingSyncSave
    from solin.core.meetings.linked_folder_sync import MeetingSyncInactive
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    initial = _save(service, folder, [_media("shared")])
    identity = MeetingSyncIdentity(initial.tree_key, initial.pub_type, initial.monday)
    service.deactivate_tree(folder, identity)
    request = _PendingSyncSave(
        folder=folder, identity=identity, nodes=initial.nodes,
        deleted_source_keys=set(), linked_folder_files={}, meeting_folder_imports={},
        canonical_reset_generation=0, hidden_canonical_media={}, expected_revision=initial.revision,
        next_attempt_at=0, snapshot=initial.snapshot,
    )
    key = os.path.normcase(os.path.abspath(folder))
    local_nodes = [_media("local-edit")]
    refreshes = []
    controller = SimpleNamespace(
        _pending_sync_saves={key: request}, _sync_folder=str(folder),
        _sync_identity=identity, _nodes=local_nodes, _arm_sync_save_timer=lambda: None,
        _request_sync_refresh=lambda: refreshes.append(True),
        syncStateChanged=SimpleNamespace(emit=lambda: None),
    )
    MeetingTreeController._on_sync_save_completed(
        controller,
        key,
        request.generation,
        None,
        MeetingSyncInactive("deactivated"),
    )
    assert key not in controller._pending_sync_saves
    assert controller._nodes is local_nodes
    assert refreshes


def test_interrupted_migration_never_exposes_a_partial_backup(tmp_path, monkeypatch):
    import json
    import os
    import pytest
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    raw = json.dumps({"version": 1, "meeting_tree": {
        "tree_key": identity.tree_key, "schema_version": 3, "nodes": [_media("saved")],
    }}).encode()
    (folder / "_solin_manifest.json").write_bytes(raw)

    def fail_replace(_source, _destination):
        raise OSError("interrupted backup")

    with monkeypatch.context() as blocked:
        blocked.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="interrupted backup"):
            MeetingLinkedFolderSync(lambda _: 0).load_tree(str(tmp_path), identity)
        assert not list(folder.glob(".solin_sync/meeting/migration/*.json"))
    MeetingLinkedFolderSync(lambda _: 0).load_tree(str(tmp_path), identity)
    backups = list(folder.rglob("*pre-journal*.json")) + list(folder.glob(".solin_sync/meeting/migration/*.json"))
    assert backups
    assert all(path.read_bytes() == raw for path in backups)


def test_remote_migration_does_not_hide_incompatible_local_baseline(tmp_path):
    import json
    import pytest
    from solin.core.meetings.linked_folder_sync import MeetingSyncError
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    left = tmp_path / "left" / "2026-05-25 MW"
    right = tmp_path / "right" / left.name
    for folder, title in ((left, "left"), (right, "right")):
        folder.mkdir(parents=True)
        (folder / "_solin_manifest.json").write_text(json.dumps({
            "version": 1, "meeting_tree": {
                "tree_key": identity.tree_key, "schema_version": 3,
                "nodes": [{**_media("same"), "title": title}],
            },
        }), encoding="utf-8")
    MeetingLinkedFolderSync(lambda _: 0).load_tree(str(left.parent), identity)
    shutil.copytree(left / ".solin_sync", right / ".solin_sync")
    with pytest.raises(MeetingSyncError, match="baseline"):
        MeetingLinkedFolderSync(lambda _: 0).load_tree(str(right.parent), identity)


def test_cancelled_published_copy_remains_recoverable_for_unknown_remote_reference(tmp_path):
    from solin.core.ingest.sync.resources import recover_file
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    copied = folder / "photo.jpg"
    copied.write_bytes(b"photo")
    service.rollback_materialized_files(folder, [copied])
    assert not copied.exists()
    assert recover_file(folder, "photo.jpg")
    assert copied.read_bytes() == b"photo"
    _save(service, folder, [_media("remote-ref", str(copied))])
    service.rollback_materialized_files(folder, [copied])
    assert copied.read_bytes() == b"photo"


def test_received_disable_is_persisted_with_its_causal_snapshot(tmp_path):
    from types import SimpleNamespace
    from solin.core.ingest.sync.journal import ReplicaSnapshot
    from solin.widgets.meetings.tree_controller import MeetingTreeController, _MeetingSyncDiscovery
    service = MeetingLinkedFolderSync(lambda _: 0)
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    initial = _save(service, folder, [_media("shared")])
    identity = MeetingSyncIdentity(initial.tree_key, initial.pub_type, initial.monday)
    service.deactivate_tree(folder, identity)
    received = MeetingLinkedFolderSync(lambda _: 0).load_tree(str(tmp_path), identity)
    assert received is None
    persisted = []
    scans = []
    local_nodes = [_media("local")]
    controller = SimpleNamespace(
        _sync_enabled=True, _sync_folder=str(folder), _sync_snapshot=initial.snapshot,
        _sync_identity=identity, _nodes=local_nodes,
        _pending_sync_saves={}, _sync_revision=initial.revision,
        _sync_transport_pending=True, _sync_busy_message="pending",
        _sync_root=str(tmp_path), _arm_sync_save_timer=lambda: None,
        inject_linked_folder_media=lambda root: scans.append(root),
        _save_local_cache=lambda: persisted.append(True),
        chromeChanged=SimpleNamespace(emit=lambda: None),
        syncStateChanged=SimpleNamespace(emit=lambda: None),
        stateChanged=SimpleNamespace(emit=lambda: None),
    )
    MeetingTreeController._apply_sync_discovery(
        controller,
        _MeetingSyncDiscovery(True, folder, received, active=False),
    )
    assert not controller._sync_enabled
    assert controller._sync_snapshot == ReplicaSnapshot()
    assert controller._nodes is local_nodes
    assert persisted
    assert scans == [str(tmp_path)]


def test_migration_backup_matches_parsed_bytes_during_manifest_replacement(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    folder = tmp_path / "2026-05-25 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("mwb:2026-05-25:T:issue", "mwb", date(2026, 5, 25))
    manifest = folder / "_solin_manifest.json"
    payload = {"version": 1, "meeting_tree": {
        "tree_key": identity.tree_key, "schema_version": 3, "nodes": [_media("original")],
    }}
    original = json.dumps(payload).encode()
    manifest.write_bytes(original)
    payload["meeting_tree"]["nodes"] = [_media("replacement")]
    replacement = json.dumps(payload).encode()
    original_open = Path.open
    replaced = False

    class ReplaceAfterReading:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self.stream.__enter__()

        def __exit__(self, *args):
            nonlocal replaced
            self.stream.__exit__(*args)
            if not replaced:
                replaced = True
                manifest.write_bytes(replacement)

    def replacing_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path == manifest and "r" in mode:
            return ReplaceAfterReading(stream)
        return stream

    monkeypatch.setattr(Path, "open", replacing_open)
    service = MeetingLinkedFolderSync(lambda _: 0)
    monkeypatch.setattr(service, "locate_folder", lambda *_args, **_kwargs: folder)
    loaded = service.load_tree(str(tmp_path), identity)
    assert loaded.nodes[0]["id"] == "original"
    assert not manifest.exists()
    backups = list(folder.glob(".solin_sync/meeting/migration/*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
