"""Recover publication subsection identities in persisted meeting trees."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

from .tree_types import Node, iter_nodes, publication_subsection_key, stable_node_id


def _publication_base(node: Node) -> tuple[str, bool] | None:
    if node.get("type") != "subsection" or not node.get("meeting_generated"):
        return None
    key = str(node.get("meeting_source_key") or "")
    parts = key.split(":")
    length = 4 if key.startswith("subsection:ref:") else 3
    if length == 3 and not key.startswith("subsection:cbs:"):
        return None
    if len(parts) not in (length, length + 1):
        return None
    return ":".join(parts[:length]), len(parts) == length


def _source_key(node: Node) -> str:
    return str(node.get("meeting_source_key") or "")


def _correct_identity(node: Node, key: str) -> None:
    node["meeting_source_key"] = key
    node["id"] = stable_node_id(key)


def migrate_publication_subsections(
    nodes: list[Node],
    canonical_nodes: list[Node],
    deleted_source_keys: set[str],
) -> bool:
    """Upgrade legacy identities and repair their collapsed publication groups.

    Canonical child ownership disambiguates legacy groups whose displayed title
    was replaced during a merge. Only children inside affected subsections are
    repartitioned: moves elsewhere in the tree and manual content are retained.
    Without a canonical baseline, the available publication titles supply the
    identity; missing historical publication metadata cannot be reconstructed.
    """
    has_legacy_nodes = any(
        identity is not None and identity[1]
        for tree in (nodes, canonical_nodes)
        for node in iter_nodes(tree)
        for identity in (_publication_base(node),)
    )
    has_legacy_deletions = any(
        (key.startswith("subsection:ref:") and len(key.split(":")) == 4)
        or (key.startswith("subsection:cbs:") and len(key.split(":")) == 3)
        for key in deleted_source_keys
    )
    if not has_legacy_nodes and not has_legacy_deletions:
        return False
    baseline = deepcopy(canonical_nodes or nodes)
    families: dict[str, dict[str, Node]] = defaultdict(dict)
    owners: dict[str, dict[str, str]] = defaultdict(dict)
    for node in iter_nodes(baseline):
        identity = _publication_base(node)
        if identity is None:
            continue
        base, legacy = identity
        key = (
            publication_subsection_key(base, str(node.get("title") or ""))
            if legacy
            else _source_key(node)
        )
        _correct_identity(node, key)
        families[base][key] = node
        for child in node.get("children", []):
            if child.get("meeting_generated") and _source_key(child):
                owners[base][_source_key(child)] = key

    changed = False
    for node in iter_nodes(canonical_nodes):
        identity = _publication_base(node)
        if identity is not None and identity[1]:
            _correct_identity(
                node,
                publication_subsection_key(identity[0], str(node.get("title") or "")),
            )
            changed = True

    for base, publications in families.items():
        if base in deleted_source_keys:
            deleted_source_keys.remove(base)
            deleted_source_keys.update(publications)
            changed = True

    sibling_orders: dict[str, list[str]] = {}

    def index_siblings(level: list[Node]) -> None:
        order = [_source_key(node) for node in level if node.get("meeting_generated")]
        for node in level:
            if _publication_base(node) is not None:
                sibling_orders[_source_key(node)] = order
            index_siblings(node.get("children", []))

    index_siblings(baseline)
    current_parents: dict[str, str] = {}

    def index_current_parents(level: list[Node], parent_base: str = "") -> None:
        for node in level:
            if node.get("meeting_generated") and _source_key(node):
                current_parents[_source_key(node)] = parent_base
            identity = _publication_base(node)
            index_current_parents(node.get("children", []), identity[0] if identity else "")

    index_current_parents(nodes)
    emitted: dict[str, Node] = {}
    child_tokens: dict[str, set[tuple[bool, str]]] = defaultdict(set)
    # A portable conflict may contain both an upgraded node and its legacy
    # counterpart. Prefer the upgraded node's edits regardless of list order.
    for node in iter_nodes(nodes):
        identity = _publication_base(node)
        if identity is None or identity[1]:
            continue
        key = _source_key(node)
        if key in emitted:
            continue
        emitted[key] = node
        for child in node.get("children", []):
            generated = bool(child.get("meeting_generated"))
            child_tokens[key].add(
                (
                    generated,
                    _source_key(child) if generated else str(child.get("id") or ""),
                )
            )

    def append_children(target: Node, children: list[Node]) -> None:
        target_key = _source_key(target)
        for child in children:
            generated = bool(child.get("meeting_generated"))
            token = (generated, _source_key(child) if generated else str(child.get("id") or ""))
            # The old merger could copy the same canonical child into both
            # colliding subsections. Preserve the first persisted occurrence.
            if token[1] and token in child_tokens[target_key]:
                continue
            child_tokens[target_key].add(token)
            target.setdefault("children", []).append(child)

    def migrate_level(level: list[Node]) -> None:
        nonlocal changed
        output: list[Node] = []
        recovered: list[Node] = []
        for node in level:
            children = node.get("children", [])
            if isinstance(children, list):
                migrate_level(children)
            identity = _publication_base(node)
            if identity is None or not identity[1]:
                existing = emitted.get(_source_key(node)) if identity is not None else None
                if existing is not None and existing is not node:
                    append_children(existing, children)
                else:
                    output.append(node)
                continue
            changed = True
            base = identity[0]
            publications = families.get(base, {})
            child_owners = owners.get(base, {})
            title_key = publication_subsection_key(base, str(node.get("title") or ""))
            matched_key = title_key
            if publications and (node.get("user_title_override") or title_key not in publications):
                source_hash = node.get("meeting_source_hash")
                hash_matches = [
                    key
                    for key, publication in publications.items()
                    if source_hash and publication.get("meeting_source_hash") == source_hash
                ]
                counts: dict[str, int] = defaultdict(int)
                for child in children:
                    owner = child_owners.get(_source_key(child))
                    if owner:
                        counts[owner] += 1
                matched_key = (
                    hash_matches[0]
                    if len(hash_matches) == 1
                    else max(counts, key=counts.__getitem__)
                    if counts
                    else next(iter(publications))
                )

            partitions: dict[str, list[Node]] = {}
            assignments = [
                child_owners.get(_source_key(child)) if child.get("meeting_generated") else None
                for child in children
            ]
            retain_empty_match = matched_key not in assignments and (
                bool(node.get("user_title_override"))
                or any(
                    owner == matched_key
                    and child_key in current_parents
                    and current_parents[child_key] != base
                    for child_key, owner in child_owners.items()
                )
            )
            next_owners = [matched_key] * len(assignments)
            next_owner = matched_key
            for index in range(len(assignments) - 1, -1, -1):
                next_owners[index] = next_owner
                assigned_owner = assignments[index]
                if assigned_owner is not None:
                    next_owner = assigned_owner
            previous_owner: str | None = None
            for index, child in enumerate(children):
                owner = assignments[index]
                if owner is not None:
                    previous_owner = owner
                elif retain_empty_match and not child.get("meeting_generated"):
                    owner = matched_key
                else:
                    owner = previous_owner or next_owners[index]
                partitions.setdefault(owner, []).append(child)
            if not partitions or retain_empty_match:
                # A renamed container remains meaningful even after all its
                # generated children have been moved elsewhere.
                partitions.setdefault(matched_key, [])

            for partition_index, (key, partition) in enumerate(partitions.items()):
                if key in emitted:
                    append_children(emitted[key], partition)
                    continue
                if key == matched_key:
                    target = deepcopy(node)
                else:
                    target = deepcopy(publications[key])
                    target["collapsed"] = bool(
                        node.get("collapsed", target.get("collapsed", False))
                    )
                _correct_identity(target, key)
                target["children"] = []
                append_children(target, partition)
                emitted[key] = target
                output.append(target)
                if partition_index:
                    recovered.append(target)
        # The old merger collapsed groups across intervening siblings. Keep
        # the first surviving group's position, then anchor reconstructed
        # siblings to their nearest available canonical neighbor.
        for target in recovered:
            key = _source_key(target)
            order = sibling_orders.get(key, [])
            if key not in order:
                continue
            output.remove(target)
            position = order.index(key)
            indices = {_source_key(node): index for index, node in enumerate(output)}
            previous = next(
                (candidate for candidate in reversed(order[:position]) if candidate in indices),
                None,
            )
            following = next(
                (candidate for candidate in order[position + 1 :] if candidate in indices),
                None,
            )
            if previous is not None:
                output.insert(indices[previous] + 1, target)
            elif following is not None:
                output.insert(indices[following], target)
            else:
                output.append(target)
        level[:] = output

    migrate_level(nodes)
    return changed
