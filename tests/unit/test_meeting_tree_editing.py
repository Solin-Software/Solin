from __future__ import annotations

import copy

from solin.core.meetings.tree_editing import (
    can_drop_tree_node,
    find_tree_node,
    media_descendants,
    move_tree_node,
    parse_tree_list_id,
    replace_tree_node,
)


def test_parse_tree_list_id_recognizes_root_and_invalid_targets():
    assert parse_tree_list_id("") == ("root", "")
    assert parse_tree_list_id("root") == ("root", "")
    assert parse_tree_list_id("section:abc") == ("section", "abc")
    assert parse_tree_list_id("section") == ("", "")


def test_can_drop_tree_node_matches_meeting_tree_rules():
    assert can_drop_tree_node("section", "root")
    assert can_drop_tree_node("media", "section:section")
    assert can_drop_tree_node("subsection", "section:section")
    assert can_drop_tree_node("marker", "subsection:sub")
    assert not can_drop_tree_node("section", "subsection:sub")
    assert not can_drop_tree_node("marker", "root")


def test_move_tree_node_reparents_and_clamps_insert_index():
    nodes = [
        {"id": "media", "type": "media", "children": []},
        {
            "id": "section",
            "type": "section",
            "children": [
                {"id": "existing", "type": "media", "children": []},
            ],
        },
    ]

    assert move_tree_node(nodes, "media", "section:section", 99)

    assert [node["id"] for node in nodes] == ["section"]
    assert [node["id"] for node in nodes[0]["children"]] == ["existing", "media"]


def test_move_tree_node_uses_post_removal_slots_within_same_parent():
    def nodes():
        return [
            {"id": node_id, "type": "media", "children": []}
            for node_id in ("a", "b", "c")
        ]

    after_b = nodes()
    assert move_tree_node(after_b, "a", "root", 1)
    assert [node["id"] for node in after_b] == ["b", "a", "c"]

    after_c = nodes()
    assert move_tree_node(after_c, "a", "root", 2)
    assert [node["id"] for node in after_c] == ["b", "c", "a"]

    unchanged = nodes()
    assert move_tree_node(unchanged, "a", "root", 0)
    assert [node["id"] for node in unchanged] == ["a", "b", "c"]

    upward = nodes()
    assert move_tree_node(upward, "c", "root", 0)
    assert [node["id"] for node in upward] == ["c", "a", "b"]


def test_move_tree_node_rejects_invalid_descendant_target_without_mutation():
    nodes = [
        {
            "id": "section",
            "type": "section",
            "children": [
                {"id": "subsection", "type": "subsection", "children": []},
            ],
        },
        {"id": "media", "type": "media", "children": []},
    ]
    before = copy.deepcopy(nodes)

    assert not move_tree_node(nodes, "section", "subsection:subsection", 0)
    assert nodes == before


def test_find_replace_and_media_descendants_operate_on_nested_tree():
    media = {"id": "media", "type": "media", "children": []}
    marker = {"id": "marker", "type": "marker", "children": []}
    nodes = [
        {
            "id": "section",
            "type": "section",
            "children": [
                {
                    "id": "subsection",
                    "type": "subsection",
                    "children": [media, marker],
                },
            ],
        }
    ]

    assert find_tree_node(nodes, "media") is media
    assert media_descendants(nodes[0]) == [media]

    replacement = [{"id": "replacement", "type": "media", "children": []}]
    assert replace_tree_node(nodes, "subsection", replacement)
    assert nodes[0]["children"] == replacement
