"""Stable, I/O-free topology fingerprints for media-tree edit commands."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


type TopologySignature = tuple[tuple[str, str, "TopologySignature"], ...]


def topology_signature(nodes: Sequence[Mapping[str, Any]]) -> TopologySignature:
    """Describe only identity, type, order, and parentage of a mutable tree."""

    return tuple(
        (
            str(node.get("id") or ""),
            str(node.get("type") or ""),
            topology_signature(node.get("children") or ()),
        )
        for node in nodes
    )


__all__ = ["TopologySignature", "topology_signature"]
