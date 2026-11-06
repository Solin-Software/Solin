"""Removal contracts shared by single, scoped and global commands."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox

from solin.core.meetings.tree_types import iter_nodes
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
    controller._save = lambda: controller.writes.append(deepcopy(controller._nodes)) or True
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


def test_rejected_meeting_save_rolls_back_before_cleanup(meeting):
    before = deepcopy(meeting._nodes)
    meeting._save = lambda: False
    result = MeetingTreeController._remove_media_ids(meeting, {"official", "manual"})
    assert result.failed_ids == ("official", "manual")
    assert meeting._nodes == before
    assert meeting._deleted_source_keys == set()
    assert meeting._hidden_canonical_media == {}
    assert meeting.cancellations == meeting.notifications == meeting.cleanup == []


def test_meeting_folder_mutation_blocks_batch(meeting):
    meeting._folder_mutation_pending = True
    result = MeetingTreeController.remove_additional_media(meeting)
    assert set(result.blocked_ids) == {"outside", "manual", "nested"}
    assert meeting.writes == []


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


def test_single_playlist_removal_and_batch_use_identical_occurrence_policy(playlist, monkeypatch):
    playlist._media_ctrl = SimpleNamespace(current_occurrence_id="playing")
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(True))
    PlaylistEditView._remove_item(playlist, "playing")
    assert warnings == [True]
    assert playlist.writes == []
    assert PlaylistEditView.remove_additional_media(playlist).blocked_ids == ("playing",)
