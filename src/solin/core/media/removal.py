"""Pure selection and filtering contracts for media removal."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


type MediaNode = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MediaRemovalResult:
    """Logical edit outcome; owned-file cleanup uses the existing async lifecycle."""

    removed_ids: tuple[str, ...] = ()
    blocked_ids: tuple[str, ...] = ()
    missing_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()
    pending_ids: tuple[str, ...] = ()


def select_media_ids(
    nodes: Sequence[MediaNode],
    scope_id: str = "root",
    *,
    eligible: Callable[[MediaNode], bool] = lambda _node: True,
) -> tuple[str, ...]:
    """Select committed media in visual order; invalid scopes select nothing."""
    roots = nodes
    if scope_id != "root":
        kind, separator, node_id = scope_id.partition(":")
        if not separator or kind not in {"section", "subsection"} or not node_id:
            return ()
        target = next(
            (node for node in _walk(nodes) if node.get("id") == node_id), None,
        )
        if target is None or target.get("type") != kind:
            return ()
        roots = target.get("children", ())
    return tuple(
        str(node["id"])
        for node in _walk(roots)
        if node.get("type") == "media" and node.get("id") and eligible(node)
    )


def media_counts_by_container(
    nodes: Sequence[MediaNode],
    *,
    eligible: Callable[[MediaNode], bool] = lambda _node: True,
) -> dict[str, int]:
    """Count eligible descendants while enforcing unique IDs and leaf nodes."""
    counts: dict[str, int] = {}
    seen_ids: set[str] = set()

    def visit(children: Sequence[MediaNode]) -> int:
        total = 0
        for node in children:
            node_id = str(node.get("id") or "")
            if not node_id:
                raise ValueError("Media-tree nodes need non-empty IDs")
            if node_id in seen_ids:
                raise ValueError(f"Duplicate node ID {node_id!r}")
            seen_ids.add(node_id)
            kind = node.get("type")
            descendants = node.get("children", ())
            if kind in {"media", "marker"} and descendants:
                raise ValueError(f"{kind!r} node {node_id!r} cannot have children")
            count = visit(descendants)
            if kind == "media":
                count += int(eligible(node))
            elif kind in {"section", "subsection"}:
                counts[node_id] = count
            total += count
        return total

    visit(nodes)
    return counts


def without_media(nodes: list[dict[str, Any]], removed_ids: set[str]) -> list[dict[str, Any]]:
    """Filter media in one pass, preserving surviving structure and order."""
    result = []
    for node in nodes:
        if node.get("type") == "media" and str(node.get("id") or "") in removed_ids:
            continue
        children = node.get("children")
        if children:
            node = {**node, "children": without_media(children, removed_ids)}
        result.append(node)
    return result


def _walk(nodes: Sequence[MediaNode]):
    for node in nodes:
        yield node
        yield from _walk(node.get("children", ()))
