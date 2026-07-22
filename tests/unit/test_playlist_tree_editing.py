import copy

from solin.core.playlists.tree_editing import (
    build_playlist_tree,
    flat_media_insert_index,
    insert_playlist_media,
    move_playlist_node,
)


def _playlist() -> dict:
    return {
        "id": "playlist-1",
        "sections": [
            {"id": "section-1", "name": "Opening", "position": 0},
            {
                "id": "subsection-1",
                "name": "Songs",
                "parent_id": "section-1",
                "position": 1,
            },
        ],
        "items": [
            {"id": "section-media", "section_id": "section-1"},
            {"id": "subsection-media", "section_id": "subsection-1"},
            {"id": "root-media", "section_id": None},
        ],
        "markers": [
            {
                "id": "marker-1",
                "text": "Stand",
                "subsection_id": "subsection-1",
                "position": 1,
            }
        ],
    }


def test_build_playlist_tree_preserves_section_marker_and_media_order() -> None:
    tree = build_playlist_tree(_playlist())

    assert [node["id"] for node in tree] == ["section-1", "root-media"]
    assert [node["id"] for node in tree[0]["children"]] == [
        "section-media",
        "subsection-1",
    ]
    assert [node["id"] for node in tree[0]["children"][1]["children"]] == [
        "marker-1",
        "subsection-media",
    ]


def test_move_playlist_node_updates_compact_storage_transactionally() -> None:
    playlist = _playlist()

    assert move_playlist_node(
        playlist,
        "root-media",
        "subsection:subsection-1",
        0,
    )

    assert [item["id"] for item in playlist["items"]] == [
        "section-media",
        "root-media",
        "subsection-media",
    ]
    moved = next(item for item in playlist["items"] if item["id"] == "root-media")
    assert moved["section_id"] == "subsection-1"
    assert playlist["markers"][0]["position"] == 2


def test_invalid_playlist_move_does_not_mutate_storage() -> None:
    playlist = _playlist()
    original = copy.deepcopy(playlist)

    assert not move_playlist_node(
        playlist,
        "section-1",
        "subsection:subsection-1",
        0,
    )
    assert playlist == original


def test_playlist_reorder_uses_ui_post_removal_slot() -> None:
    playlist = {
        "id": "playlist",
        "sections": [],
        "markers": [],
        "items": [
            {"id": node_id, "section_id": None}
            for node_id in ("a", "b", "c")
        ],
    }

    assert move_playlist_node(playlist, "a", "root", 1)

    assert [item["id"] for item in playlist["items"]] == ["b", "a", "c"]


def test_insert_playlist_media_targets_a_visual_tree_slot() -> None:
    playlist = _playlist()
    inserted = {"id": "new-media", "title": "New", "url": "new.mp4"}

    assert insert_playlist_media(
        playlist,
        "section:section-1",
        0,
        [inserted],
    )

    section_items = [
        item["id"]
        for item in playlist["items"]
        if item.get("section_id") == "section-1"
    ]
    assert section_items == ["new-media", "section-media"]


def test_negative_media_insert_index_appends_to_root() -> None:
    playlist = _playlist()

    assert insert_playlist_media(
        playlist,
        "root",
        -1,
        [{"id": "new-media", "title": "New", "url": "new.mp4"}],
    )

    assert [node["id"] for node in build_playlist_tree(playlist)] == [
        "section-1",
        "root-media",
        "new-media",
    ]


def test_negative_media_insert_index_appends_inside_section() -> None:
    playlist = _playlist()

    assert insert_playlist_media(
        playlist,
        "section:section-1",
        -1,
        [{"id": "new-media", "title": "New", "url": "new.mp4"}],
    )

    assert [node["id"] for node in build_playlist_tree(playlist)[0]["children"]] == [
        "section-media",
        "subsection-1",
        "new-media",
    ]


def test_negative_flat_media_insert_index_means_append() -> None:
    playlist = _playlist()

    assert flat_media_insert_index(playlist, "root", -1) == 3
    assert flat_media_insert_index(playlist, "section:section-1", -1) == 2


def test_flat_media_index_counts_nested_virtual_slots() -> None:
    playlist = _playlist()

    assert flat_media_insert_index(playlist, "subsection:subsection-1", 1) == 1
