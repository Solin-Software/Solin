"""Detect and describe reorder-only changes in tree-shaped UI data."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

TreeNode = dict[str, Any]


@dataclass(frozen=True, slots=True)
class TreeNodeMove:
    node_id: str
    target_list_id: str
    insert_index: int


def reorder_only_moves(
    current_nodes: list[TreeNode],
    desired_nodes: list[TreeNode],
    *,
    ignored_payload_keys: set[str] | frozenset[str] | None = None,
) -> list[TreeNodeMove] | None:
    """Return moves needed to transform *current_nodes* into *desired_nodes*.

    ``None`` means the trees differ by more than order/parent placement.
    An empty list means the trees are already equivalent.
    """
    ignored = frozenset(ignored_payload_keys or ())
    current_payloads = _node_payloads(current_nodes, ignored)
    desired_payloads = _node_payloads(desired_nodes, ignored)
    if current_payloads is None or desired_payloads is None:
        return None
    if current_payloads != desired_payloads:
        return None

    working = copy.deepcopy(current_nodes)
    moves: list[TreeNodeMove] = []
    if not _reconcile_children(working, desired_nodes, "root", moves):
        return None
    return moves


def _reconcile_children(
    working_root: list[TreeNode],
    desired_children: list[TreeNode],
    target_list_id: str,
    moves: list[TreeNodeMove],
) -> bool:
    target_children = _children_for_list(working_root, target_list_id)
    if target_children is None:
        return False

    for desired_index, desired_node in enumerate(desired_children):
        node_id = str(desired_node.get("id") or "")
        if not node_id:
            return False

        if (
            desired_index < len(target_children)
            and str(target_children[desired_index].get("id") or "") == node_id
        ):
            current_node = target_children[desired_index]
        else:
            current_node = _pop_node(working_root, node_id)
            if current_node is None:
                return False
            insert_index = max(0, min(desired_index, len(target_children)))
            target_children.insert(insert_index, current_node)
            moves.append(TreeNodeMove(node_id, target_list_id, insert_index))

        child_list_id = _child_list_id(current_node)
        desired_grandchildren = desired_node.get("children", [])
        if desired_grandchildren and not _reconcile_children(
            working_root,
            desired_grandchildren,
            child_list_id,
            moves,
        ):
            return False
    return True


def _node_payloads(
    nodes: list[TreeNode],
    ignored_payload_keys: frozenset[str],
) -> dict[str, Any] | None:
    payloads: dict[str, Any] = {}
    for node in _iter_nodes(nodes):
        node_id = str(node.get("id") or "")
        if not node_id or node_id in payloads:
            return None
        payloads[node_id] = _normalized_payload(node, ignored_payload_keys)
    return payloads


def _iter_nodes(nodes: list[TreeNode]):
    for node in nodes:
        yield node
        children = node.get("children", [])
        if isinstance(children, list):
            yield from _iter_nodes(children)


def _normalized_payload(value: Any, ignored_payload_keys: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalized_payload(child, ignored_payload_keys)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if key != "children" and str(key) not in ignored_payload_keys
        }
    if isinstance(value, (list, tuple)):
        return [
            _normalized_payload(child, ignored_payload_keys)
            for child in value
        ]
    return value


def _children_for_list(
    nodes: list[TreeNode],
    list_id: str,
) -> list[TreeNode] | None:
    kind, node_id = _parse_list_id(list_id)
    if kind == "root":
        return nodes
    target = _find_node(nodes, node_id)
    if not target or str(target.get("type") or "") != kind:
        return None
    children = target.setdefault("children", [])
    return children if isinstance(children, list) else None


def _find_node(nodes: list[TreeNode], node_id: str) -> TreeNode | None:
    for node in nodes:
        if str(node.get("id") or "") == node_id:
            return node
        found = _find_node(node.get("children", []), node_id)
        if found is not None:
            return found
    return None


def _pop_node(nodes: list[TreeNode], node_id: str) -> TreeNode | None:
    for index, node in enumerate(nodes):
        if str(node.get("id") or "") == node_id:
            return nodes.pop(index)
        found = _pop_node(node.get("children", []), node_id)
        if found is not None:
            return found
    return None


def _child_list_id(node: TreeNode) -> str:
    node_type = str(node.get("type") or "")
    node_id = str(node.get("id") or "")
    if node_type in {"section", "subsection"} and node_id:
        return f"{node_type}:{node_id}"
    return ""


def _parse_list_id(list_id: str) -> tuple[str, str]:
    if not list_id or list_id == "root":
        return "root", ""
    if ":" not in list_id:
        return "", ""
    kind, node_id = list_id.split(":", 1)
    return kind, node_id
