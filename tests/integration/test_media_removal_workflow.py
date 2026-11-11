"""Removal contracts shared by single, scoped and global commands."""

from concurrent.futures import Future
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QMessageBox

from solin.controllers.snapshot_write_coordinator import SnapshotWriteCoordinator
from solin.core.ingest.sync.journal import ReplicaSnapshot
from solin.core.meetings.tree_types import iter_nodes
from solin.core.meetings.linked_folder_sync import (
    MeetingLinkedFolderSync, MeetingSyncError, MeetingSyncIdentity,
)
from solin.core.meetings.tree_store import MeetingTreeStore
from solin.core.media.removal import select_media_ids
from solin.ui.dialogs.media_removal import ContainerRemovalChoice
from solin.widgets.meetings.tree_controller import MeetingTreeController
from solin.widgets.playlist.widget import PlaylistEditView


def _media(node_id, *, official=False):
    node = {"id": node_id, "type": "media", "children": [], "meeting_generated": official}
    if official:
        node["meeting_source_key"] = f"media:{node_id}"
    return node


@pytest.fixture
def meeting():
    controller = SimpleNamespace(
        _tree_key="test", _tree_session=SimpleNamespace(generation=1),
        _nodes=[_media("outside"), {
            "id": "section", "type": "section", "title": "Main",
            "meeting_generated": True, "meeting_source_key": "section:main",
            "children": [_media("official", official=True), _media("manual"), {
                "id": "sub", "type": "subsection", "title": "Nested",
                "children": [{"id": "marker", "type": "marker"}, _media("nested")],
            }],
        }],
        _deleted_source_keys=set(), _hidden_canonical_media={},
        _linked_folder_files={}, _meeting_folder_imports={},
        _sync_snapshot=ReplicaSnapshot(), _deferred_media_cleanup=[],
        _sync_binding_writes={},
        _unstaged_sync_edits=set(),
        _sync_recovery_bases={},
        writes=[], notifications=[], cancellations=[], cleanup=[], parent=lambda: None,
    )
    controller._find_node = lambda node_id: next(
        (node for node in iter_nodes(controller._nodes) if node["id"] == node_id), None,
    )
    controller._save = lambda **_kwargs: controller.writes.append(deepcopy(controller._nodes)) or True
    controller._snapshot_storage_key = lambda: "meeting:test"
    controller._emit_section_counts = lambda: controller.notifications.append(True)
    controller._cancel_media_info_requests_for_item = controller.cancellations.append
    controller._remember_deleted_sources = lambda node, **kwargs: (
        MeetingTreeController._remember_deleted_sources(controller, node, **kwargs)
    )
    controller._cleanup_meeting_folder_import_for_removed_node = lambda *args, **kwargs: (
        MeetingTreeController._cleanup_meeting_folder_import_for_removed_node(
            controller, *args, **kwargs,
        )
    )
    controller._url_for_node = lambda node: node.get("media_ref", {}).get("file_path", "")
    controller._queue_linked_media_removal = lambda *args: controller.cleanup.append(args)
    return controller


@pytest.fixture
def synced_meeting(meeting, tmp_path):
    folder = tmp_path / "2026-11-02 MW"
    folder.mkdir()
    meeting._tree_key = "mwb:2026-11-02:en:2026-11"
    identity = MeetingSyncIdentity(meeting._tree_key, "mwb", date(2026, 11, 2))
    service = MeetingLinkedFolderSync(lambda _pub_type: 0)
    initial = service.save_tree(
        folder, identity, nodes=deepcopy(meeting._nodes), deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={}, enable=True,
    )
    meeting._sync_enabled = True
    meeting._sync_folder = str(folder)
    meeting._sync_identity = identity
    meeting._sync_snapshot = initial.snapshot
    meeting._sync_revision = initial.revision
    meeting._sync_service = service
    meeting._pending_sync_saves = {}
    meeting._image_framing_save_timer = SimpleNamespace(stop=lambda: None)
    meeting._save_local_cache = lambda **_kwargs: True
    meeting._schedule_sync_manifest_save = lambda **kwargs: (
        MeetingTreeController._schedule_sync_manifest_save(meeting, **kwargs)
    )
    meeting._arm_sync_save_timer = lambda: None
    meeting.syncStateChanged = SimpleNamespace(emit=lambda: None)
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *_args: None)
    meeting._save = lambda **kwargs: MeetingTreeController._save(meeting, **kwargs)
    return meeting, identity


def _attach_meeting_store(meeting, store):
    snapshots = SnapshotWriteCoordinator(debounce_ms=100)
    meeting._store = store
    meeting._media_tree_runtime = SimpleNamespace(snapshots=snapshots)
    meeting._canonical_hash = "hash"
    meeting._current_overview = lambda: None
    meeting._snapshot_storage_key = lambda tree_key=None: (
        MeetingTreeController._snapshot_storage_key(meeting, tree_key)
    )
    meeting._save_local_cache = lambda **kwargs: (
        MeetingTreeController._save_local_cache(meeting, **kwargs)
    )
    return snapshots


@pytest.mark.parametrize("scope, removed", [
    ("root", {"outside", "manual", "nested"}),
    ("section:section", {"manual", "nested"}),
    ("subsection:sub", {"nested"}),
    ("section:missing", set()),
])
def test_meeting_additional_removal_preserves_official_media_and_structure(meeting, scope, removed):
    result = MeetingTreeController.remove_additional_media(meeting, scope)
    assert set(result.removed_ids) == removed
    assert set(select_media_ids(meeting._nodes)) == {"outside", "official", "manual", "nested"} - removed
    assert meeting._deleted_source_keys == set()
    assert meeting._hidden_canonical_media == {}
    assert meeting._find_node("sub") is not None
    assert meeting._find_node("marker") is not None
    assert len(meeting.writes) == int(bool(removed))
    assert len(meeting.notifications) == int(bool(removed))


def test_delete_container_with_media_hides_official_and_removes_manual_structure(meeting, monkeypatch):
    monkeypatch.setattr(
        "solin.widgets.meetings.tree_controller.confirm_container_removal",
        lambda *_args, **_kwargs: ContainerRemovalChoice.DELETE_MEDIA,
    )
    MeetingTreeController.deleteSection(meeting, "section")
    assert select_media_ids(meeting._nodes) == ("outside",)
    assert meeting._find_node("section") is None
    assert meeting._deleted_source_keys == {"section:main", "media:official"}
    assert meeting._hidden_canonical_media["media:official"]["id"] == "official"
    assert len(meeting.writes) == len(meeting.notifications) == 1


def test_meeting_container_removal_preserves_active_media_and_commits_once(meeting, monkeypatch):
    meeting._find_node("manual")["media_ref"] = {"file_path": "C:/video/live.mp4"}
    meeting._active_media_paths_provider = lambda: ("C:/video/live.mp4",)
    warnings = []
    monkeypatch.setattr(
        "solin.widgets.meetings.tree_controller.confirm_container_removal",
        lambda *_args, **_kwargs: ContainerRemovalChoice.DELETE_MEDIA,
    )
    monkeypatch.setattr(
        "solin.widgets.meetings.tree_controller.warn_playing_media_removal",
        lambda _parent, titles: warnings.append(titles),
    )

    MeetingTreeController.deleteSection(meeting, "section")

    assert select_media_ids(meeting._nodes) == ("outside", "manual")
    assert meeting._find_node("section") is None
    assert meeting._hidden_canonical_media["media:official"]["id"] == "official"
    assert len(meeting.writes) == len(meeting.notifications) == 1
    assert len(warnings) == 1 and "live.mp4" in warnings[0]


