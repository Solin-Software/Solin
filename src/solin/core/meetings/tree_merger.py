"""
tree_merger.py - Solin
==============================
Merge canonical meeting trees with saved user state.

The meeting QML controller is intentionally thin around this module: tree
merging is domain logic and should remain usable without importing Qt widgets.
"""
from __future__ import annotations

import copy

from .tree_types import Node, clone_nodes, iter_nodes

_DURABLE_MEDIA_FIELDS = (
    "resolved_url",
    "thumbnail_url",
    "thumbnail_local_path",
    "thumbnail_cache_key",
    "base_duration_ticks",
    "start_trim_ticks",
    "end_trim_ticks",
    "image_framing",
)
_CANONICAL_RESOLUTION_FIELDS = (
    "resolved_url",
    "thumbnail_url",
    "base_duration_ticks",
)
_MEDIA_IDENTITY_FIELDS = (
    "multimedia_id",
    "mime_type",
    "file_path",
    "key_symbol",
    "track",
    "issue_tag",
    "meps_doc_id",
)


def _has_jw_media_identity(ref: dict) -> bool:
    return bool(str(ref.get("key_symbol") or "").strip() or ref.get("meps_doc_id"))


def _media_identity_value(ref: dict, field: str) -> str:
    if field == "file_path" and _has_jw_media_identity(ref):
        return ""
    return str(ref.get(field, ""))


def media_identity_signature(node: Node) -> tuple:
    ref = node.get("media_ref") or {}
    return tuple(_media_identity_value(ref, field) for field in _MEDIA_IDENTITY_FIELDS)


def merge_persisted_meeting_trees(
    prepared: list[Node],
    portable: list[Node],
    deleted_source_keys: set[str] | None = None,
) -> list[Node]:
    """Merge a prepared local aggregate with its portable manifest replica.

    Official nodes follow the regular canonical merge rules. Manual nodes are
    unioned by their stable IDs so a stale manifest cannot delete a local-only
    insertion, while moves and edits already present in the manifest remain
    authoritative. Fresh resolver URLs from the local aggregate are overlaid
    without replacing portable trims, framing, or materialized paths.
    """

    merged = MeetingTreeMerger(prepared, deleted_source_keys).merge(portable)
    _PersistedManualNodeMerger(merged).include(prepared)
    return merged


class _PersistedManualNodeMerger:
    _RESOLUTION_FIELDS = (
        "resolved_url",
        "thumbnail_url",
        "base_duration_ticks",
    )

    def __init__(self, target: list[Node]) -> None:
        self._target = target
        self._manual_by_id: dict[str, Node] = {}
        self._generated_by_key: dict[str, Node] = {}
        self._index_target(target)

    def include(self, source: list[Node]) -> None:
        self._include_level(self._target, source)

    def _index_target(self, nodes: list[Node]) -> None:
        for node in iter_nodes(nodes):
            node_id = str(node.get("id") or "")
            source_key = str(node.get("meeting_source_key") or "")
            if node.get("meeting_generated") and source_key:
                self._generated_by_key[source_key] = node
            elif node_id:
                self._manual_by_id[node_id] = node

    def _include_level(self, target_level: list[Node], source_level: list[Node]) -> None:
        for source_index, source in enumerate(source_level):
            target = self._matching_target(source)
            if target is None and not source.get("meeting_generated"):
                node_id = str(source.get("id") or "")
                if not node_id:
                    continue
                target = copy.deepcopy(source)
                if target.get("type") in ("section", "subsection"):
                    target["children"] = []
                self._insert_in_source_position(
                    target_level,
                    target,
                    source_level,
                    source_index,
                )
                self._index_target([target])
                if target.get("type") in ("section", "subsection"):
                    source_children = source.get("children", [])
                    if isinstance(source_children, list):
                        self._include_level(target["children"], source_children)
                continue
            if target is None:
                continue
            self._overlay_resolution(source, target)
            if source.get("type") in ("section", "subsection") and target.get(
                "type"
            ) in ("section", "subsection"):
                target_children = target.setdefault("children", [])
                source_children = source.get("children", [])
                if isinstance(target_children, list) and isinstance(source_children, list):
                    self._include_level(target_children, source_children)

    def _matching_target(self, source: Node) -> Node | None:
        if source.get("meeting_generated"):
            source_key = str(source.get("meeting_source_key") or "")
            return self._generated_by_key.get(source_key) if source_key else None
        node_id = str(source.get("id") or "")
        return self._manual_by_id.get(node_id) if node_id else None

    def _overlay_resolution(self, source: Node, target: Node) -> None:
        if (
            source.get("type") != "media"
            or target.get("type") != "media"
            or media_identity_signature(source) != media_identity_signature(target)
        ):
            return
        for field in self._RESOLUTION_FIELDS:
            value = source.get(field)
            if value not in (None, ""):
                target[field] = copy.deepcopy(value)

    def _insert_in_source_position(
        self,
        target_level: list[Node],
        node: Node,
        source_level: list[Node],
        source_index: int,
    ) -> None:
        for sibling in reversed(source_level[:source_index]):
            index = self._target_index(target_level, sibling)
            if index >= 0:
                target_level.insert(index + 1, node)
                return
        for sibling in source_level[source_index + 1 :]:
            index = self._target_index(target_level, sibling)
            if index >= 0:
                target_level.insert(index, node)
                return
        target_level.append(node)

    @staticmethod
    def _target_index(target_level: list[Node], source: Node) -> int:
        source_key = str(source.get("meeting_source_key") or "")
        node_id = str(source.get("id") or "")
        for index, candidate in enumerate(target_level):
            if source.get("meeting_generated") and source_key:
                if candidate.get("meeting_source_key") == source_key:
                    return index
            elif node_id and str(candidate.get("id") or "") == node_id:
                return index
        return -1


