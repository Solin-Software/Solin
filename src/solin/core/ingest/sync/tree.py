"""Portable, globally unique tree projection with stable fractional ordering."""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from fractions import Fraction
from typing import Any

PARENT = "__sync_parent"
POSITION = "__sync_position"
ANCESTORS = "__sync_ancestors"
_RESERVED = {PARENT, POSITION, ANCESTORS}
_CONTAINERS = {"section", "subsection"}
_NODE_TYPES = _CONTAINERS | {"media", "marker"}


def _position(value: object) -> Fraction:
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("Invalid synchronized node position") from exc


def flatten_nodes(
    nodes: list[dict[str, Any]],
    baseline: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Flatten IDs globally; index shifts do not rewrite untouched positions."""
    baseline = baseline or {}
    result: dict[str, dict[str, Any]] = {}

    def visit(level: list[dict[str, Any]], ancestors: list[str]) -> None:
        parent = ancestors[0] if ancestors else ""
        # Keep a longest increasing subsequence of existing positions. Only new
        # or actually moved siblings need an ordering operation.
        candidates: list[tuple[int, Fraction]] = []
        for index, node in enumerate(level):
            if not isinstance(node, dict) or node.get("type") not in _NODE_TYPES:
                raise ValueError("Invalid synchronized node type")
            if not isinstance(node.get("children", []), list):
                raise ValueError("Synchronized node children must be a list")
            if node.get("children") and node["type"] not in _CONTAINERS:
                raise ValueError("Only a synchronized container may have children")
            previous = baseline.get(str(node.get("id") or ""), {})
            if previous and previous.get(PARENT, "") == parent:
                candidates.append((index, _position(previous.get(POSITION))))
        tails: list[tuple[Fraction, str]] = []
        tail_indices: list[int] = []
        predecessors: dict[int, int] = {}
        for index, position in candidates:
            ordering = (position, str(level[index].get("id") or ""))
            slot = bisect_left(tails, ordering)
            predecessors[index] = tail_indices[slot - 1] if slot else -1
            if slot == len(tails):
                tails.append(ordering)
                tail_indices.append(index)
            else:
                tails[slot] = ordering
                tail_indices[slot] = index
        anchors: dict[int, Fraction] = {}
        cursor = tail_indices[-1] if tail_indices else -1
        existing_positions = dict(candidates)
        while cursor != -1:
            anchors[cursor] = existing_positions[cursor]
            cursor = predecessors[cursor]
        # Equal tokens are possible after concurrent insertions. If the user
        # inserts between tied anchors, release the right anchors until a real
        # fractional gap exists; an unchanged tied order remains a no-op.
        last_index, last_position = -1, None
        for index in sorted(anchors):
            position = anchors[index]
            if last_position is not None and index > last_index + 1 and position <= last_position:
                del anchors[index]
                continue
            last_index, last_position = index, position
        assigned: dict[int, Fraction] = dict(anchors)
        left_index, left_position = -1, Fraction(0)
        for right_index in [*sorted(anchors), len(level)]:
            count = right_index - left_index - 1
            if count:
                right_position = anchors.get(right_index, left_position + count + 1)
                if left_index == -1 and right_index in anchors:
                    left_position = right_position - count - 1
                step = (right_position - left_position) / (count + 1)
                for offset in range(1, count + 1):
                    assigned[left_index + offset] = left_position + offset * step
            left_index = right_index
            left_position = anchors.get(right_index, left_position)
        for index, node in enumerate(level):
            node_id = str(node.get("id") or "")
            if not node_id:
                raise ValueError("Every synchronized tree node must have an ID")
            if node_id in result:
                raise ValueError(f"Duplicate tree node ID: {node_id}")
            value = deepcopy({key: value for key, value in node.items() if key != "children"})
            value[PARENT] = parent
            value[POSITION] = str(assigned[index])
            value[ANCESTORS] = ancestors
            result[node_id] = value
            visit(node.get("children", []), [node_id, *ancestors])

    visit(nodes, [])
    return result


def rebuild_nodes(entities: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Project one parent per ID, preserving orphans and breaking cycles stably."""
    for entity in entities.values():
        if entity.get("type") not in _NODE_TYPES:
            raise ValueError("Invalid synchronized node type")
        if not isinstance(entity.get(ANCESTORS, []), list) or any(
            not isinstance(value, str) for value in entity.get(ANCESTORS, [])
        ):
            raise ValueError("Invalid synchronized ancestor chain")
        _position(entity.get(POSITION))
    nodes = {
        node_id: {
            **deepcopy({key: value for key, value in entity.items() if key not in _RESERVED}),
            "id": node_id,
            "children": [],
        }
        for node_id, entity in entities.items()
    }
    parents: dict[str, str] = {}
    for node_id, entity in entities.items():
        preferred = str(entity.get(PARENT) or "")
        candidates = [preferred, *entity.get(ANCESTORS, [])]
        parents[node_id] = next(
            (candidate for candidate in candidates if candidate in nodes and candidate != node_id
             and nodes[candidate]["type"] in _CONTAINERS),
            "",
        )
    visited: set[str] = set()
    for start in sorted(nodes):
        chain: list[str] = []
        offsets: dict[str, int] = {}
        current = start
        while current and current not in visited:
            if current in offsets:
                # All replicas detach the same member of a concurrent cycle.
                parents[min(chain[offsets[current] :])] = ""
                break
            offsets[current] = len(chain)
            chain.append(current)
            current = parents[current]
        visited.update(chain)
    children: dict[str, list[str]] = defaultdict(list)
    for node_id, parent in parents.items():
        children[parent].append(node_id)
    for parent, siblings in children.items():
        siblings.sort(key=lambda key: (_position(entities[key].get(POSITION)), key))
        if parent:
            nodes[parent]["children"] = [nodes[key] for key in siblings]
    return [nodes[key] for key in children[""]]
