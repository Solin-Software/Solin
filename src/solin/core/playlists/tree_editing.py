"""Pure conversion and editing helpers for persisted playlist trees."""

from __future__ import annotations

from typing import Any

from solin.core.meetings.tree_editing import (
    can_drop_tree_node,
    children_for_tree_target,
    find_tree_node,
    move_tree_node,
    parse_tree_list_id,
)


PlaylistNode = dict[str, Any]
_POSITION_FALLBACK = 1_000_000_000


def build_playlist_tree(playlist: dict[str, Any]) -> list[PlaylistNode]:
    """Return the ordered nested form of compact playlist persistence data."""

    items = list(playlist.get("items", []))
    sections = list(playlist.get("sections", []))
    sections_by_id = {str(section["id"]): section for section in sections}
    markers_by_subsection: dict[str, list[dict[str, Any]]] = {}
    for marker in playlist.get("markers", []):
        subsection_id = str(marker.get("subsection_id") or "")
        subsection = sections_by_id.get(subsection_id)
        if subsection and subsection.get("parent_id"):
            markers_by_subsection.setdefault(subsection_id, []).append(marker)

    top_sections = [section for section in sections if not section.get("parent_id")]
    subsections_by_parent: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        parent_id = str(section.get("parent_id") or "")
        if parent_id:
            subsections_by_parent.setdefault(parent_id, []).append(section)

    first_index = _first_index_by_section(items)

    def media_node(item: dict[str, Any]) -> PlaylistNode:
        return {"id": item["id"], "type": "media", "ref": item, "children": []}

    def marker_node(marker: dict[str, Any]) -> PlaylistNode:
        return {"id": marker["id"], "type": "marker", "ref": marker, "children": []}

    def subsection_children(section_id: str) -> list[PlaylistNode]:
        ordered: list[tuple[float, int, int, int, str, dict[str, Any]]] = []
        for index, item in enumerate(items):
            if item.get("section_id") == section_id:
                ordered.append((float(index), 1, 0, index, "media", item))
        for fallback, marker in enumerate(markers_by_subsection.get(section_id, [])):
            position = marker.get("position")
            if not isinstance(position, int) or isinstance(position, bool):
                position = _POSITION_FALLBACK + fallback
            slot_order = marker.get("slot_order")
            if not isinstance(slot_order, int) or isinstance(slot_order, bool):
                slot_order = fallback
            ordered.append((float(position), 0, slot_order, fallback, "marker", marker))
        ordered.sort(key=lambda entry: entry[:4])
        return [
            media_node(ref) if node_type == "media" else marker_node(ref)
            for *_order, node_type, ref in ordered
        ]

    def section_node(section: dict[str, Any], *, subsection: bool) -> PlaylistNode:
        section_id = str(section["id"])
        children = subsection_children(section_id) if subsection else []
        if not subsection:
            nested_sections = subsections_by_parent.get(section_id, [])
            for node_type, ref in _ordered_section_children(
                section_id,
                nested_sections,
                items,
                first_index,
            ):
                children.append(
                    media_node(ref)
                    if node_type == "media"
                    else section_node(ref, subsection=True)
                )
        return {
            "id": section_id,
            "type": "subsection" if subsection else "section",
            "ref": section,
            "children": children,
        }

    root_nodes: list[tuple[tuple[float, int, int, int], PlaylistNode]] = []
    for index, item in enumerate(items):
        if not item.get("section_id"):
            root_nodes.append((_media_order_key(index), media_node(item)))
    for fallback, section in enumerate(top_sections):
        root_nodes.append((
            _section_order_key(section, fallback, len(items), first_index),
            section_node(section, subsection=False),
        ))
    root_nodes.sort(key=lambda entry: entry[0])
    return [node for _order, node in root_nodes]


def flatten_playlist_tree(
    playlist: dict[str, Any],
    nodes: list[PlaylistNode],
) -> None:
    """Persist an ordered nested tree back into the compact playlist schema."""

    items: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    marker_slots: dict[tuple[str, int], int] = {}
    section_slots: dict[tuple[str, int], int] = {}
    item_position = 0

    def walk(children: list[PlaylistNode], parent_section_id: str | None = None) -> None:
        nonlocal item_position
        list_id = parent_section_id or "root"
        for node in children:
            ref = node["ref"]
            node_type = str(node["type"])
            if node_type == "media":
                ref["section_id"] = parent_section_id
                items.append(ref)
                item_position += 1
            elif node_type == "marker" and parent_section_id:
                ref["subsection_id"] = parent_section_id
                ref["position"] = item_position
                key = (parent_section_id, item_position)
                ref["slot_order"] = marker_slots.get(key, 0)
                marker_slots[key] = int(ref["slot_order"]) + 1
                markers.append(ref)
            elif node_type in {"section", "subsection"}:
                ref["parent_id"] = parent_section_id if node_type == "subsection" else None
                ref["position"] = item_position
                key = (list_id, item_position)
                ref["slot_order"] = section_slots.get(key, 0)
                section_slots[key] = int(ref["slot_order"]) + 1
                sections.append(ref)
                walk(node.get("children", []), str(ref["id"]))

    walk(nodes)
    playlist["items"] = items
    playlist["sections"] = sections
    playlist["markers"] = markers


