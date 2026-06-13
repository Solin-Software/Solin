"""
tree_types.py - Solin
=============================
Serializable tree helpers for meeting media.

The QML tree surface works with plain dictionaries, so this module keeps the
runtime contract explicit without forcing the UI layer to know about dataclass
instances.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from typing import Any, cast


Node = dict[str, Any]


def new_node_id() -> str:
    return str(uuid.uuid4())


def stable_node_id(source_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"solin:meeting-tree:{source_key}"))


def clean_dict(value: Any) -> Any:
    """Return a JSON-friendly deep copy."""
    if is_dataclass(value):
        return clean_dict(asdict(cast(Any, value)))
    if isinstance(value, dict):
        return {str(k): clean_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_dict(v) for v in value]
    return value


def source_hash(payload: Any) -> str:
    clean = clean_dict(payload)
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def clone_nodes(nodes: list[Node]) -> list[Node]:
    return deepcopy(nodes)


def count_media(nodes: list[Node]) -> int:
    total = 0
    for node in nodes:
        if node.get("type") == "media":
            total += 1
        total += count_media(node.get("children", []))
    return total


def iter_nodes(nodes: list[Node]):
    for node in nodes:
        yield node
        yield from iter_nodes(node.get("children", []))
