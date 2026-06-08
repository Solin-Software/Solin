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


def _media_identity_signature(node: Node) -> tuple:
    ref = node.get("media_ref") or {}
    return tuple(str(ref.get(field, "")) for field in _MEDIA_IDENTITY_FIELDS)


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
        if _media_identity_signature(saved) != _media_identity_signature(node):
            return
        for field in _DURABLE_MEDIA_FIELDS:
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


__all__ = ["MeetingTreeMerger", "_media_identity_signature"]
