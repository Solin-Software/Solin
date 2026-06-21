"""Detect and describe safe incremental changes in tree-shaped UI data."""
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


@dataclass(frozen=True, slots=True)
class TreeNodeInsert:
    target_list_id: str
    insert_index: int
    nodes: list[TreeNode]


@dataclass(frozen=True, slots=True)
class TreeNodeRemove:
    node_id: str


@dataclass(frozen=True, slots=True)
class TreeSectionUpdate:
    node_id: str
    metadata_changed: bool
    collapsed_changed: bool


@dataclass(frozen=True, slots=True)
class TreeIncrementalChanges:
    moves: list[TreeNodeMove]
    inserts: list[TreeNodeInsert]
    removals: list[TreeNodeRemove]
    section_updates: list[TreeSectionUpdate]

    @property
    def has_changes(self) -> bool:
        return bool(
            self.moves
            or self.inserts
            or self.removals
            or self.section_updates
        )


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


def incremental_tree_changes(
    current_nodes: list[TreeNode],
    desired_nodes: list[TreeNode],
    *,
    ignored_payload_keys: set[str] | frozenset[str] | None = None,
    section_patch_keys: set[str] | frozenset[str] | None = None,
) -> TreeIncrementalChanges | None:
    """Return a conservative patch set from *current_nodes* to *desired_nodes*.

    ``None`` means the change is too broad or ambiguous for incremental UI
    patching and callers should fall back to a full rebuild.
    """
    ignored = frozenset(ignored_payload_keys or ())
    section_keys = frozenset(section_patch_keys or ())
    current_index = _index_nodes(current_nodes)
    desired_index = _index_nodes(desired_nodes)
    if current_index is None or desired_index is None:
        return None

    current_ids = set(current_index)
    desired_ids = set(desired_index)
    shared_ids = current_ids & desired_ids
    added_ids = desired_ids - current_ids
    removed_ids = current_ids - desired_ids

    section_updates: list[TreeSectionUpdate] = []
    allowed_section_ignored = ignored | section_keys
    for node_id in shared_ids:
        current_node = current_index[node_id].node
        desired_node = desired_index[node_id].node
        if current_node.get("type") != desired_node.get("type"):
            return None

        if _normalized_payload(current_node, ignored) == _normalized_payload(
            desired_node,
            ignored,
        ):
            continue

        is_section = str(current_node.get("type") or "") in {"section", "subsection"}
        if not is_section:
            return None
        if _normalized_payload(current_node, allowed_section_ignored) != _normalized_payload(
            desired_node,
            allowed_section_ignored,
        ):
            return None

        collapsed_changed = (
            _first_key_value(current_node, "collapsed")
            != _first_key_value(desired_node, "collapsed")
        )
        metadata_changed = (
            _normalized_payload(current_node, ignored | {"collapsed"})
            != _normalized_payload(desired_node, ignored | {"collapsed"})
        )
        section_updates.append(TreeSectionUpdate(
            node_id=node_id,
            metadata_changed=metadata_changed,
            collapsed_changed=collapsed_changed,
        ))

    if not added_ids and not removed_ids:
        moves = reorder_only_moves(
            current_nodes,
            desired_nodes,
            ignored_payload_keys=allowed_section_ignored,
        )
        if moves is None:
            return None
        return TreeIncrementalChanges(
            moves=moves,
            inserts=[],
            removals=[],
            section_updates=_sorted_section_updates(section_updates, desired_index),
        )

    if not _existing_children_stay_aligned(
        current_nodes,
        desired_nodes,
        added_ids,
        removed_ids,
    ):
        return None

    return TreeIncrementalChanges(
        moves=[],
        inserts=_insertions_for_added_nodes(desired_nodes, added_ids),
        removals=_removals_for_removed_nodes(current_index, removed_ids),
        section_updates=_sorted_section_updates(section_updates, desired_index),
    )


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


@dataclass(frozen=True, slots=True)
class _NodeInfo:
    node: TreeNode
    parent_list_id: str
    index: int
    depth: int
    ancestor_ids: tuple[str, ...]