def test_meeting_container_removal_with_only_active_media_commits_structure(meeting, monkeypatch):
    meeting._find_node("nested")["media_ref"] = {"file_path": "C:/video/live.mp4"}
    meeting._active_media_paths_provider = lambda: ("C:/video/live.mp4",)
    warnings = []
    monkeypatch.setattr(
        "solin.widgets.meetings.tree_controller.confirm_container_removal",
        lambda *_args, **_kwargs: ContainerRemovalChoice.DELETE_MEDIA,
    )
    monkeypatch.setattr(
        "solin.widgets.meetings.tree_controller.warn_playing_media_removal",
        lambda _parent, titles: warnings.append(titles),
    )

    MeetingTreeController.deleteSection(meeting, "sub")

    assert meeting._find_node("sub") is None
    assert meeting._find_node("nested") is not None
    assert len(meeting.writes) == len(meeting.notifications) == 1
    assert len(warnings) == 1


def test_rejected_meeting_save_rolls_back_before_cleanup(meeting):
    before = deepcopy(meeting._nodes)
    meeting._save = lambda **_kwargs: False
    result = MeetingTreeController._remove_media_ids(meeting, {"official", "manual"})
    assert result.failed_ids == ("official", "manual")
    assert meeting._nodes == before
    assert meeting._deleted_source_keys == set()
    assert meeting._hidden_canonical_media == {}
    assert meeting.cancellations == meeting.notifications == meeting.cleanup == []


def test_rejected_meeting_local_snapshot_keeps_durable_sync_deletion(meeting, tmp_path):
    before = deepcopy(meeting._nodes)
    folder = tmp_path / "2026-11-02 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("test", "mwb", date(2026, 11, 2))
    service = MeetingLinkedFolderSync(lambda _pub_type: 0)
    initial = service.save_tree(
        folder, identity, nodes=deepcopy(before), deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={}, enable=True,
    )
    journal_path = folder / ".solin_sync" / "meeting" / "snapshot.json"
    journal_before = journal_path.read_bytes()
    snapshots = SnapshotWriteCoordinator()
    snapshots.shutdown()
    baseline = initial.snapshot
    meeting._sync_enabled = True
    meeting._sync_folder = str(folder)
    meeting._sync_identity = identity
    meeting._sync_snapshot = baseline
    meeting._sync_revision = initial.revision
    meeting._pending_sync_saves = {}
    meeting._sync_service = service
    meeting._image_framing_save_timer = SimpleNamespace(stop=lambda: None)
    meeting._store = SimpleNamespace(path=tmp_path / "trees.json")
    meeting._canonical_hash = ""
    meeting._current_overview = lambda: None
    meeting._snapshot_storage_key = lambda tree_key=None: (
        MeetingTreeController._snapshot_storage_key(meeting, tree_key)
    )
    meeting._media_tree_runtime = SimpleNamespace(snapshots=snapshots)
    meeting._save_local_cache = lambda: MeetingTreeController._save_local_cache(meeting)
    meeting._schedule_sync_manifest_save = lambda **kwargs: (
        MeetingTreeController._schedule_sync_manifest_save(meeting, **kwargs)
    )
    meeting._arm_sync_save_timer = lambda: None
    meeting.syncStateChanged = SimpleNamespace(emit=lambda: None)
    storage_failures = []
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *args: storage_failures.append(args))
    meeting._save = lambda **kwargs: MeetingTreeController._save(meeting, **kwargs)

    result = MeetingTreeController.remove_additional_media(meeting)

    assert set(result.removed_ids) == {"outside", "manual", "nested"}
    assert set(select_media_ids(meeting._nodes)) == {"official"}
    assert meeting._local_edit_revision == 1
    assert len(meeting._pending_sync_saves) == 1
    assert meeting._sync_snapshot != baseline
    assert journal_path.read_bytes() == journal_before
    # Recovery must work even if the local queue was never written and its
    # in-memory retry request disappears on process exit.
    persisted = MeetingLinkedFolderSync(lambda _pub_type: 0).load_tree(str(tmp_path), identity)
    assert persisted is not None
    assert set(select_media_ids(persisted.nodes)) == {"official"}
    assert not meeting._store.path.exists()
    assert storage_failures == []
    assert set(meeting.cancellations) == {"outside", "manual", "nested"}
    assert len(meeting.notifications) == 1


def test_local_cache_exception_after_staging_does_not_undo_meeting_removal(
    synced_meeting, tmp_path,
):
    meeting, identity = synced_meeting

    def unavailable_cache():
        raise OSError("cache unavailable")

    meeting._save_local_cache = unavailable_cache

    result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

    assert result.removed_ids == ("nested",)
    assert meeting._find_node("nested") is None
    assert len(meeting._pending_sync_saves) == 1
    reloaded = MeetingLinkedFolderSync(lambda _pub_type: 0).load_tree(str(tmp_path), identity)
    assert reloaded is not None
    assert "nested" not in {node["id"] for node in iter_nodes(reloaded.nodes)}


def test_meeting_removal_stages_sync_before_local_cache(meeting):
    events = []
    meeting._sync_enabled = True
    meeting._sync_snapshot = "previous"
    meeting._image_framing_save_timer = SimpleNamespace(stop=lambda: None)
    meeting._save_local_cache = lambda: events.append(("local", meeting._sync_snapshot)) or True

    def stage(**_kwargs):
        events.append(("sync", meeting._sync_snapshot))
        meeting._sync_snapshot = "staged"
        return True

    meeting._schedule_sync_manifest_save = stage
    meeting._save = lambda **kwargs: MeetingTreeController._save(meeting, **kwargs)

    result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

    assert result.removed_ids == ("nested",)
    assert events == [("sync", "previous"), ("local", "staged")]


@pytest.mark.parametrize("error_type", [MeetingSyncError, OSError])
def test_meeting_removal_sync_stage_error_rolls_back_unstaged_edit(meeting, error_type):
    events = []
    failures = []
    meeting._sync_enabled = True
    meeting._sync_snapshot = "previous"
    meeting._image_framing_save_timer = SimpleNamespace(stop=lambda: None)
    meeting._save_local_cache = lambda: events.append("local") or True
    meeting._arm_sync_save_timer = lambda: events.append("retry")
    meeting.syncStateChanged = SimpleNamespace(emit=lambda: events.append("signal"))
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *args: failures.append(args))

    def fail_stage(**_kwargs):
        raise error_type("temporary sync journal failure")

    meeting._schedule_sync_manifest_save = fail_stage
    meeting._save = lambda **kwargs: MeetingTreeController._save(meeting, **kwargs)

    result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

    assert result.failed_ids == ("nested",)
    assert meeting._find_node("nested") is not None
    assert events == []
    assert len(failures) == 1


def test_failed_meeting_removal_rearms_pending_image_framing(meeting):
    timer_starts = []
    meeting._find_node("outside")["image_framing"] = {"zoom": 1.3}
    meeting._sync_enabled = True
    meeting._image_framing_save_pending = True
    meeting._image_framing_save_timer = SimpleNamespace(
        stop=lambda: None, start=lambda: timer_starts.append(True),
    )
    meeting._schedule_image_framing_save = lambda: (
        MeetingTreeController._schedule_image_framing_save(meeting)
    )

    def journal_unavailable(**_kwargs):
        raise OSError("journal unavailable")

    meeting._schedule_sync_manifest_save = journal_unavailable
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *_args: None)
    meeting._save = lambda **kwargs: MeetingTreeController._save(meeting, **kwargs)

    result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

    assert result.failed_ids == ("nested",)
    assert meeting._find_node("nested") is not None
    assert meeting._image_framing_save_pending is True
    assert timer_starts == [True]

    saved_nodes = []
    meeting._schedule_sync_manifest_save = lambda **_kwargs: True
    meeting._save_local_cache = lambda: saved_nodes.append(deepcopy(meeting._nodes)) or True
    assert MeetingTreeController._flush_image_framing_save(meeting)
    assert next(node for node in iter_nodes(saved_nodes[0]) if node["id"] == "outside")[
        "image_framing"
    ] == {"zoom": 1.3}


