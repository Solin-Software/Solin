"""Canonical meeting-tree comparison and restoration."""
from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass

from .tree_editing import can_drop_tree_node
from .tree_merger import overlay_durable_media_state
from .tree_types import Node, clone_nodes, iter_nodes

_ROOT = "root"


@dataclass(frozen=True, slots=True)
class CanonicalTreeDiff:
    hidden: int = 0
    renamed: int = 0
    recolored: int = 0
    moved: int = 0
    stale: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(
            self.hidden
            or self.renamed
            or self.recolored
            or self.moved
            or self.stale
        )


@dataclass(slots=True)
class _NodeRecord:
    node: Node
    parent_token: str
    siblings: list[Node]
    sequence: int


def canonical_source_keys(canonical: list[Node]) -> set[str]:
    return {
        str(node.get("meeting_source_key") or "")
        for node in iter_nodes(canonical)
        if node.get("meeting_generated") and node.get("meeting_source_key")
    }


def canonical_tree_diff(
    canonical: list[Node],
    current: list[Node],
    deleted_source_keys: set[str] | None = None,
) -> CanonicalTreeDiff:
    """Describe user-visible divergence from an immutable canonical baseline."""
    if not canonical:
        return CanonicalTreeDiff()

    canonical_records = _generated_records(canonical)
    current_records = _generated_records(current)
    canonical_keys = set(canonical_records)
    current_keys = set(current_records)
    deleted = set(deleted_source_keys or ()) & canonical_keys
    missing = canonical_keys - current_keys

    renamed = 0
    recolored = 0
    moved_keys: set[str] = set()
    for key in canonical_keys & current_keys:
        expected = canonical_records[key]
        actual = current_records[key]
        if expected.node.get("type") != actual.node.get("type"):
            moved_keys.add(key)
            continue
        node_type = str(expected.node.get("type") or "")
        if node_type == "marker":
            if actual.node.get("text") != expected.node.get("text"):
                renamed += 1
        elif node_type in {"section", "subsection"}:
            if actual.node.get("title") != expected.node.get("title"):
                renamed += 1
            if actual.node.get("color_hue") != expected.node.get("color_hue"):
                recolored += 1
        elif node_type == "media" and actual.node.get("user_title_override"):
            renamed += 1
        if actual.parent_token != expected.parent_token:
            moved_keys.add(key)

    canonical_orders = _generated_child_orders(canonical, canonical_keys)
    current_orders = _generated_child_orders(current, canonical_keys)
    for parent_token, expected_order in canonical_orders.items():
        visible_expected = [
            key
            for key in expected_order
            if key in current_keys and key not in moved_keys
        ]
        actual_order = [
            key
            for key in current_orders.get(parent_token, [])
            if key in canonical_keys and key not in moved_keys
        ]
        if actual_order == visible_expected:
            continue
        for index, key in enumerate(actual_order):
            if index >= len(visible_expected) or visible_expected[index] != key:
                moved_keys.add(key)

    return CanonicalTreeDiff(
        hidden=len(missing | deleted),
        renamed=renamed,
        recolored=recolored,
        moved=len(moved_keys),
        stale=len(current_keys - canonical_keys),
    )


def restore_canonical_tree(
    canonical: list[Node],
    current: list[Node],
    *,
    hidden_canonical_media: dict[str, Node] | None = None,
    orphan_section_source_key: str = "",
    orphan_media_ids: set[str] | None = None,
) -> list[Node]:
    """Restore generated content while retaining all manual nodes and durable media state."""
    if not canonical:
        return clone_nodes(current)

    restored = clone_nodes(canonical)
    canonical_records = _all_records(restored)
    current_records = _all_records(current)
    hidden_media = hidden_canonical_media or {}

    for key, target_record in canonical_records.generated.items():
        saved_record = current_records.generated.get(key)
        saved = saved_record.node if saved_record is not None else hidden_media.get(key)
        if saved is None:
            continue
        target = target_record.node
        target["id"] = saved.get("id") or target.get("id")
        if target.get("type") in {"section", "subsection"}:
            target["collapsed"] = bool(saved.get("collapsed", target.get("collapsed", False)))
        overlay_durable_media_state(
            saved,
            target,
            prefer_saved_resolution=True,
        )

    manual_nodes = {
        node_id: copy.deepcopy(record.node)
        for node_id, record in current_records.manual.items()
    }
    for node in manual_nodes.values():
        node["children"] = []

    target_nodes: dict[str, Node | None] = {_ROOT: None}
    target_nodes.update({key: record.node for key, record in canonical_records.generated.items()})
    target_nodes.update({f"manual:{node_id}": node for node_id, node in manual_nodes.items()})

    attachments: dict[str, list[tuple[_NodeRecord, Node]]] = defaultdict(list)
    fallback_token = (
        orphan_section_source_key
        if orphan_section_source_key in canonical_records.generated
        else ""
    )
    folder_import_ids = set(orphan_media_ids or ())
    for node_id, record in current_records.manual.items():
        node = manual_nodes[node_id]
        target_token = _resolve_manual_parent(
            record,
            current_records,
            target_nodes,
            str(node.get("type") or ""),
            fallback_token,
            node_id in folder_import_ids,
        )
        attachments[target_token].append((record, node))

    for target_token, entries in attachments.items():
        target_parent = target_nodes.get(target_token)
        target_children = restored if target_parent is None else target_parent.setdefault("children", [])
        canonical_children = list(target_children)
        canonical_index = {
            str(child.get("meeting_source_key") or ""): index
            for index, child in enumerate(canonical_children)
            if child.get("meeting_generated") and child.get("meeting_source_key")
        }
        slots: list[list[Node]] = [[] for _ in range(len(canonical_children) + 1)]
        ordered_entries = sorted(entries, key=lambda entry: entry[0].sequence)
        preferred_slots = [
            _manual_slot(record, target_token, canonical_index, current_records)
            for record, _node in ordered_entries
        ]
        stable_slots = _monotonic_slots(preferred_slots, len(canonical_children))
        for (_record, node), slot in zip(ordered_entries, stable_slots, strict=True):
            slots[max(0, min(slot, len(canonical_children)))].append(node)
        merged: list[Node] = []
        for index, canonical_child in enumerate(canonical_children):
            merged.extend(slots[index])
            merged.append(canonical_child)
        merged.extend(slots[-1])
        target_children[:] = merged

    return restored


