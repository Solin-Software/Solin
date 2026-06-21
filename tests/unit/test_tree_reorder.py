from __future__ import annotations

from solin.core.tree_reorder import TreeNodeMove, reorder_only_moves


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