def test_nonremoval_sync_stage_failure_retains_and_retries_save(synced_meeting, monkeypatch):
    meeting, identity = synced_meeting
    service = meeting._sync_service
    original_save_tree = service.save_tree
    meeting._find_node("manual")["title"] = "Edited while offline"

    def stage_unavailable(*args, **kwargs):
        if kwargs.get("stage_only"):
            raise OSError("journal temporarily unavailable")
        return original_save_tree(*args, **kwargs)

    monkeypatch.setattr(service, "save_tree", stage_unavailable)
    events = []
    meeting._sync_save_inflight = None
    meeting._cancel_sync_refresh = lambda: events.append("cancel-refresh")
    meeting._sync_save_timer = SimpleNamespace(
        start=lambda *_args: events.append("retry-timer"), stop=lambda: None,
    )
    meeting._arm_sync_save_timer = lambda: MeetingTreeController._arm_sync_save_timer(meeting)
    meeting.syncStateChanged = SimpleNamespace(emit=lambda: events.append("state-changed"))

    with pytest.raises(OSError, match="journal temporarily unavailable"):
        MeetingTreeController._schedule_sync_manifest_save(meeting, delay_ms=0)

    assert len(meeting._pending_sync_saves) == 1
    assert events == ["cancel-refresh", "retry-timer", "state-changed"]
    monkeypatch.setattr(service, "save_tree", original_save_tree)

    def run_immediately(task, *args):
        future = Future()
        future.set_result(task(*args))
        return future

    meeting._sync_save_executor = SimpleNamespace(submit=run_immediately)
    meeting._media_tree_runtime = SimpleNamespace(
        resource_lanes=SimpleNamespace(run=lambda _claim, callback: callback()),
    )
    meeting._apply_sync_record = lambda record: setattr(meeting, "_sync_revision", record.revision)
    meeting._syncSaveCompleted = SimpleNamespace(
        emit=lambda *args: MeetingTreeController._on_sync_save_completed(meeting, *args),
    )
    meeting._emit_sync_save_completed = lambda *args: (
        MeetingTreeController._emit_sync_save_completed(meeting, *args)
    )
    MeetingTreeController._drain_sync_manifest_saves(meeting)

    assert meeting._pending_sync_saves == {}
    reloaded = service.load_tree(str(Path(meeting._sync_folder).parent), identity)
    assert reloaded is not None
    assert next(node for node in iter_nodes(reloaded.nodes) if node["id"] == "manual")[
        "title"
    ] == "Edited while offline"


def test_rejected_synced_insertion_cannot_be_retried(synced_meeting, monkeypatch):
    meeting, identity = synced_meeting
    meeting._parse_list_id = lambda value: MeetingTreeController._parse_list_id(meeting, value)
    meeting._children_for_target = lambda kind, node_id: (
        MeetingTreeController._children_for_target(meeting, kind, node_id)
    )
    meeting._start_media_requests = lambda *_args: None
    meeting._sync_save_inflight = None
    MeetingTreeController._schedule_sync_manifest_save(meeting)
    previously_queued = deepcopy(meeting._pending_sync_saves)
    original_save_tree = meeting._sync_service.save_tree

    def fail_stage(*args, **kwargs):
        if kwargs.get("stage_only"):
            raise OSError("journal unavailable during insertion")
        return original_save_tree(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(meeting._sync_service, "save_tree", fail_stage)
        accepted = MeetingTreeController._commit_inserted_nodes(
            meeting, "section:section", 0, [_media("rejected")], signal_name="media",
        )

    assert not accepted
    assert meeting._find_node("rejected") is None
    assert meeting._pending_sync_saves == previously_queued
    for request in meeting._pending_sync_saves.values():
        meeting._sync_service.save_tree(
            request.folder, request.identity, nodes=request.nodes,
            deleted_source_keys=request.deleted_source_keys,
            linked_folder_files=request.linked_folder_files,
            meeting_folder_imports=request.meeting_folder_imports,
            base_snapshot=request.snapshot,
            canonical_reset_generation=request.canonical_reset_generation,
            hidden_canonical_media=request.hidden_canonical_media,
        )
    reloaded = meeting._sync_service.load_tree(str(Path(meeting._sync_folder).parent), identity)
    assert reloaded is not None
    assert "rejected" not in {node["id"] for node in iter_nodes(reloaded.nodes)}


def test_rejected_synced_insertion_rearms_pending_image_framing(synced_meeting, monkeypatch):
    meeting, _identity = synced_meeting
    meeting._parse_list_id = lambda value: MeetingTreeController._parse_list_id(meeting, value)
    meeting._children_for_target = lambda kind, node_id: (
        MeetingTreeController._children_for_target(meeting, kind, node_id)
    )
    meeting._image_framing_save_pending = True
    meeting._find_node("outside")["image_framing"] = {"zoom": 1.4}
    starts = []
    meeting._image_framing_save_timer = SimpleNamespace(
        stop=lambda: None, start=lambda: starts.append(True),
    )
    meeting._schedule_image_framing_save = lambda: (
        MeetingTreeController._schedule_image_framing_save(meeting)
    )

    def fail_stage(*_args, **kwargs):
        if kwargs.get("stage_only"):
            raise OSError("journal unavailable")
        raise AssertionError("Unexpected non-staging save")

    monkeypatch.setattr(meeting._sync_service, "save_tree", fail_stage)
    meeting._start_media_requests = lambda *_args: None
    assert not MeetingTreeController._commit_inserted_nodes(
        meeting, "section:section", 0, [_media("rejected")], signal_name="media",
    )
    assert meeting._find_node("rejected") is None
    assert meeting._image_framing_save_pending
    assert starts == [True]


def test_synced_insertion_staged_before_rejected_local_cache(synced_meeting):
    meeting, identity = synced_meeting
    meeting._parse_list_id = lambda value: MeetingTreeController._parse_list_id(meeting, value)
    meeting._children_for_target = lambda kind, node_id: (
        MeetingTreeController._children_for_target(meeting, kind, node_id)
    )
    meeting._start_media_requests = lambda *_args: None
    meeting._save_local_cache = lambda: False

    assert MeetingTreeController._commit_inserted_nodes(
        meeting, "section:section", 0, [_media("added")], signal_name="media",
    )
    reloaded = meeting._sync_service.load_tree(str(Path(meeting._sync_folder).parent), identity)
    assert reloaded is not None
    assert "added" in {node["id"] for node in iter_nodes(reloaded.nodes)}


def test_synced_insertion_waits_for_initial_local_sync_binding(synced_meeting):
    meeting, identity = synced_meeting
    meeting._parse_list_id = lambda value: MeetingTreeController._parse_list_id(meeting, value)
    meeting._children_for_target = lambda kind, node_id: (
        MeetingTreeController._children_for_target(meeting, kind, node_id)
    )
    meeting._start_media_requests = lambda *_args: None
    meeting._sync_binding_writes = {"meeting:binding": 3}
    meeting._snapshot_storage_key = lambda *_args: "meeting:binding"
    meeting.storageSaved = SimpleNamespace(emit=lambda *_args: None)

    assert not MeetingTreeController._commit_inserted_nodes(
        meeting, "section:section", 0, [_media("early")], signal_name="media",
    )
    assert meeting._find_node("early") is None
    assert not meeting._pending_sync_saves
    reloaded = meeting._sync_service.load_tree(str(Path(meeting._sync_folder).parent), identity)
    assert reloaded is not None
    assert "early" not in {node["id"] for node in iter_nodes(reloaded.nodes)}

    MeetingTreeController._on_snapshot_write_completed(meeting, "meeting:binding", 2)
    assert meeting._sync_binding_writes == {"meeting:binding": 3}
    MeetingTreeController._on_snapshot_write_completed(meeting, "meeting:binding", 3)
    assert not meeting._sync_binding_writes
    assert MeetingTreeController._commit_inserted_nodes(
        meeting, "section:section", 0, [_media("accepted")], signal_name="media",
    )


@pytest.mark.parametrize("storage_available", [False, True])
def test_ordinary_edit_during_sync_binding_waits_for_local_recovery_copy(
    synced_meeting, tmp_path, storage_available,
):
    meeting, identity = synced_meeting
    store = MeetingTreeStore(tmp_path / "meeting-tree-cache.json")
    snapshots = _attach_meeting_store(meeting, store)
    saves = []
    real_save = store.save

    def save_local(*args, **kwargs):
        if not storage_available:
            raise OSError("local snapshot unavailable")
        saved = real_save(*args, **kwargs)
        saves.append(saved.tree_key)
        return saved

    store.save = save_local
    storage_key = meeting._snapshot_storage_key()
    meeting._sync_binding_writes = {storage_key: 0}
    failures = []
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *args: failures.append(args))
    meeting._find_node("manual")["title"] = "Edited after sync activation"

    try:
        saved = MeetingTreeController._save(meeting)
        if storage_available:
            assert saved
            assert saves
            persisted = MeetingTreeStore(store.path).snapshot(meeting._tree_key)
            assert persisted is not None
            assert persisted.linked_sync["enabled"]
            assert next(node for node in iter_nodes(persisted.nodes) if node["id"] == "manual")[
                "title"
            ] == "Edited after sync activation"
            assert storage_key not in meeting._sync_binding_writes
            synced = meeting._sync_service.load_tree(str(Path(meeting._sync_folder).parent), identity)
            assert synced is not None
            assert next(node for node in iter_nodes(synced.nodes) if node["id"] == "manual")[
                "title"
            ] == "Edited after sync activation"
        else:
            assert not saved
            assert failures and "local snapshot unavailable" in failures[-1][1]
            assert storage_key in meeting._sync_binding_writes
            assert not meeting._pending_sync_saves
            synced = meeting._sync_service.load_tree(str(Path(meeting._sync_folder).parent), identity)
            assert synced is not None
            assert next(node for node in iter_nodes(synced.nodes) if node["id"] == "manual").get(
                "title"
            ) != "Edited after sync activation"
    finally:
        snapshots.shutdown()


