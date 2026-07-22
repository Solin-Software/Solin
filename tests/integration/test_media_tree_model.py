from __future__ import annotations

import random

from PySide6.QtCore import QCoreApplication, QModelIndex, QPersistentModelIndex
from PySide6.QtTest import QAbstractItemModelTester

from solin.ui.qml.media_tree.model import MediaTreeModel, ReconcileResult
from solin.ui.qml.media_tree.roles import MediaTreeRole
from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeNodeType,
    MediaTreeSnapshot,
)


_APP = QCoreApplication.instance() or QCoreApplication([])
_INVALID_INDEX = QModelIndex()


def _node(
    node_id: str,
    node_type: MediaTreeNodeType = MediaTreeNodeType.MEDIA,
    *,
    revision: int = 0,
    children: tuple[MediaTreeNodeSnapshot, ...] = (),
    **roles,
) -> MediaTreeNodeSnapshot:
    return MediaTreeNodeSnapshot.create(
        node_id,
        node_type,
        source_revision=revision,
        roles=roles,
        children=children,
    )


def _snapshot(revision: int, *roots: MediaTreeNodeSnapshot) -> MediaTreeSnapshot:
    return MediaTreeSnapshot.create("playlist:test", revision, roots)


def _ids(model: MediaTreeModel, parent: QModelIndex = _INVALID_INDEX) -> list[str]:
    return [
        str(model.data(model.index(row, 0, parent), int(MediaTreeRole.NODE_ID)))
        for row in range(model.rowCount(parent))
    ]


def test_model_exposes_a_valid_hierarchy_and_complete_role_defaults() -> None:
    model = MediaTreeModel("playlist:test")
    tester = QAbstractItemModelTester(
        model,
        QAbstractItemModelTester.FailureReportingMode.Warning,
    )
    media = _node("media-1", title="Song")
    section = _node(
        "section-1",
        MediaTreeNodeType.SECTION,
        children=(media,),
        title="Opening",
    )

    assert model.apply_snapshot(_snapshot(1, section)) == ReconcileResult.APPLIED

    section_index = model.index(0, 0)
    media_index = model.index(0, 0, section_index)
    assert _ids(model) == ["section-1"]
    assert _ids(model, section_index) == ["media-1"]
    assert not model.parent(section_index).isValid()
    assert model.parent(media_index) == section_index
    assert model.data(media_index, int(MediaTreeRole.TITLE)) == "Song"
    assert model.data(media_index, int(MediaTreeRole.CLOUD_PROGRESS)) == -1.0
    assert tester.model() is model


def test_reconcile_preserves_identity_across_reorder_and_cross_parent_move() -> None:
    model = MediaTreeModel("playlist:test")
    media = _node("media-1", title="Song")
    first = _node(
        "section-1",
        MediaTreeNodeType.SECTION,
        children=(media,),
        title="First",
    )
    second = _node("section-2", MediaTreeNodeType.SECTION, title="Second")
    model.apply_snapshot(_snapshot(1, first, second))
    persistent = QPersistentModelIndex(model.index_for_id("media-1"))
    resets: list[bool] = []
    model.modelReset.connect(lambda: resets.append(True))

    moved_media = _node("media-1", revision=2, title="Renamed")
    next_second = _node(
        "section-2",
        MediaTreeNodeType.SECTION,
        revision=2,
        children=(moved_media,),
        title="Second",
    )
    next_first = _node(
        "section-1",
        MediaTreeNodeType.SECTION,
        revision=2,
        title="First",
    )
    assert (
        model.apply_snapshot(_snapshot(2, next_second, next_first))
        == ReconcileResult.APPLIED
    )

    assert persistent.isValid()
    assert persistent.data(int(MediaTreeRole.NODE_ID)) == "media-1"
    assert persistent.data(int(MediaTreeRole.TITLE)) == "Renamed"
    assert persistent.parent().data(int(MediaTreeRole.NODE_ID)) == "section-2"
    assert _ids(model) == ["section-2", "section-1"]
    assert resets == []


def test_reconcile_rejects_a_type_change_for_a_stable_id() -> None:
    model = MediaTreeModel("playlist:test")
    model.apply_snapshot(_snapshot(1, _node("stable", MediaTreeNodeType.MEDIA)))

    try:
        model.apply_snapshot(_snapshot(2, _node("stable", MediaTreeNodeType.SECTION)))
    except ValueError as exc:
        assert "changed type" in str(exc)
    else:
        raise AssertionError("Changing a stable node ID's type must be rejected")


def test_full_replacement_batches_sibling_removals_and_insertions() -> None:
    model = MediaTreeModel("playlist:test")
    inserted: list[tuple[int, int]] = []
    removed: list[tuple[int, int]] = []
    model.rowsInserted.connect(
        lambda _parent, first, last: inserted.append((first, last))
    )
    model.rowsRemoved.connect(
        lambda _parent, first, last: removed.append((first, last))
    )
    first_roots = tuple(_node(f"old-{index}") for index in range(100))
    next_roots = tuple(_node(f"new-{index}") for index in range(100))

    model.apply_snapshot(_snapshot(1, *first_roots))
    model.apply_snapshot(_snapshot(2, *next_roots))

    assert inserted == [(0, 99), (0, 99)]
    assert removed == [(0, 99)]
    assert model.rowCount() == 100


