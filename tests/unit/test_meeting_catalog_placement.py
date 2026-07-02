from __future__ import annotations

import copy

from solin.core.meetings.catalog_placement import (
    build_meeting_catalog_playlist_ref,
)


def test_empty_meeting_tree_has_empty_catalog_placement():
    assert build_meeting_catalog_playlist_ref([]) == {
        "items": [],
        "sections": [],
    }


def test_catalog_placement_preserves_depth_first_order_and_section_parents():
    nodes = [
        {
            "id": "section-1",
            "type": "section",
            "title": "Section 1",
            "color_hue": "210",
            "children": [
                {"id": "media-1", "type": "media", "children": []},
                {
                    "id": "subsection-1",
                    "type": "subsection",
                    "title": "Subsection 1",
                    "children": [
                        {"id": "media-2", "type": "media", "children": []},
                    ],
                },
            ],
        },
        {"id": "media-3", "type": "media", "children": []},
    ]

    result = build_meeting_catalog_playlist_ref(nodes)

    def placement_item(item_id: str) -> dict:
        return {
            "id": item_id,
            "url": "",
            "key_symbol": "",
            "track": 0,
            "issue_tag": 0,
            "doc_id": 0,
            "meps_language": 0,
            "language": "",
            "jw_media_id": "",
        }

    assert result == {
        "items": [
            placement_item("media-1"),
            placement_item("media-2"),
            placement_item("media-3"),
        ],
        "sections": [
            {
                "id": "section-1",
                "name": "Section 1",
                "parent_id": None,
                "color_hue": 210,
            },
            {
                "id": "subsection-1",
                "name": "Subsection 1",
                "parent_id": "section-1",
                "color_hue": 215,
            },
        ],
    }


def test_unknown_container_is_transparent_and_input_is_not_modified():
    nodes = [
        {
            "id": "section",
            "type": "section",
            "title": "Section",
            "children": [
                {
                    "id": "wrapper",
                    "type": "unknown",
                    "children": [
                        {
                            "id": "subsection",
                            "type": "subsection",
                            "title": "Subsection",
                            "children": [],
                        }
                    ],
                }
            ],
        }
    ]
    original = copy.deepcopy(nodes)

    result = build_meeting_catalog_playlist_ref(nodes)

    assert result["sections"][1]["parent_id"] == "section"
    assert nodes == original