@pytest.mark.parametrize("remote_active, binding_pending", [
    (True, True), (False, True), (True, False),
])
def test_sync_refresh_preserves_edit_awaiting_local_recovery(
    synced_meeting, remote_active, binding_pending,
):
    from solin.widgets.meetings.tree_controller import _MeetingSyncDiscovery

    meeting, identity = synced_meeting
    original_snapshot = meeting._sync_snapshot
    remote_nodes = deepcopy(meeting._nodes)
    remote_nodes[0]["title"] = "Remote change"
    remote = meeting._sync_service.save_tree(
        Path(meeting._sync_folder), identity, nodes=remote_nodes,
        deleted_source_keys=set(), linked_folder_files={},
        meeting_folder_imports={}, base_snapshot=original_snapshot,
    )

    meeting._find_node("manual")["title"] = "Unstaged local edit"
    storage_key = meeting._snapshot_storage_key()
    meeting._unstaged_sync_edits.add(storage_key)
    meeting._sync_recovery_bases[storage_key] = original_snapshot
    if binding_pending:
        meeting._sync_binding_writes[storage_key] = 3
    meeting._sync_root = ""
    meeting._sync_retry_timer = SimpleNamespace(start=lambda *_args: None, stop=lambda: None)
    meeting.chromeChanged = SimpleNamespace(emit=lambda: None)
    meeting.stateChanged = SimpleNamespace(emit=lambda: None)
    meeting.storageSaved = SimpleNamespace(emit=lambda *_args: None)
    meeting._meeting_folder_pending_sources = set()
    meeting._start_media_requests = lambda: None

    discovery = _MeetingSyncDiscovery(
        True, Path(meeting._sync_folder),
        remote if remote_active else None, active=remote_active,
    )
    MeetingTreeController._apply_sync_discovery(meeting, discovery)

    assert meeting._find_node("manual")["title"] == "Unstaged local edit"
    assert meeting._sync_enabled
    assert meeting._sync_transport_pending

    if binding_pending:
        assert meeting._sync_snapshot == original_snapshot
        assert storage_key in meeting._unstaged_sync_edits
        assert meeting._sync_binding_writes[storage_key] == 3
        MeetingTreeController._on_snapshot_write_completed(meeting, storage_key, 3)

    assert storage_key not in meeting._sync_binding_writes
    assert storage_key not in meeting._unstaged_sync_edits
    assert meeting._pending_sync_saves
    recovered = meeting._sync_service.load_tree(
        str(Path(meeting._sync_folder).parent), identity,
    )
    assert recovered is not None
    assert next(node for node in iter_nodes(recovered.nodes) if node["id"] == "manual")[
        "title"
    ] == "Unstaged local edit"


def test_uncertain_sync_edit_survives_restart_without_its_journal_operation(
    synced_meeting, tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    meeting, identity = synced_meeting
    store = MeetingTreeStore(tmp_path / "meeting-tree-cache.json")
    snapshots = _attach_meeting_store(meeting, store)
    completed = []
    snapshots.writeCompleted.connect(
        lambda key, generation: completed.append((key, generation))
    )
    replica = meeting._sync_service._replica(Path(meeting._sync_folder))
    baseline = meeting._sync_snapshot
    assert MeetingTreeController._save_local_cache(meeting, durable=True)
    previous_outbox = set(replica.outbox_dir.glob("*.json"))
    real_publish = journal._publish
    real_read = Path.read_bytes

    def error_after_publication(path, operation):
        real_publish(path, operation)
        if path.parent == replica.outbox_dir:
            raise OSError("publication acknowledgement unavailable")

    def unreadable_outbox(path):
        if path.parent in (replica.outbox_dir, replica.archive_dir):
            raise PermissionError("journal read unavailable")
        return real_read(path)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(journal, "_publish", error_after_publication)
            patch.setattr(
                journal, "_confirm_local_publication",
                lambda *_args: (_ for _ in ()).throw(PermissionError("confirmation denied")),
            )
            patch.setattr(Path, "read_bytes", unreadable_outbox)
            result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

        assert result.removed_ids + result.pending_ids == ("nested",)
        assert meeting._sync_snapshot.durability_pending
        storage_key = meeting._snapshot_storage_key()
        expected_write = (storage_key, snapshots._generations[storage_key])
        deadline = time.monotonic() + 3
        while expected_write not in completed and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.002)
        assert expected_write in completed
        persisted = MeetingTreeStore(store.path).snapshot(meeting._tree_key)
        assert persisted is not None
        assert persisted.linked_sync["staging_pending"]
        assert persisted.linked_sync["snapshot"]["operation_ids"] == list(baseline.operation_ids)
        assert "nested" not in {node["id"] for node in iter_nodes(persisted.nodes)}

        # The ambiguous publication did not survive the restart. The local
        # snapshot is still enough to recreate the intended deletion.
        new_operations = set(replica.outbox_dir.glob("*.json")) - previous_outbox
        assert len(new_operations) == 1
        new_operations.pop().unlink()
        restarted_service = MeetingLinkedFolderSync(lambda _pub_type: 0)
        before_recovery = restarted_service.load_tree(str(tmp_path), identity)
        assert before_recovery is not None
        assert "nested" in {node["id"] for node in iter_nodes(before_recovery.nodes)}

        meeting._sync_service = restarted_service
        meeting._sync_snapshot = ReplicaSnapshot()
        meeting._unstaged_sync_edits.clear()
        meeting._sync_recovery_bases.clear()
        meeting._pending_sync_saves.clear()
        MeetingTreeController._restore_local_sync_binding(meeting, persisted)
        meeting._nodes = deepcopy(persisted.nodes)
        MeetingTreeController._stage_recovered_local_edit(meeting)

        assert not meeting._unstaged_sync_edits
        assert meeting._pending_sync_saves
        recovered = restarted_service.load_tree(str(tmp_path), identity)
        assert recovered is not None
        assert "nested" not in {node["id"] for node in iter_nodes(recovered.nodes)}
    finally:
        snapshots.shutdown()