def _index_nodes(nodes: list[TreeNode]) -> dict[str, _NodeInfo] | None:
    indexed: dict[str, _NodeInfo] = {}

    def visit(
        children: list[TreeNode],
        parent_list_id: str,
        depth: int,
        ancestor_ids: tuple[str, ...],
    ) -> bool:
        for index, node in enumerate(children):
            node_id = str(node.get("id") or "")
            if not node_id or node_id in indexed:
                return False
            indexed[node_id] = _NodeInfo(
                node=node,
                parent_list_id=parent_list_id,
                index=index,
                depth=depth,
                ancestor_ids=ancestor_ids,
            )
            child_list_id = _child_list_id(node)
            grandchildren = node.get("children", [])
            if child_list_id and isinstance(grandchildren, list):
                if not visit(grandchildren, child_list_id, depth + 1, ancestor_ids + (node_id,)):
                    return False
        return True

    return indexed if visit(nodes, "root", 0, ()) else None


def _existing_children_stay_aligned(
    current_nodes: list[TreeNode],
    desired_nodes: list[TreeNode],
    added_ids: set[str],
    removed_ids: set[str],
) -> bool:
    current_lists = _children_by_list(current_nodes)
    desired_lists = _children_by_list(desired_nodes)
    if current_lists is None or desired_lists is None:
        return False

    for list_id in set(current_lists) | set(desired_lists):
        current_existing = [
            node_id
            for node_id in current_lists.get(list_id, ())
            if node_id not in removed_ids
        ]
        desired_existing = [
            node_id
            for node_id in desired_lists.get(list_id, ())
            if node_id not in added_ids
        ]
        if current_existing != desired_existing:
            return False
    return True


def _children_by_list(nodes: list[TreeNode]) -> dict[str, tuple[str, ...]] | None:
    by_list: dict[str, tuple[str, ...]] = {}

    def visit(children: list[TreeNode], list_id: str) -> bool:
        ids: list[str] = []
        for node in children:
            node_id = str(node.get("id") or "")
            if not node_id:
                return False
            ids.append(node_id)
        by_list[list_id] = tuple(ids)
        for node in children:
            child_list_id = _child_list_id(node)
            grandchildren = node.get("children", [])
            if child_list_id and isinstance(grandchildren, list):
                if not visit(grandchildren, child_list_id):
                    return False
        return True

    return by_list if visit(nodes, "root") else None


def _insertions_for_added_nodes(
    desired_nodes: list[TreeNode],
    added_ids: set[str],
) -> list[TreeNodeInsert]:
    inserts: list[TreeNodeInsert] = []

    def visit(children: list[TreeNode], list_id: str) -> None:
        index = 0
        while index < len(children):
            node = children[index]
            node_id = str(node.get("id") or "")
            if node_id in added_ids:
                start = index
                group: list[TreeNode] = []
                while index < len(children):
                    candidate = children[index]
                    candidate_id = str(candidate.get("id") or "")
                    if candidate_id not in added_ids:
                        break
                    group.append(candidate)
                    index += 1
                inserts.append(TreeNodeInsert(list_id, start, copy.deepcopy(group)))
                continue

            child_list_id = _child_list_id(node)
            grandchildren = node.get("children", [])
            if child_list_id and isinstance(grandchildren, list):
                visit(grandchildren, child_list_id)
            index += 1

    visit(desired_nodes, "root")
    return inserts


def _removals_for_removed_nodes(
    current_index: dict[str, _NodeInfo],
    removed_ids: set[str],
) -> list[TreeNodeRemove]:
    removals = [
        info
        for node_id, info in current_index.items()
        if node_id in removed_ids and not any(parent in removed_ids for parent in info.ancestor_ids)
    ]
    removals.sort(key=lambda info: (info.depth, info.index), reverse=True)
    return [
        TreeNodeRemove(str(info.node.get("id") or ""))
        for info in removals
    ]


def _sorted_section_updates(
    updates: list[TreeSectionUpdate],
    desired_index: dict[str, _NodeInfo],
) -> list[TreeSectionUpdate]:
    return sorted(
        updates,
        key=lambda update: (
            desired_index[update.node_id].depth,
            desired_index[update.node_id].index,
            update.node_id,
        ),
    )


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


def _first_key_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return _normalized_payload(value[key], frozenset())
        for child_key, child in sorted(value.items(), key=lambda item: str(item[0])):
            if child_key == "children":
                continue
            found = _first_key_value(child, key)
            if found is not _MISSING:
                return found
    if isinstance(value, (list, tuple)):
        for child in value:
            found = _first_key_value(child, key)
            if found is not _MISSING:
                return found
    return _MISSING


_MISSING = object()


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
