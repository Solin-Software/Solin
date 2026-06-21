from __future__ import annotations

from solin.core.tree_delta import (
    TreeNodeInsert,
    TreeNodeMove,
    TreeNodeRemove,
    TreeSectionUpdate,
    incremental_tree_changes,
    reorder_only_moves,
)


def test_reorder_only_moves_returns_empty_for_equivalent_trees() -> None:
    nodes = [
        {"id": "a", "type": "media", "title": "A", "children": []},
        {"id": "b", "type": "media", "title": "B", "children": []},
    ]

    assert reorder_only_moves(nodes, nodes) == []


def test_reorder_only_moves_describes_root_reorder() -> None:
    current = [
        {"id": "a", "type": "media", "title": "A", "children": []},
        {"id": "b", "type": "media", "title": "B", "children": []},
        {"id": "c", "type": "media", "title": "C", "children": []},
    ]
    desired = [
        {"id": "b", "type": "media", "title": "B", "children": []},
        {"id": "c", "type": "media", "title": "C", "children": []},
        {"id": "a", "type": "media", "title": "A", "children": []},
    ]

    assert reorder_only_moves(current, desired) == [
        TreeNodeMove("b", "root", 0),
        TreeNodeMove("c", "root", 1),
    ]


def test_reorder_only_moves_describes_reparent_to_section() -> None:
    current = [
        {"id": "media", "type": "media", "title": "Media", "children": []},
        {
            "id": "section",
            "type": "section",
            "title": "Section",
            "children": [],
        },
    ]
    desired = [
        {
            "id": "section",
            "type": "section",
            "title": "Section",
            "children": [
                {"id": "media", "type": "media", "title": "Media", "children": []},
            ],
        },
    ]

    assert reorder_only_moves(current, desired) == [
        TreeNodeMove("section", "root", 0),
        TreeNodeMove("media", "section:section", 0),
    ]


def test_reorder_only_moves_rejects_content_changes() -> None:
    current = [{"id": "a", "type": "media", "title": "A", "children": []}]
    desired = [{"id": "a", "type": "media", "title": "Changed", "children": []}]

    assert reorder_only_moves(current, desired) is None


def test_reorder_only_moves_can_ignore_storage_location_fields() -> None:
    current = [
        {
            "id": "section",
            "type": "section",
            "ref": {"id": "section", "name": "Section", "position": 0},
            "children": [],
        },
        {
            "id": "media",
            "type": "media",
            "ref": {"id": "media", "title": "Media", "section_id": None},
            "children": [],
        },
    ]
    desired = [
        {
            "id": "section",
            "type": "section",
            "ref": {"id": "section", "name": "Section", "position": 0},
            "children": [
                {
                    "id": "media",
                    "type": "media",
                    "ref": {
                        "id": "media",
                        "title": "Media",
                        "section_id": "section",
                    },
                    "children": [],
                },
            ],
        },
    ]

    assert reorder_only_moves(
        current,
        desired,
        ignored_payload_keys={"section_id", "position"},
    ) == [TreeNodeMove("media", "section:section", 0)]


def test_incremental_tree_changes_describes_section_metadata() -> None:
    current = [
        {
            "id": "section",
            "type": "section",
            "title": "Before",
            "color_hue": 120,
            "collapsed": False,
            "children": [],
        },
    ]
    desired = [
        {
            "id": "section",
            "type": "section",
            "title": "After",
            "color_hue": 220,
            "collapsed": True,
            "children": [],
        },
    ]

    changes = incremental_tree_changes(
        current,
        desired,
        section_patch_keys={"title", "color_hue", "collapsed"},
    )

    assert changes is not None
    assert changes.moves == []
    assert changes.inserts == []
    assert changes.removals == []
    assert changes.section_updates == [
        TreeSectionUpdate(
            "section",
            metadata_changed=True,
            collapsed_changed=True,
        ),
    ]


def test_incremental_tree_changes_describes_inserted_nodes() -> None:
    current = [
        {"id": "a", "type": "media", "title": "A", "children": []},
    ]
    desired = [
        {"id": "a", "type": "media", "title": "A", "children": []},
        {"id": "b", "type": "media", "title": "B", "children": []},
        {"id": "c", "type": "media", "title": "C", "children": []},
    ]

    changes = incremental_tree_changes(current, desired)

    assert changes is not None
    assert changes.moves == []
    assert changes.removals == []
    assert changes.section_updates == []
    assert changes.inserts == [
        TreeNodeInsert(
            "root",
            1,
            [
                {"id": "b", "type": "media", "title": "B", "children": []},
                {"id": "c", "type": "media", "title": "C", "children": []},
            ],
        ),
    ]


def test_incremental_tree_changes_describes_removed_nodes() -> None:
    current = [
        {"id": "a", "type": "media", "title": "A", "children": []},
        {"id": "b", "type": "media", "title": "B", "children": []},
    ]
    desired = [
        {"id": "a", "type": "media", "title": "A", "children": []},
    ]

    changes = incremental_tree_changes(current, desired)

    assert changes is not None
    assert changes.inserts == []
    assert changes.removals == [TreeNodeRemove("b")]


def test_incremental_tree_changes_rejects_mixed_insert_and_reorder() -> None:
    current = [
        {"id": "a", "type": "media", "title": "A", "children": []},
        {"id": "b", "type": "media", "title": "B", "children": []},
    ]
    desired = [
        {"id": "b", "type": "media", "title": "B", "children": []},
        {"id": "a", "type": "media", "title": "A", "children": []},
        {"id": "c", "type": "media", "title": "C", "children": []},
    ]

    assert incremental_tree_changes(current, desired) is None