def test_uncertain_removal_without_local_recovery_is_reported_as_pending(
    synced_meeting, tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    meeting, _identity = synced_meeting
    store = MeetingTreeStore(tmp_path / "meeting-tree-cache.json")
    snapshots = _attach_meeting_store(meeting, store)
    failures = []
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *args: failures.append(args))
    replica = meeting._sync_service._replica(Path(meeting._sync_folder))
    real_publish = journal._publish

    def uncertain_publish(path, operation):
        real_publish(path, operation)
        if path.parent == replica.outbox_dir:
            raise OSError("journal confirmation unavailable")

    def fail_local_snapshot(*_args, **_kwargs):
        raise OSError("local storage unavailable")

    store.save = fail_local_snapshot
    try:
        with monkeypatch.context() as patch:
            patch.setattr(journal, "_publish", uncertain_publish)
            patch.setattr(
                journal, "_confirm_local_publication",
                lambda *_args: (_ for _ in ()).throw(OSError("journal fsync unavailable")),
            )
            result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

        assert result.removed_ids == ()
        assert result.pending_ids == ("nested",)
        assert meeting._find_node("nested") is None
        assert meeting._sync_snapshot.durability_pending
        assert meeting._pending_sync_saves
        assert failures
    finally:
        snapshots.shutdown()


def test_slow_recovery_write_does_not_report_a_storage_failure(
    synced_meeting, tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    meeting, _identity = synced_meeting
    store = MeetingTreeStore(tmp_path / "meeting-tree-cache.json")
    snapshots = _attach_meeting_store(meeting, store)
    started = threading.Event()
    release = threading.Event()
    failures = []
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *args: failures.append(args))
    real_save = store.save
    real_publish = journal._publish
    replica = meeting._sync_service._replica(Path(meeting._sync_folder))

    def slow_save(*args, **kwargs):
        started.set()
        if not release.wait(2):
            raise TimeoutError("recovery write was never released")
        return real_save(*args, **kwargs)

    def uncertain_publish(path, operation):
        real_publish(path, operation)
        if path.parent == replica.outbox_dir:
            raise OSError("journal confirmation unavailable")

    store.save = slow_save
    try:
        with monkeypatch.context() as patch:
            patch.setattr(journal, "_publish", uncertain_publish)
            patch.setattr(
                journal, "_confirm_local_publication",
                lambda *_args: (_ for _ in ()).throw(OSError("journal fsync unavailable")),
            )
            result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

        assert started.is_set()
        assert result.pending_ids == ("nested",)
        assert failures and all("journal" in error.lower() for _, error in failures)
    finally:
        release.set()
        snapshots.shutdown()


def test_uncertain_insertion_keeps_prepared_media_when_local_recovery_fails(
    synced_meeting, tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    meeting, _identity = synced_meeting
    snapshots = _attach_meeting_store(
        meeting, MeetingTreeStore(tmp_path / "meeting-tree-cache.json"),
    )
    meeting._parse_list_id = lambda value: MeetingTreeController._parse_list_id(meeting, value)
    meeting._children_for_target = lambda kind, node_id: (
        MeetingTreeController._children_for_target(meeting, kind, node_id)
    )
    meeting._start_media_requests = lambda *_args: None
    meeting._store.save = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        OSError("local storage unavailable")
    )
    replica = meeting._sync_service._replica(Path(meeting._sync_folder))
    real_publish = journal._publish

    def uncertain_publish(path, operation):
        real_publish(path, operation)
        if path.parent == replica.outbox_dir:
            raise OSError("journal acknowledgement unavailable")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(journal, "_publish", uncertain_publish)
            patch.setattr(
                journal, "_confirm_local_publication",
                lambda *_args: (_ for _ in ()).throw(OSError("journal fsync unavailable")),
            )
            accepted = MeetingTreeController._commit_inserted_nodes(
                meeting, "section:section", 0, [_media("inserted")], signal_name="media",
            )

        assert accepted
        assert meeting._find_node("inserted") is not None
        assert meeting._sync_snapshot.durability_pending
        assert meeting._pending_sync_saves
    finally:
        snapshots.shutdown()


def test_meeting_removal_refuses_sync_edit_without_journal(meeting):
    failures = []
    meeting._sync_enabled = True
    meeting._sync_folder = ""
    meeting._sync_identity = None
    meeting._image_framing_save_timer = SimpleNamespace(stop=lambda: None)
    meeting._schedule_sync_manifest_save = lambda **kwargs: (
        MeetingTreeController._schedule_sync_manifest_save(meeting, **kwargs)
    )
    meeting._save_local_cache = lambda: pytest.fail("Unstaged removal reached local persistence")
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *args: failures.append(args))
    meeting._save = lambda **kwargs: MeetingTreeController._save(meeting, **kwargs)

    result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

    assert result.failed_ids == ("nested",)
    assert meeting._find_node("nested") is not None
    assert len(failures) == 1


@pytest.mark.parametrize("prior_pending", [False, True])
def test_meeting_removal_rejects_edit_when_journal_write_fails(
    meeting, tmp_path, monkeypatch, prior_pending,
):
    from solin.core.ingest.sync import journal

    folder = tmp_path / "2026-11-02 MW"
    folder.mkdir()
    identity = MeetingSyncIdentity("test", "mwb", date(2026, 11, 2))
    service = MeetingLinkedFolderSync(lambda _pub_type: 0)
    initial = service.save_tree(
        folder, identity, nodes=deepcopy(meeting._nodes), deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={}, enable=True,
    )
    local_writes = []
    timer_starts = []
    meeting._sync_enabled = True
    meeting._sync_folder = str(folder)
    meeting._sync_identity = identity
    meeting._sync_snapshot = initial.snapshot
    meeting._sync_revision = initial.revision
    meeting._sync_service = service
    meeting._pending_sync_saves = {}
    meeting._sync_save_inflight = None
    meeting._image_framing_save_timer = SimpleNamespace(stop=lambda: None)
    meeting._sync_save_timer = SimpleNamespace(
        start=timer_starts.append, stop=lambda: None,
    )
    meeting._save_local_cache = lambda: local_writes.append(deepcopy(meeting._nodes)) or True
    meeting._arm_sync_save_timer = lambda: MeetingTreeController._arm_sync_save_timer(meeting)
    meeting._schedule_sync_manifest_save = lambda **kwargs: (
        MeetingTreeController._schedule_sync_manifest_save(meeting, **kwargs)
    )
    meeting.syncStateChanged = SimpleNamespace(emit=lambda: None)
    failures = []
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *args: failures.append(args))
    meeting._save = lambda **kwargs: MeetingTreeController._save(meeting, **kwargs)
    if prior_pending:
        MeetingTreeController._schedule_sync_manifest_save(meeting)
    pending_before = deepcopy(meeting._pending_sync_saves)

    original_publish = journal.publish_bytes_immutable
    failed_writes = []

    def inaccessible_outbox(path, payload, **kwargs):
        if "outbox" in path.parts:
            failed_writes.append(path)
            raise PermissionError("Journal outbox is inaccessible")
        return original_publish(path, payload, **kwargs)

    monkeypatch.setattr(journal, "publish_bytes_immutable", inaccessible_outbox)
    result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

    assert failed_writes
    assert result.failed_ids == ("nested",)
    assert meeting._find_node("nested") is not None
    assert local_writes == []
    assert len(failures) == 1
    assert meeting._pending_sync_saves == pending_before
    assert meeting._sync_snapshot == initial.snapshot

    monkeypatch.setattr(journal, "publish_bytes_immutable", original_publish)
    # A fresh process sees the original media because the deletion was rejected.
    reloaded = MeetingLinkedFolderSync(lambda _pub_type: 0).load_tree(str(tmp_path), identity)
    assert reloaded is not None
    assert "nested" in {node["id"] for node in iter_nodes(reloaded.nodes)}