class MeetingTreeMerger:
    """Merge canonical meeting nodes with the user's saved tree."""

    def __init__(self, canonical: list[Node], deleted_source_keys: set[str] | None = None):
        self._canonical = clone_nodes(canonical)
        self._deleted_source_keys = deleted_source_keys or set()
        self._canonical_by_key = {
            node.get("meeting_source_key"): node
            for node in iter_nodes(self._canonical)
            if node.get("meeting_generated") and node.get("meeting_source_key")
            and node.get("meeting_source_key") not in self._deleted_source_keys
        }
        self._used: set[str] = set()

    def merge(self, saved: list[Node] | None) -> list[Node]:
        if not saved:
            return clone_nodes(self._canonical)
        return self._merge_level(saved, self._canonical, insert_missing=True)

    def _merge_level(
        self,
        saved_children: list[Node],
        canonical_children: list[Node],
        *,
        insert_missing: bool,
    ) -> list[Node]:
        output: list[Node] = []
        canonical_level_by_key: dict[str, Node] = {}
        for node in canonical_children:
            key = node.get("meeting_source_key")
            if node.get("meeting_generated") and isinstance(key, str) and key:
                canonical_level_by_key[key] = node

        for saved in saved_children:
            merged = self._merge_saved_node(saved, canonical_level_by_key)
            output.extend(merged)

        if insert_missing:
            for canonical in canonical_children:
                key = canonical.get("meeting_source_key")
                if not key or key in self._used or key in self._deleted_source_keys:
                    continue
                self._insert_missing_canonical(output, canonical, canonical_children)

        return output

    def _merge_saved_node(
        self,
        saved: Node,
        canonical_level_by_key: dict[str, Node],
    ) -> list[Node]:
        if not saved.get("meeting_generated"):
            node = copy.deepcopy(saved)
            if node.get("type") in ("section", "subsection"):
                node["children"] = self._merge_level(
                    node.get("children", []), [], insert_missing=False
                )
            return [node]

        key = saved.get("meeting_source_key")
        if key in self._deleted_source_keys:
            return self._salvage_children(saved)
        canonical = self._canonical_by_key.get(key or "")
        if not key or canonical is None:
            return self._salvage_children(saved)

        self._used.add(key)
        node = copy.deepcopy(canonical)
        node["id"] = saved.get("id") or node.get("id")
        self._copy_durable_media_state(saved, node)
        if saved.get("user_title_override"):
            if node.get("type") == "marker":
                node["text"] = saved.get("text", node.get("text", ""))
            else:
                node["title"] = saved.get("title", node.get("title", ""))
            node["user_title_override"] = True
        elif (
            node.get("type") == "media"
            and node.get("auto_title")
            and saved.get("title")
            and not saved.get("auto_title")
        ):
            node["title"] = saved.get("title", node.get("title", ""))
            node["auto_title"] = False
            node.setdefault("media_ref", {})["label"] = node["title"]
        if node.get("type") in ("section", "subsection"):
            node["collapsed"] = bool(saved.get("collapsed", node.get("collapsed", False)))
            node["color_hue"] = saved.get("color_hue", node.get("color_hue", 215))
            node["children"] = self._merge_level(
                saved.get("children", []),
                canonical.get("children", []),
                insert_missing=True,
            )
        return [node]

    def _copy_durable_media_state(self, saved: Node, node: Node) -> None:
        if node.get("type") != "media" or saved.get("type") != "media":
            return
        if media_identity_signature(saved) != media_identity_signature(node):
            return
        for field in _DURABLE_MEDIA_FIELDS:
            if field in _CANONICAL_RESOLUTION_FIELDS and node.get(field) not in (
                None,
                "",
            ):
                continue
            value = saved.get(field)
            if value not in (None, ""):
                node[field] = copy.deepcopy(value)

    def _salvage_children(self, stale_node: Node) -> list[Node]:
        children = stale_node.get("children", [])
        if not children:
            return []
        return self._merge_level(children, [], insert_missing=False)

    def _insert_missing_canonical(
        self,
        output: list[Node],
        canonical: Node,
        canonical_siblings: list[Node],
    ) -> None:
        node = copy.deepcopy(canonical)
        key = node.get("meeting_source_key")
        if key:
            self._used.add(key)

        previous_keys: list[str] = []
        next_keys: list[str] = []
        seen_self = False
        for sibling in canonical_siblings:
            skey = sibling.get("meeting_source_key")
            if sibling is canonical:
                seen_self = True
                continue
            if not skey:
                continue
            if seen_self:
                next_keys.append(skey)
            else:
                previous_keys.append(skey)

        for prev_key in reversed(previous_keys):
            idx = self._index_by_source_key(output, prev_key)
            if idx >= 0:
                output.insert(idx + 1, node)
                return
        for next_key in next_keys:
            idx = self._index_by_source_key(output, next_key)
            if idx >= 0:
                output.insert(idx, node)
                return
        output.append(node)

    def _index_by_source_key(self, nodes: list[Node], key: str) -> int:
        for idx, node in enumerate(nodes):
            if node.get("meeting_source_key") == key:
                return idx
        return -1


__all__ = [
    "MeetingTreeMerger",
    "media_identity_signature",
    "merge_persisted_meeting_trees",
]