@dataclass(slots=True)
class _TreeRecords:
    generated: dict[str, _NodeRecord]
    manual: dict[str, _NodeRecord]
    parents: dict[str, str]


def _all_records(nodes: list[Node]) -> _TreeRecords:
    generated: dict[str, _NodeRecord] = {}
    manual: dict[str, _NodeRecord] = {}
    parents: dict[str, str] = {}
    sequence = 0

    def visit(children: list[Node], parent_token: str) -> None:
        nonlocal sequence
        for node in children:
            sequence += 1
            source_key = str(node.get("meeting_source_key") or "")
            node_id = str(node.get("id") or "")
            token = source_key if node.get("meeting_generated") and source_key else f"manual:{node_id}"
            record = _NodeRecord(node, parent_token, children, sequence)
            parents[token] = parent_token
            if node.get("meeting_generated") and source_key:
                generated[source_key] = record
            elif node_id:
                manual[node_id] = record
            child_list_id = _list_id(node)
            grandchildren = node.get("children", [])
            if child_list_id and isinstance(grandchildren, list):
                visit(grandchildren, token)

    visit(nodes, _ROOT)
    return _TreeRecords(generated, manual, parents)


def _generated_records(nodes: list[Node]) -> dict[str, _NodeRecord]:
    return _all_records(nodes).generated


def _generated_child_orders(nodes: list[Node], allowed_keys: set[str]) -> dict[str, list[str]]:
    orders: dict[str, list[str]] = defaultdict(list)

    def visit(children: list[Node], parent_token: str) -> None:
        for node in children:
            key = str(node.get("meeting_source_key") or "")
            if node.get("meeting_generated") and key in allowed_keys:
                orders[parent_token].append(key)
                child_parent = key
            else:
                child_parent = f"manual:{node.get('id') or ''}"
            grandchildren = node.get("children", [])
            if isinstance(grandchildren, list):
                visit(grandchildren, child_parent)

    visit(nodes, _ROOT)
    return orders


def _resolve_manual_parent(
    record: _NodeRecord,
    records: _TreeRecords,
    targets: dict[str, Node | None],
    node_type: str,
    fallback_token: str,
    allow_folder_fallback: bool,
) -> str:
    token = record.parent_token
    visited: set[str] = set()
    prefer_folder_fallback = bool(
        fallback_token
        and node_type == "media"
        and allow_folder_fallback
        and record.parent_token != _ROOT
    )
    while token and token not in visited:
        visited.add(token)
        if token == _ROOT and prefer_folder_fallback:
            break
        if token in targets and _target_accepts(targets[token], node_type):
            return token
        token = records.parents.get(token, _ROOT)
    if fallback_token and _target_accepts(targets.get(fallback_token), node_type):
        return fallback_token
    for candidate, target in targets.items():
        if candidate != _ROOT and _target_accepts(target, node_type):
            return candidate
    return _ROOT


def _manual_slot(
    record: _NodeRecord,
    target_token: str,
    canonical_index: dict[str, int],
    records: _TreeRecords,
) -> int:
    try:
        own_index = record.siblings.index(record.node)
    except ValueError:
        return len(canonical_index)
    for sibling in record.siblings[own_index + 1 :]:
        key = str(sibling.get("meeting_source_key") or "")
        sibling_record = records.generated.get(key)
        if key in canonical_index and sibling_record and sibling_record.parent_token == target_token:
            return canonical_index[key]
    for sibling in reversed(record.siblings[:own_index]):
        key = str(sibling.get("meeting_source_key") or "")
        sibling_record = records.generated.get(key)
        if key in canonical_index and sibling_record and sibling_record.parent_token == target_token:
            return canonical_index[key] + 1
    return len(canonical_index)


def _target_accepts(target: Node | None, node_type: str) -> bool:
    list_id = _ROOT if target is None else _list_id(target)
    return bool(list_id and can_drop_tree_node(node_type, list_id))


def _monotonic_slots(preferred: list[int], maximum: int) -> list[int]:
    """Project preferred anchor slots without ever reordering manual siblings."""
    blocks: list[tuple[int, int]] = []
    for value in preferred:
        blocks.append((max(0, min(value, maximum)), 1))
        while len(blocks) >= 2:
            left_total, left_count = blocks[-2]
            right_total, right_count = blocks[-1]
            if left_total * right_count <= right_total * left_count:
                break
            blocks[-2:] = [(left_total + right_total, left_count + right_count)]

    result: list[int] = []
    for total, count in blocks:
        slot = max(0, min((total + count // 2) // count, maximum))
        result.extend([slot] * count)
    return result


def _list_id(node: Node) -> str:
    node_type = str(node.get("type") or "")
    node_id = str(node.get("id") or "")
    if node_type in {"section", "subsection"} and node_id:
        return f"{node_type}:{node_id}"
    return ""


__all__ = [
    "CanonicalTreeDiff",
    "canonical_source_keys",
    "canonical_tree_diff",
    "restore_canonical_tree",
]