def can_drop_playlist_node(
    playlist: dict[str, Any],
    node_id: str,
    node_type: str,
    target_list_id: str,
) -> bool:
    if not can_drop_tree_node(node_type, target_list_id):
        return False
    if node_type != "marker":
        return True
    _kind, target_id = parse_tree_list_id(target_list_id)
    marker = next(
        (
            candidate
            for candidate in playlist.get("markers", [])
            if candidate.get("id") == node_id
        ),
        None,
    )
    return bool(marker and marker.get("subsection_id") == target_id)


def move_playlist_node(
    playlist: dict[str, Any],
    node_id: str,
    target_list_id: str,
    insert_index: int,
) -> bool:
    tree = build_playlist_tree(playlist)
    source = find_tree_node(tree, node_id)
    if source is None or not can_drop_playlist_node(
        playlist,
        node_id,
        str(source.get("type") or ""),
        target_list_id,
    ):
        return False
    if not move_tree_node(tree, node_id, target_list_id, insert_index):
        return False
    flatten_playlist_tree(playlist, tree)
    return True


def insert_playlist_media(
    playlist: dict[str, Any],
    target_list_id: str,
    insert_index: int,
    media_items: list[dict[str, Any]],
) -> bool:
    if not media_items or not can_drop_tree_node("media", target_list_id):
        return False
    tree = build_playlist_tree(playlist)
    kind, target_id = parse_tree_list_id(target_list_id)
    target = children_for_tree_target(tree, kind, target_id)
    if target is None:
        return False
    row = max(0, min(insert_index, len(target)))
    nodes = [
        {"id": item["id"], "type": "media", "ref": item, "children": []}
        for item in media_items
    ]
    target[row:row] = nodes
    flatten_playlist_tree(playlist, tree)
    return True


def flat_media_insert_index(
    playlist: dict[str, Any],
    target_list_id: str,
    insert_index: int,
) -> int:
    tree = build_playlist_tree(playlist)
    target_kind, target_id = parse_tree_list_id(target_list_id)
    count = 0

    def media_count(node: PlaylistNode) -> int:
        if node["type"] == "media":
            return 1
        return sum(media_count(child) for child in node.get("children", []))

    def visit(children: list[PlaylistNode], kind: str, node_id: str) -> int | None:
        nonlocal count
        if kind == target_kind and node_id == target_id:
            row = max(0, min(insert_index, len(children)))
            return count + sum(media_count(child) for child in children[:row])
        for child in children:
            if child["type"] == "media":
                count += 1
            else:
                found = visit(child.get("children", []), str(child["type"]), str(child["id"]))
                if found is not None:
                    return found
        return None

    result = visit(tree, "root", "")
    return len(playlist.get("items", [])) if result is None else result


def _media_order_key(index: int) -> tuple[float, int, int, int]:
    return (float(index), 1, 0, index)


def _section_order_key(
    section: dict[str, Any],
    fallback: int,
    item_count: int,
    first_index: dict[str, int],
) -> tuple[float, int, int, int]:
    position = section.get("position")
    if not isinstance(position, int) or isinstance(position, bool):
        position = first_index.get(str(section["id"]), item_count + fallback + 1)
    slot_order = section.get("slot_order")
    if not isinstance(slot_order, int) or isinstance(slot_order, bool):
        slot_order = fallback
    return (float(position), 0, slot_order, fallback)


def _first_index_by_section(items: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, item in enumerate(items):
        section_id = str(item.get("section_id") or "")
        if section_id:
            result.setdefault(section_id, index)
    return result


def _ordered_section_children(
    section_id: str,
    subsections: list[dict[str, Any]],
    items: list[dict[str, Any]],
    first_index: dict[str, int],
) -> list[tuple[str, dict[str, Any]]]:
    ordered: list[tuple[tuple[float, int, int, int], str, dict[str, Any]]] = []
    for index, item in enumerate(items):
        if item.get("section_id") == section_id:
            ordered.append((_media_order_key(index), "media", item))
    for fallback, subsection in enumerate(subsections):
        ordered.append((
            _section_order_key(subsection, fallback, len(items), first_index),
            "subsection",
            subsection,
        ))
    ordered.sort(key=lambda entry: entry[0])
    return [(node_type, ref) for _order, node_type, ref in ordered]
