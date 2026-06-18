"""Build the JW catalog placement snapshot for a meeting tree."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

from .tree_types import Node


class MeetingPlacementItem(TypedDict):
    id: Any


class MeetingPlacementSection(TypedDict):
    id: str
    name: Any
    parent_id: str | None
    color_hue: int


class MeetingCatalogPlaylistRef(TypedDict):
    items: list[MeetingPlacementItem]
    sections: list[MeetingPlacementSection]


def build_meeting_catalog_playlist_ref(
    nodes: Sequence[Node],
) -> MeetingCatalogPlaylistRef:
    """Project a meeting tree into the placement contract consumed by JW dialogs."""
    items: list[MeetingPlacementItem] = []
    sections: list[MeetingPlacementSection] = []

    def visit(children: Sequence[Node], parent_section_id: str = "") -> None:
        for node in children:
            node_type = node.get("type", "")
            if node_type == "media":
                items.append({"id": node.get("id", "")})
                continue
            if node_type in ("section", "subsection"):
                node_id = str(node.get("id", ""))
                sections.append(
                    {
                        "id": node_id,
                        "name": node.get("title", ""),
                        "parent_id": parent_section_id or None,
                        "color_hue": int(node.get("color_hue", 215)),
                    }
                )
                visit(node.get("children", []), node_id)
                continue
            visit(node.get("children", []), parent_section_id)

    visit(nodes)
    return {"items": items, "sections": sections}
