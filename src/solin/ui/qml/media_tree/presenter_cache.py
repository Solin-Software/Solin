"""Structural sharing for complete immutable media-tree presentations."""

from __future__ import annotations

from collections.abc import Callable, Hashable

from solin.ui.qml.media_tree.snapshot import MediaTreeNodeSnapshot


class PresenterNodeCache:
    """Reuse immutable nodes whose complete presentation input is unchanged."""

    def __init__(self) -> None:
        self._tree_id = ""
        self._entries: dict[str, tuple[Hashable, MediaTreeNodeSnapshot]] = {}
        self._used_ids: set[str] = set()

    def begin(self, tree_id: str) -> None:
        if tree_id != self._tree_id:
            self._tree_id = tree_id
            self._entries.clear()
        self._used_ids.clear()

    def resolve(
        self,
        node_id: str,
        key: Hashable,
        factory: Callable[[], MediaTreeNodeSnapshot],
    ) -> MediaTreeNodeSnapshot:
        self._used_ids.add(node_id)
        cached = self._entries.get(node_id)
        if cached is not None and cached[0] == key:
            return cached[1]
        node = factory()
        self._entries[node_id] = (key, node)
        return node

    def finish(self) -> None:
        for node_id in self._entries.keys() - self._used_ids:
            del self._entries[node_id]


__all__ = ["PresenterNodeCache"]
