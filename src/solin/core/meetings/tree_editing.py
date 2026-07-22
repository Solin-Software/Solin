"""Pure editing helpers for mutable meeting media trees."""

from __future__ import annotations

from .tree_types import Node


def parse_tree_list_id(list_id: str) -> tuple[str, str]:
    if list_id == "root" or not list_id:
        return "root", ""
    if ":" not in list_id:
        return "", ""
    kind, node_id = list_id.split(":", 1)
    return kind, node_id


def find_tree_node(
    nodes: list[Node],
    node_id: str,
    children: list[Node] | None = None,
) -> Node | None:
    for node in nodes if children is None else children:
        if node.get("id") == node_id:
            return node
        found = find_tree_node(nodes, node_id, node.get("children", []))
        if found is not None:
            return found
    return None


def children_for_tree_target(
    nodes: list[Node],
    kind: str,
    node_id: str,
) -> list[Node] | None:
    if kind == "root":
        return nodes
    target = find_tree_node(nodes, node_id)
    if not target or target.get("type") != kind:
        return None
    return target.setdefault("children", [])


def replace_tree_node(
    nodes: list[Node],
    node_id: str,
    replacement: list[Node],
) -> bool:
    def visit(children: list[Node]) -> bool:
        for index, child in enumerate(children):
            if child.get("id") == node_id:
                children[index:index + 1] = replacement
                return True
            if visit(child.get("children", [])):
                return True
        return False

    return visit(nodes)


def media_descendants(node: Node) -> list[Node]:
    result: list[Node] = []
    for child in node.get("children", []):
        if child.get("type") == "media":
            result.append(child)
        else:
            result.extend(media_descendants(child))
    return result


def can_drop_tree_node(node_type: str, target_list_id: str) -> bool:
    kind, _ = parse_tree_list_id(target_list_id)
    allowed_types = {
        "root": {"media", "section"},
        "section": {"media", "subsection"},
        "subsection": {"media", "marker"},
    }
    return node_type in allowed_types.get(kind, set())


def move_tree_node(
    nodes: list[Node],
    node_id: str,
    target_list_id: str,
    insert_index: int,
) -> bool:
    """Move *node_id* into a post-removal insertion slot.

    The tree UI replaces the dragged node with a placeholder before it computes
    the destination index.  Consequently ``insert_index`` already addresses the
    destination list with the source node removed.  Keeping that contract here
    avoids adjusting same-parent downward moves twice.
    """

    source, parent_children, original_index = _pop_node_with_parent(nodes, node_id)
    if source is None or parent_children is None or original_index < 0:
        return False
    if not can_drop_tree_node(str(source.get("type", "")), target_list_id):
        parent_children.insert(original_index, source)
        return False

    kind, target_id = parse_tree_list_id(target_list_id)
    if target_id and _contains_node(source, target_id):
        parent_children.insert(original_index, source)
        return False
    target_children = children_for_tree_target(nodes, kind, target_id)
    if target_children is None:
        parent_children.insert(original_index, source)
        return False
    if not _insert_existing_tree_node(nodes, source, kind, target_id, insert_index):
        parent_children.insert(original_index, source)
        return False
    return True


def _pop_node_with_parent(
    nodes: list[Node],
    node_id: str,
) -> tuple[Node | None, list[Node] | None, int]:
    def visit(children: list[Node]) -> tuple[Node | None, list[Node] | None, int]:
        for index, child in enumerate(children):
            if child.get("id") == node_id:
                return children.pop(index), children, index
            found, parent, found_index = visit(child.get("children", []))
            if found is not None:
                return found, parent, found_index
        return None, None, -1

    return visit(nodes)


def _insert_existing_tree_node(
    nodes: list[Node],
    node: Node,
    target_kind: str,
    target_id: str,
    insert_index: int,
) -> bool:
    target_children = children_for_tree_target(nodes, target_kind, target_id)
    if target_children is None:
        return False
    index = max(0, min(insert_index, len(target_children)))
    target_children.insert(index, node)
    return True


def _contains_node(node: Node, target_id: str) -> bool:
    if not target_id:
        return False
    if node.get("id") == target_id:
        return True
    return any(
        _contains_node(child, target_id)
        for child in node.get("children", [])
    )