def test_meeting_removal_reconciles_error_after_outbox_publication(
    synced_meeting, tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    meeting, identity = synced_meeting

    original_publish = journal.publish_bytes_immutable
    published = []

    def error_after_publish(path, payload, **kwargs):
        created = original_publish(path, payload, **kwargs)
        if "outbox" in path.parts:
            published.append(path)
            raise OSError("directory sync failed after publication")
        return created

    with monkeypatch.context() as patch:
        patch.setattr(journal, "publish_bytes_immutable", error_after_publish)
        result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

    assert published and published[0].exists()
    assert result.removed_ids == ("nested",)
    assert meeting._find_node("nested") is None
    assert len(meeting._pending_sync_saves) == 1
    reloaded = MeetingLinkedFolderSync(lambda _pub_type: 0).load_tree(str(tmp_path), identity)
    assert reloaded is not None
    assert "nested" not in {node["id"] for node in iter_nodes(reloaded.nodes)}


def test_meeting_removal_keeps_published_intent_when_journal_reads_are_denied(
    synced_meeting, tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    meeting, identity = synced_meeting
    replica = meeting._sync_service._replica(Path(meeting._sync_folder))
    original_unlink = Path.unlink
    original_read = Path.read_bytes

    def fail_cleanup_after_publication(path, *args, **kwargs):
        if path.parent == replica.outbox_dir and path.name.startswith("."):
            raise PermissionError("temporary file cleanup denied")
        return original_unlink(path, *args, **kwargs)

    def unreadable_operation(path):
        if path.parent in (replica.outbox_dir, replica.archive_dir) and path.suffix == ".json":
            raise PermissionError("journal read denied")
        return original_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_cleanup_after_publication)
        patch.setattr(
            journal, "_confirm_local_publication",
            lambda *_args: (_ for _ in ()).throw(PermissionError("journal confirmation denied")),
        )
        patch.setattr(Path, "read_bytes", unreadable_operation)
        result = MeetingTreeController.remove_additional_media(meeting, "subsection:sub")

    assert result.removed_ids == ("nested",)
    assert meeting._find_node("nested") is None
    assert meeting._sync_snapshot.durability_pending
    reloaded = MeetingLinkedFolderSync(lambda _pub_type: 0).load_tree(str(tmp_path), identity)
    assert reloaded is not None
    assert "nested" not in {node["id"] for node in iter_nodes(reloaded.nodes)}


def test_uncertain_meeting_removal_defers_file_cleanup_until_journal_is_published(
    synced_meeting, tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    meeting, identity = synced_meeting
    removed_source = meeting._find_node("manual")
    removed_source["linked_folder_source"] = meeting._sync_folder
    removed_source["media_ref"] = {
        "file_path": str(Path(meeting._sync_folder) / "manual.mp4"),
    }
    meeting._meeting_folder_imports["manual.mp4"] = {
        "kind": "media", "path": removed_source["media_ref"]["file_path"],
        "node_ids": ["manual"],
    }
    cleanup = []
    meeting._queue_linked_media_removal = lambda *args: cleanup.append(args)
    failures = []
    meeting.storageSaveFailed = SimpleNamespace(emit=lambda *args: failures.append(args))
    replica = meeting._sync_service._replica(Path(meeting._sync_folder))
    original_publish = journal._publish

    def error_after_publish(path, operation):
        original_publish(path, operation)
        if path.parent == replica.outbox_dir:
            raise OSError("publication became visible")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "_publish", error_after_publish)
        patch.setattr(
            journal, "_confirm_local_publication",
            lambda *_args: (_ for _ in ()).throw(OSError("durability unavailable")),
        )
        result = MeetingTreeController.remove_additional_media(meeting, "section:section")

    assert "manual" in result.removed_ids
    assert meeting._find_node("manual") is None
    assert meeting._sync_snapshot.durability_pending
    assert failures and "durability" in failures[0][1].lower()
    assert cleanup == []
    restarted = MeetingLinkedFolderSync(lambda _pub_type: 0)
    record = restarted.load_tree(str(tmp_path), identity)
    assert record is not None
    assert "manual" not in {node["id"] for node in iter_nodes(record.nodes)}

    request = next(iter(meeting._pending_sync_saves.values()))
    published = meeting._sync_service.save_tree(
        request.folder, request.identity, nodes=request.nodes,
        deleted_source_keys=request.deleted_source_keys,
        linked_folder_files=request.linked_folder_files,
        meeting_folder_imports=request.meeting_folder_imports,
        base_snapshot=request.snapshot,
    )
    meeting._apply_sync_record = lambda record: setattr(meeting, "_sync_snapshot", record.snapshot)
    MeetingTreeController._on_sync_save_completed(
        meeting, next(iter(meeting._pending_sync_saves)), request.generation, published, None,
    )
    assert not published.pending_count
    assert cleanup == [(
        "manual", str(Path(meeting._sync_folder) / "manual.mp4"), meeting._sync_folder,
    )]
    assert not meeting._deferred_media_cleanup


def test_deferred_media_cleanup_waits_for_detachment_persistence(synced_meeting):
    meeting, _identity = synced_meeting
    source = str(Path(meeting._sync_folder) / "manual.mp4")
    meeting._deferred_media_cleanup = [
        (meeting._tree_key, meeting._sync_snapshot.document_id, "manual", source, meeting._sync_folder),
    ]
    cleanup = []
    meeting._queue_linked_media_removal = lambda *args: cleanup.append(args)
    meeting._detach_pending_persistence = {meeting._tree_key}
    meeting._snapshot_storage_key = lambda *_args: "meeting:detachment"
    meeting.storageSaved = SimpleNamespace(emit=lambda *_args: None)

    # A stale completion cannot delete a source after this folder is
    # reactivated with a new document identity.
    MeetingTreeController._on_snapshot_write_completed(meeting, "meeting:detachment", 1)
    assert not cleanup
    assert meeting._deferred_media_cleanup

    meeting._sync_enabled = False
    meeting._detach_pending_persistence.add(meeting._tree_key)
    meeting._request_sync_refresh = lambda: None
    MeetingTreeController._on_snapshot_write_completed(meeting, "meeting:detachment", 2)
    assert cleanup == [("manual", source, meeting._sync_folder)]
    assert not meeting._deferred_media_cleanup


def test_received_sync_disable_releases_deferred_cleanup_after_local_save(synced_meeting):
    from solin.widgets.meetings.tree_controller import _MeetingSyncDiscovery

    meeting, _identity = synced_meeting
    source = str(Path(meeting._sync_folder) / "manual.mp4")
    folder = Path(meeting._sync_folder)
    meeting._deferred_media_cleanup = [
        (meeting._tree_key, meeting._sync_snapshot.document_id, "manual", source, str(folder)),
    ]
    meeting._detach_pending_persistence = set()
    meeting._sync_root = ""
    meeting._sync_retry_timer = SimpleNamespace(start=lambda *_args: None)
    meeting._snapshot_storage_key = lambda *_args: "meeting:received-disable"
    meeting.chromeChanged = SimpleNamespace(emit=lambda: None)
    meeting.stateChanged = SimpleNamespace(emit=lambda: None)
    meeting.storageSaved = SimpleNamespace(emit=lambda *_args: None)
    meeting._request_sync_refresh = lambda: None
    meeting._queue_linked_media_removal = lambda *args: meeting.cleanup.append(args)

    MeetingTreeController._apply_sync_discovery(
        meeting, _MeetingSyncDiscovery(True, folder, active=False),
    )

    assert meeting._tree_key in meeting._detach_pending_persistence
    assert not meeting.cleanup
    MeetingTreeController._on_snapshot_write_completed(meeting, "meeting:received-disable", 1)
    assert meeting.cleanup == [("manual", source, str(folder))]
    assert not meeting._deferred_media_cleanup


def test_returning_to_synced_meeting_releases_confirmed_cleanup(synced_meeting, tmp_path):
    from solin.widgets.meetings.tree_controller import _MeetingSyncDiscovery

    meeting, identity = synced_meeting
    source = str(Path(meeting._sync_folder) / "manual.mp4")
    result = MeetingTreeController.remove_additional_media(meeting, "section:section")
    assert "manual" in result.removed_ids
    request = next(iter(meeting._pending_sync_saves.values()))
    published = meeting._sync_service.save_tree(
        request.folder, request.identity, nodes=request.nodes,
        deleted_source_keys=request.deleted_source_keys,
        linked_folder_files=request.linked_folder_files,
        meeting_folder_imports=request.meeting_folder_imports,
        base_snapshot=request.snapshot,
    )
    assert not published.pending_count
    meeting._sync_snapshot = published.snapshot
    meeting._deferred_media_cleanup = [
        (meeting._tree_key, published.snapshot.document_id, "manual", source, meeting._sync_folder),
    ]
    meeting._sync_root = ""
    meeting._sync_retry_timer = SimpleNamespace(start=lambda *_args: None, stop=lambda: None)
    meeting._pending_sync_saves.clear()

    MeetingTreeController._apply_sync_discovery(
        meeting, _MeetingSyncDiscovery(True, Path(meeting._sync_folder), published, active=True),
    )

    assert meeting.cleanup == [("manual", source, meeting._sync_folder)]
    assert not meeting._deferred_media_cleanup


def test_meeting_sync_retains_media_files_until_deletion_journal_is_published(
    tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    folder = tmp_path / "2026-11-02 MW"
    folder.mkdir()
    source = folder / "manual.mp4"
    source.write_bytes(b"original media")
    identity = MeetingSyncIdentity("test", "mwb", date(2026, 11, 2))
    service = MeetingLinkedFolderSync(lambda _pub_type: 0)
    media = _media("manual")
    media["media_ref"] = {"file_path": str(source)}
    media["linked_folder_source"] = str(folder)
    initial = service.save_tree(
        folder, identity, nodes=[media], deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={}, enable=True,
    )
    replica = service._replica(folder)
    original_publish = journal._publish

    def unavailable_publication(path, operation):
        if path.parent == replica.operations_dir:
            raise OSError("shared journal unavailable")
        original_publish(path, operation)
        if path.parent == replica.outbox_dir:
            raise OSError("durability cannot be confirmed")

    with monkeypatch.context() as patch:
        patch.setattr(journal, "_publish", unavailable_publication)
        patch.setattr(
            journal, "_confirm_local_publication",
            lambda *_args: (_ for _ in ()).throw(OSError("directory sync unavailable")),
        )
        staged = service.save_tree(
            folder, identity, nodes=[], deleted_source_keys=set(),
            linked_folder_files={}, meeting_folder_imports={},
            base_snapshot=initial.snapshot, stage_only=True,
        )
        assert staged.snapshot.durability_pending
        with pytest.raises(MeetingSyncError, match="awaits durability verification"):
            service.save_tree(
                folder, identity, nodes=[], deleted_source_keys=set(),
                linked_folder_files={}, meeting_folder_imports={},
                base_snapshot=staged.snapshot,
            )
        assert source.read_bytes() == b"original media"

        reloaded = MeetingLinkedFolderSync(lambda _pub_type: 0).load_tree(
            str(tmp_path), identity,
        )
        assert reloaded is not None and reloaded.pending_count > 0
        assert source.read_bytes() == b"original media"

    published = service.save_tree(
        folder, identity, nodes=[], deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={},
        base_snapshot=staged.snapshot,
    )
    assert published.pending_count == 0
    assert not published.resource_error
    assert not source.exists()


def test_direct_meeting_cleanup_waits_for_pending_deletion_publication(
    tmp_path, monkeypatch,
):
    from solin.core.ingest.sync import journal

    folder = tmp_path / "2026-11-02 MW"
    folder.mkdir()
    source = folder / "manual.mp4"
    source.write_bytes(b"original media")
    identity = MeetingSyncIdentity("test", "mwb", date(2026, 11, 2))
    service = MeetingLinkedFolderSync(lambda _pub_type: 0)
    media = _media("manual")
    media["media_ref"] = {"file_path": str(source)}
    media["linked_folder_source"] = str(folder)
    initial = service.save_tree(
        folder, identity, nodes=[media], deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={}, enable=True,
    )
    replica = service._replica(folder)
    original_publish = journal._publish

    def fail_shared_publication(path, operation):
        if path.parent == replica.operations_dir:
            raise OSError("shared journal unavailable")
        original_publish(path, operation)

    with monkeypatch.context() as patch:
        patch.setattr(journal, "_publish", fail_shared_publication)
        staged = service.save_tree(
            folder, identity, nodes=[], deleted_source_keys=set(),
            linked_folder_files={}, meeting_folder_imports={},
            base_snapshot=initial.snapshot, stage_only=True,
        )
        assert not staged.snapshot.durability_pending
        assert replica.pending_count > 0
        assert not service.remove_media_file(
            folder, identity, str(source),
            document_id=staged.snapshot.document_id,
            file_store=SimpleNamespace(),
        )
        assert source.read_bytes() == b"original media"

    published = service.save_tree(
        folder, identity, nodes=[], deleted_source_keys=set(),
        linked_folder_files={}, meeting_folder_imports={},
        base_snapshot=staged.snapshot,
    )
    assert published.pending_count == 0
    assert not published.resource_error
    assert not source.exists()


def test_meeting_folder_mutation_blocks_batch(meeting):
    meeting._folder_mutation_pending = True
    result = MeetingTreeController.remove_additional_media(meeting)
    assert set(result.failed_ids) == {"outside", "manual", "nested"}
    assert result.blocked_ids == ()
    assert meeting.writes == []


def test_meeting_folder_mutation_prevents_removal_prompts(meeting, monkeypatch):
    meeting._folder_mutation_pending = True
    monkeypatch.setattr(
        "solin.widgets.meetings.tree_controller.confirm_additional_media_removal",
        lambda *_args, **_kwargs: pytest.fail("Cannot confirm during a folder mutation"),
    )
    monkeypatch.setattr(
        "solin.widgets.meetings.tree_controller.confirm_container_removal",
        lambda *_args, **_kwargs: pytest.fail("Cannot confirm during a folder mutation"),
    )

    MeetingTreeController.removeAdditionalMedia(meeting, "section:section")
    MeetingTreeController.deleteSection(meeting, "section")

    assert meeting.writes == []
    assert meeting._find_node("section") is not None


def test_meeting_bulk_removal_keeps_active_local_media(meeting):
    meeting._find_node("manual")["media_ref"] = {"file_path": "C:/video/live.mp4"}
    meeting._active_media_paths_provider = lambda: ("C:/video/live.mp4",)
    result = MeetingTreeController.remove_additional_media(meeting, "section:section")
    assert result.blocked_ids == ("manual",)
    assert result.removed_ids == ("nested",)
    assert meeting._find_node("manual") is not None
    assert meeting._find_node("nested") is None


@pytest.mark.parametrize("change", ["context", "move", "insert", "cancel"])
def test_meeting_confirmation_revalidates_original_scope(meeting, monkeypatch, change):
    def confirm(*_args):
        if change == "context":
            meeting._tree_session.generation += 1
        elif change == "move":
            meeting._nodes.append(meeting._find_node("section")["children"].pop(1))
        elif change == "insert":
            meeting._find_node("section")["children"].append(_media("new"))
        return change != "cancel"

    monkeypatch.setattr(
        "solin.widgets.meetings.tree_controller.confirm_additional_media_removal", confirm,
    )
    MeetingTreeController.removeAdditionalMedia(meeting, "section:section")
    survivors = set(select_media_ids(meeting._nodes))
    if change in {"context", "cancel"}:
        assert meeting.writes == []
    elif change == "move":
        assert survivors == {"outside", "official", "manual"}
    else:
        assert survivors == {"outside", "official", "new"}


@pytest.fixture
def playlist():
    view = SimpleNamespace(
        _pl={"id": "playlist", "sections": [
            {"id": "section", "name": "Main"},
            {"id": "sub", "name": "Nested", "parent_id": "section"},
        ], "markers": [{"id": "marker", "subsection_id": "sub"}], "items": [
            {"id": "outside", "type": "video", "url": "same.mp4"},
            {"id": "playing", "type": "video", "url": "same.mp4", "section_id": "section"},
            {"id": "nested", "type": "video", "url": "same.mp4", "section_id": "sub"},
        ]},
        _is_watched=False, _watched_path="", _media_ctrl=None,
        _tree_session=SimpleNamespace(pending_item=lambda _id: None),
        writes=[], publications=[], cleanup=[], warnings=[],
        tr=lambda text: text,
        _cancel_thumbnail_requests_for_item=lambda _id: None,
    )
    view._save = lambda: view.writes.append(deepcopy(view._pl))
    view._publish_tree_snapshot = lambda: view.publications.append(True)
    view._sync_playlist_chrome = lambda **_kwargs: None
    view._schedule_cleanup = lambda items: view.cleanup.extend(items)
    view._section_by_id = lambda node_id: next(
        (node for node in view._pl["sections"] if node["id"] == node_id), None,
    )
    return view


@pytest.mark.parametrize("scope, expected", [
    ("root", ("outside", "playing", "nested")),
    ("section:section", ("playing", "nested")),
    ("subsection:sub", ("nested",)),
])
def test_playlist_bulk_removal_preserves_containers_and_commits_once(playlist, scope, expected):
    result = PlaylistEditView.remove_additional_media(playlist, scope)
    assert result.removed_ids == expected
    assert len(playlist._pl["sections"]) == 2
    assert playlist._pl["markers"]
    assert len(playlist.writes) == len(playlist.publications) == 1


def test_playlist_bulk_keeps_only_the_active_occurrence(playlist):
    playlist._media_ctrl = SimpleNamespace(
        current_occurrence_id="playing", current_occurrence_container_id="playlist",
    )
    result = PlaylistEditView.remove_additional_media(playlist)
    assert result.blocked_ids == ("playing",)
    assert result.removed_ids == ("outside", "nested")
    assert [item["id"] for item in playlist._pl["items"]] == ["playing"]


def test_linked_playlist_submits_one_batch_with_final_survivors(playlist):
    calls = []
    playlist._is_watched = True
    playlist._watched_path = "linked"
    playlist._remove_watched_items = lambda *args, **kwargs: calls.append((args, kwargs))
    PlaylistEditView.remove_additional_media(playlist, "section:section")
    assert len(calls) == 1
    assert [item["id"] for item in calls[0][0][1]] == ["playing", "nested"]
    assert [item["id"] for item in calls[0][1]["remaining_items"]] == ["outside"]


@pytest.mark.parametrize("choice", list(ContainerRemovalChoice))
def test_playlist_container_removal_uses_same_media_policy(playlist, monkeypatch, choice):
    before = deepcopy(playlist._pl)
    monkeypatch.setattr(
        "solin.widgets.playlist.widget.confirm_container_removal",
        lambda *_args, **_kwargs: choice,
    )
    PlaylistEditView._delete_section(playlist, "section")
    if choice is ContainerRemovalChoice.CANCEL:
        assert playlist._pl == before
        assert playlist.writes == []
    else:
        assert playlist._pl["sections"] == playlist._pl["markers"] == []
        expected = ["outside"] if choice is ContainerRemovalChoice.DELETE_MEDIA else ["outside", "playing", "nested"]
        assert [item["id"] for item in playlist._pl["items"]] == expected
        assert all(not item.get("section_id") for item in playlist._pl["items"])
        assert len(playlist.writes) == 1


def test_playlist_container_removal_preserves_active_media_and_publishes_once(playlist, monkeypatch):
    playlist._media_ctrl = SimpleNamespace(
        current_occurrence_id="playing", current_occurrence_container_id="playlist",
    )
    warnings = []
    monkeypatch.setattr(
        "solin.widgets.playlist.widget.confirm_container_removal",
        lambda *_args, **_kwargs: ContainerRemovalChoice.DELETE_MEDIA,
    )
    monkeypatch.setattr(
        "solin.widgets.playlist.widget.warn_playing_media_removal",
        lambda _parent, titles: warnings.append(titles),
    )

    PlaylistEditView._delete_section(playlist, "section")

    assert playlist._pl["sections"] == playlist._pl["markers"] == []
    assert [item["id"] for item in playlist._pl["items"]] == ["outside", "playing"]
    assert playlist._pl["items"][1]["section_id"] is None
    assert [item["id"] for item in playlist.cleanup] == ["nested"]
    assert len(playlist.writes) == len(playlist.publications) == 1
    assert len(warnings) == 1


def test_playlist_container_removal_with_only_active_media_commits_structure(playlist, monkeypatch):
    playlist._media_ctrl = SimpleNamespace(
        current_occurrence_id="nested", current_occurrence_container_id="playlist",
    )
    warnings = []
    monkeypatch.setattr(
        "solin.widgets.playlist.widget.confirm_container_removal",
        lambda *_args, **_kwargs: ContainerRemovalChoice.DELETE_MEDIA,
    )
    monkeypatch.setattr(
        "solin.widgets.playlist.widget.warn_playing_media_removal",
        lambda _parent, titles: warnings.append(titles),
    )

    PlaylistEditView._delete_section(playlist, "sub")

    assert [section["id"] for section in playlist._pl["sections"]] == ["section"]
    assert playlist._pl["markers"] == []
    assert playlist._pl["items"][-1]["section_id"] == "section"
    assert [item["id"] for item in playlist._pl["items"]] == ["outside", "playing", "nested"]
    assert playlist.cleanup == []
    assert len(playlist.writes) == len(playlist.publications) == 1
    assert len(warnings) == 1


def test_single_playlist_removal_and_batch_use_identical_occurrence_policy(playlist, monkeypatch):
    playlist._media_ctrl = SimpleNamespace(current_occurrence_id="playing")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(True))
    PlaylistEditView._remove_item(playlist, "playing")
    assert warnings == [True]
    assert playlist.writes == []
    assert PlaylistEditView.remove_additional_media(playlist).blocked_ids == ("playing",)
