from solin.core.meetings.media_nodes import additional_meeting_media
from solin.core.media.removal import (
    select_media_ids,
    without_media,
)


def _tree():
    return [
        {"id": "root-media", "type": "media"},
        {"id": "section", "type": "section", "children": [
            {"id": "official", "type": "media", "meeting_generated": True},
            {"id": "manual", "type": "media"},
            {"id": "sub", "type": "subsection", "children": [
                {"id": "marker", "type": "marker"},
                {"id": "nested", "type": "media"},
            ]},
        ]},
    ]


def test_selection_uses_scope_and_provenance_in_visual_order():
    tree = _tree()
    assert select_media_ids(tree, eligible=additional_meeting_media) == (
        "root-media", "manual", "nested",
    )
    assert select_media_ids(tree, "section:section", eligible=additional_meeting_media) == (
        "manual", "nested",
    )
    assert select_media_ids(tree, "subsection:sub") == ("nested",)
    assert select_media_ids(tree, "section:section") == ("official", "manual", "nested")


def test_invalid_scope_never_falls_back_to_global_removal():
    for scope in ("", "section", "section:missing", "subsection:section", "media:manual"):
        assert select_media_ids(_tree(), scope) == ()


def test_filter_preserves_structure_markers_and_unselected_occurrences():
    tree = _tree()
    filtered = without_media(tree, {"manual", "nested"})
    assert select_media_ids(filtered) == ("root-media", "official")
    assert filtered[1]["children"][1]["children"] == [{"id": "marker", "type": "marker"}]
    assert select_media_ids(tree) == ("root-media", "official", "manual", "nested")
