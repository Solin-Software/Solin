"""Removal contracts shared by single, scoped and global commands."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox

from solin.controllers.snapshot_write_coordinator import SnapshotWriteCoordinator
from solin.core.meetings.tree_types import iter_nodes
from solin.core.meetings.linked_folder_sync import (
    MeetingLinkedFolderSync, MeetingSyncError, MeetingSyncIdentity,
)
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
        writes=[], notifications=[], cancellations=[], cleanup=[], parent=lambda: None,
    )
    controller._find_node = lambda node_id: next(
        (node for node in iter_nodes(controller._nodes) if node["id"] == node_id), None,
    )
    controller._save = lambda **_kwargs: controller.writes.append(deepcopy(controller._nodes)) or True
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
    pending_before = dict(meeting._pending_sync_saves)

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
