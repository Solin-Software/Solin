"""Build the JW catalog placement snapshot for a meeting tree."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from .tree_types import Node


class MeetingPlacementItem(TypedDict):
    id: Any
    url: str
    key_symbol: str
    track: int
    issue_tag: int
    doc_id: int
    meps_language: int
    language: str
    jw_media_id: str


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
    *,
    section_title: Callable[[Node], str] | None = None,
) -> MeetingCatalogPlaylistRef:
    """Project a meeting tree into the placement contract consumed by JW dialogs."""
    items: list[MeetingPlacementItem] = []
    sections: list[MeetingPlacementSection] = []

    def visit(children: Sequence[Node], parent_section_id: str = "") -> None:
        for node in children:
            node_type = node.get("type", "")
            if node_type == "media":
                ref = node.get("media_ref") or {}
                items.append(
                    {
                        "id": node.get("id", ""),
                        "url": str(ref.get("file_path") or ""),
                        "key_symbol": str(ref.get("key_symbol") or ""),
                        "track": int(ref.get("track") or 0),
                        "issue_tag": int(ref.get("issue_tag") or 0),
                        "doc_id": int(ref.get("meps_doc_id") or 0),
                        "meps_language": int(ref.get("meps_language") or 0),
                        "language": str(ref.get("language") or ""),
                        "jw_media_id": str(ref.get("jw_media_id") or ""),
                    }
                )
                continue
            if node_type in ("section", "subsection"):
                node_id = str(node.get("id", ""))
                sections.append(
                    {
                        "id": node_id,
                        "name": (
                            section_title(node)
                            if section_title is not None
                            else node.get("title", "")
                        ),
                        "parent_id": parent_section_id or None,
                        "color_hue": int(node.get("color_hue", 215)),
                    }
                )
                visit(node.get("children", []), node_id)
                continue
            visit(node.get("children", []), parent_section_id)

    visit(nodes)
    return {"items": items, "sections": sections}
