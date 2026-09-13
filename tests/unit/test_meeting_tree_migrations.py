from __future__ import annotations

from copy import deepcopy

import pytest

from solin.core.meetings.tree_migrations import migrate_publication_subsections
from solin.core.meetings.tree_types import Node, publication_subsection_key, stable_node_id


@pytest.mark.parametrize("modern_first", [False, True])
def test_mixed_publication_identities_preserve_modern_edits_and_manual_union(
    modern_first: bool,
) -> None:
    base = "subsection:ref:tgw:w"
    key = publication_subsection_key(base, "Publication 2021")
    media: Node = {
        "id": "media-2021",
        "type": "media",
        "meeting_generated": True,
        "meeting_source_key": "media:ref:tgw:w:2021:mm1",
        "children": [],
    }
    canonical: Node = {
        "id": stable_node_id(key),
        "type": "subsection",
        "meeting_generated": True,
        "meeting_source_key": key,
        "title": "Publication 2021",
        "children": [media],
    }
    modern = deepcopy(canonical)
    modern.update(title="My title", user_title_override=True, color_hue=120)
    modern["children"][0]["start_trim_ticks"] = 25
    shared_manual: Node = {"id": "manual-shared", "type": "marker", "text": "Modern"}
    modern["children"].append(shared_manual)
    legacy = deepcopy(canonical)
    legacy.update(id=stable_node_id(base), meeting_source_key=base)
    legacy["children"].extend(
        [
            {"id": "manual-shared", "type": "marker", "text": "Old"},
            {"id": "manual-old-only", "type": "marker", "text": "Keep"},
        ]
    )
    nodes = [modern, legacy] if modern_first else [legacy, modern]

    assert migrate_publication_subsections(nodes, [canonical], set())

    assert len(nodes) == 1
    assert nodes[0]["title"] == "My title"
    assert nodes[0]["user_title_override"] is True
    assert nodes[0]["color_hue"] == 120
    assert [child["id"] for child in nodes[0]["children"]] == [
        "media-2021",
        "manual-shared",
        "manual-old-only",
    ]
    assert nodes[0]["children"][0]["start_trim_ticks"] == 25
    assert nodes[0]["children"][1]["text"] == "Modern"
    assert not migrate_publication_subsections(nodes, [canonical], set())


@pytest.mark.parametrize("children_moved", [False, True])
def test_renamed_empty_publication_uses_original_source_hash(children_moved: bool) -> None:
    base = "subsection:ref:tgw:w"
    canonical: list[Node] = []
    for year in (2021, 2025):
        key = publication_subsection_key(base, f"Publication {year}")
        canonical.append(
            {
                "id": stable_node_id(key),
                "type": "subsection",
                "meeting_generated": True,
                "meeting_source_key": key,
                "meeting_source_hash": f"original-{year}",
                "title": f"Publication {year}",
                "children": [
                    {
                        "id": f"media-{year}",
                        "type": "media",
                        "meeting_generated": True,
                        "meeting_source_key": f"media:ref:tgw:w:{year}:mm1",
                        "children": [],
                    }
                ],
            }
        )
    legacy = deepcopy(canonical[1])
    legacy.update(
        id=stable_node_id(base),
        meeting_source_key=base,
        title="Renamed second edition",
        user_title_override=True,
        children=[{"id": "manual", "type": "marker"}],
    )
    nodes = [legacy]
    if children_moved:
        nodes.extend(deepcopy(canonical[1]["children"]))

    assert migrate_publication_subsections(nodes, canonical, set())

    assert nodes[0]["meeting_source_key"] == canonical[1]["meeting_source_key"]
    assert nodes[0]["title"] == "Renamed second edition"
    assert nodes[0]["children"] == [{"id": "manual", "type": "marker"}]
    assert len(nodes) == (2 if children_moved else 1)