def test_data_changes_emit_only_the_changed_roles() -> None:
    model = MediaTreeModel("playlist:test")
    model.apply_snapshot(_snapshot(1, _node("media-1", title="Old", duration="1:00")))
    changes: list[list[int]] = []
    model.dataChanged.connect(
        lambda _first, _last, roles: changes.append(list(roles))
    )

    model.apply_snapshot(
        _snapshot(2, _node("media-1", title="New", duration="1:00"))
    )

    assert changes == [[int(MediaTreeRole.TITLE)]]


def test_structural_snapshots_are_coalesced_during_interaction() -> None:
    model = MediaTreeModel("playlist:test")
    model.apply_snapshot(_snapshot(1, _node("a", title="A"), _node("b", title="B")))
    applied: list[int] = []
    model.snapshotApplied.connect(lambda _tree_id, revision: applied.append(revision))

    model.beginInteraction()
    assert model.apply_snapshot(
        _snapshot(2, _node("b", title="B"), _node("a", title="A2"))
    ) == ReconcileResult.DEFERRED
    assert model.apply_snapshot(
        _snapshot(3, _node("b", title="B3"), _node("a", title="A3"))
    ) == ReconcileResult.DEFERRED

    assert _ids(model) == ["a", "b"]
    assert model.index_for_id("a").data(int(MediaTreeRole.TITLE)) == "A3"
    assert model.revision == 1

    model.endInteraction()

    assert _ids(model) == ["b", "a"]
    assert model.revision == 3
    assert applied == [3]


def test_stale_and_wrong_tree_snapshots_are_rejected() -> None:
    model = MediaTreeModel("playlist:test")
    assert (
        model.apply_snapshot(_snapshot(2, _node("media-1")))
        == ReconcileResult.APPLIED
    )
    assert (
        model.apply_snapshot(_snapshot(1, _node("stale")))
        == ReconcileResult.STALE
    )

    wrong_tree = MediaTreeSnapshot.create("meeting:test", 3, (_node("wrong"),))
    assert model.apply_snapshot(wrong_tree) == ReconcileResult.TREE_MISMATCH

    assert _ids(model) == ["media-1"]


def test_activate_snapshot_switches_document_without_model_reset() -> None:
    model = MediaTreeModel("playlist:first")
    model.apply_snapshot(
        MediaTreeSnapshot.create("playlist:first", 1, (_node("first-media"),))
    )
    resets: list[bool] = []
    model.modelReset.connect(lambda: resets.append(True))

    result = model.activate_snapshot(
        MediaTreeSnapshot.create(
            "playlist:second",
            7,
            (_node("second-media"), _node("third-media")),
        )
    )

    assert result == ReconcileResult.APPLIED
    assert model.treeId == "playlist:second"
    assert model.revision == 7
    assert model.mediaCount == 2
    assert _ids(model) == ["second-media", "third-media"]
    assert resets == []


def test_reconciler_matches_many_arbitrary_valid_forests() -> None:
    rng = random.Random(20260721)
    model = MediaTreeModel("playlist:test")
    section_ids = [f"section-{index}" for index in range(6)]
    subsection_ids = [f"subsection-{index}" for index in range(6)]
    media_ids = [f"media-{index}" for index in range(12)]
    node_types = {
        **{node_id: MediaTreeNodeType.SECTION for node_id in section_ids},
        **{node_id: MediaTreeNodeType.SUBSECTION for node_id in subsection_ids},
        **{node_id: MediaTreeNodeType.MEDIA for node_id in media_ids},
    }

    for revision in range(1, 151):
        selected_sections = rng.sample(section_ids, rng.randint(0, len(section_ids)))
        selected_subsections = (
            rng.sample(subsection_ids, rng.randint(0, len(subsection_ids)))
            if selected_sections
            else []
        )
        selected_media = rng.sample(media_ids, rng.randint(0, len(media_ids)))
        children_by_parent: dict[str, list[str]] = {"": []}
        parent_by_id: dict[str, str] = {}
        for node_id in selected_sections:
            parent_id = ""
            parent_by_id[node_id] = parent_id
            children_by_parent.setdefault(parent_id, []).append(node_id)
            children_by_parent.setdefault(node_id, [])
        for node_id in selected_subsections:
            parent_id = rng.choice(selected_sections)
            parent_by_id[node_id] = parent_id
            children_by_parent.setdefault(parent_id, []).append(node_id)
            children_by_parent.setdefault(node_id, [])
        for node_id in selected_media:
            parent_id = rng.choice([""] + selected_sections + selected_subsections)
            parent_by_id[node_id] = parent_id
            children_by_parent.setdefault(parent_id, []).append(node_id)
            children_by_parent.setdefault(node_id, [])
        for children in children_by_parent.values():
            rng.shuffle(children)

        def build(
            node_id: str,
            *,
            current_revision: int = revision,
            child_map: dict[str, list[str]] = children_by_parent,
            type_map: dict[str, MediaTreeNodeType] = node_types,
        ) -> MediaTreeNodeSnapshot:
            return _node(
                node_id,
                type_map[node_id],
                revision=current_revision,
                children=tuple(build(child_id) for child_id in child_map[node_id]),
                title=f"{node_id}:{current_revision}",
            )

        roots = tuple(build(node_id) for node_id in children_by_parent[""])
        assert (
            model.apply_snapshot(_snapshot(revision, *roots))
            == ReconcileResult.APPLIED
        )
        assert model.rowCount() == len(roots)
        for node_id, parent_id in parent_by_id.items():
            assert model.contains(node_id)
            assert model.parentId(node_id) == parent_id
            expected_row = children_by_parent[parent_id].index(node_id)
            assert model.childRow(node_id) == expected_row
