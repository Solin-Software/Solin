"""Hierarchical Qt model with transactional snapshot reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    Property,
    QThread,
    Qt,
    Signal,
    Slot,
)

from solin.ui.qml.media_tree.roles import (
    IDENTITY_ROLE_NAMES,
    ROLE_BY_NAME,
    ROLE_DEFAULTS,
    ROLE_NAMES,
    MediaTreeRole,
)
from solin.ui.qml.media_tree.snapshot import MediaTreeNodeSnapshot, MediaTreeSnapshot


_ROOT_ID: Final = "\x00media-tree-root"
_INVALID_INDEX: Final = QModelIndex()


@dataclass(slots=True)
class _TreeNode:
    node_id: str
    node_type: str
    source_revision: int
    roles: dict[str, object]
    parent: "_TreeNode | None" = None
    children: list["_TreeNode"] = field(default_factory=list)
    row_index: int = 0

    @classmethod
    def from_snapshot(cls, snapshot: MediaTreeNodeSnapshot) -> "_TreeNode":
        return cls(
            node_id=snapshot.node_id,
            node_type=snapshot.node_type.value,
            source_revision=snapshot.source_revision,
            roles=snapshot.thawed_roles(),
        )

    def row(self) -> int:
        return self.row_index


@dataclass(frozen=True, slots=True)
class _Park:
    node_id: str


@dataclass(frozen=True, slots=True)
class _Remove:
    parent_id: str
    node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Insert:
    parent_id: str
    row: int
    nodes: tuple[MediaTreeNodeSnapshot, ...]


@dataclass(frozen=True, slots=True)
class _Move:
    node_id: str
    parent_id: str
    row: int


@dataclass(frozen=True, slots=True)
class _Layout:
    parent_id: str
    node_ids: tuple[str, ...]


type _StructuralOperation = _Park | _Remove | _Insert | _Move | _Layout


class ReconcileResult(StrEnum):
    APPLIED = "applied"
    DEFERRED = "deferred"
    STALE = "stale"
    TREE_MISMATCH = "tree_mismatch"


@dataclass(slots=True)
class _ShadowTree:
    children: dict[str, list[str]]
    parents: dict[str, str]

    @classmethod
    def from_model(cls, root: _TreeNode) -> "_ShadowTree":
        children: dict[str, list[str]] = {_ROOT_ID: []}
        parents: dict[str, str] = {}
        stack = [root]
        while stack:
            parent = stack.pop()
            parent_id = parent.node_id
            children[parent_id] = [child.node_id for child in parent.children]
            for child in parent.children:
                parents[child.node_id] = parent_id
            stack.extend(reversed(parent.children))
        return cls(children=children, parents=parents)

    def depth(self, node_id: str) -> int:
        depth = 0
        parent_id = self.parents[node_id]
        while parent_id != _ROOT_ID:
            depth += 1
            parent_id = self.parents[parent_id]
        return depth

    def move(self, node_id: str, parent_id: str, row: int) -> None:
        source_parent_id = self.parents[node_id]
        source_children = self.children[source_parent_id]
        source_children.remove(node_id)
        destination = self.children[parent_id]
        destination.insert(row, node_id)
        self.parents[node_id] = parent_id

    def insert(self, parent_id: str, row: int, nodes: tuple[MediaTreeNodeSnapshot, ...]) -> None:
        node_ids = [node.node_id for node in nodes]
        self.children[parent_id][row:row] = node_ids
        for node_id in node_ids:
            self.parents[node_id] = parent_id
            self.children[node_id] = []

    def remove(self, parent_id: str, node_ids: tuple[str, ...]) -> None:
        first = self.children[parent_id].index(node_ids[0])
        del self.children[parent_id][first:first + len(node_ids)]
        for node_id in node_ids:
            self._drop_subtree(node_id)

    def _drop_subtree(self, node_id: str) -> None:
        for child_id in tuple(self.children[node_id]):
            self._drop_subtree(child_id)
        del self.children[node_id]
        del self.parents[node_id]


class MediaTreeModel(QAbstractItemModel):
    """The single indexed representation of a playlist or meeting tree."""

    snapshotApplied = Signal(str, int)
    treeIdChanged = Signal()
    revisionChanged = Signal()
    mediaCountChanged = Signal()
    interactionActiveChanged = Signal()

    def __init__(self, tree_id: str, parent=None) -> None:
        if not tree_id.strip():
            raise ValueError("tree_id must not be empty")
        super().__init__(parent)
        self._tree_id = tree_id
        self._revision = -1
        self._root = _TreeNode(_ROOT_ID, "root", 0, {})
        self._nodes: dict[str, _TreeNode] = {}
        self._interaction_depth = 0
        self._pending_snapshot: MediaTreeSnapshot | None = None
        self._media_count = 0

    def roleNames(self) -> dict[int, bytes]:  # noqa: N802 - Qt API
        return dict(ROLE_NAMES)

    def columnCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        return 1

    def rowCount(self, parent: QModelIndex = _INVALID_INDEX) -> int:  # noqa: N802
        if parent.isValid() and parent.column() != 0:
            return 0
        node = self._node_for_index(parent)
        return len(node.children)

    def index(
        self,
        row: int,
        column: int,
        parent: QModelIndex = _INVALID_INDEX,
    ) -> QModelIndex:
        if column != 0 or row < 0:
            return QModelIndex()
        parent_node = self._node_for_index(parent)
        if row >= len(parent_node.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_node.children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node = self._node_for_index(index)
        parent_node = node.parent
        if parent_node is None or parent_node is self._root:
            return QModelIndex()
        return self.createIndex(parent_node.row(), 0, parent_node)

    def data(
        self,
        index: QModelIndex,
        role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> object:
        if not index.isValid():
            return None
        node = self._node_for_index(index)
        if role == int(Qt.ItemDataRole.DisplayRole):
            return node.roles.get("title") or node.roles.get("text") or ""
        if role == int(MediaTreeRole.NODE_ID):
            return node.node_id
        if role == int(MediaTreeRole.NODE_TYPE):
            return node.node_type
        if role == int(MediaTreeRole.SOURCE_REVISION):
            return node.source_revision
        role_name = ROLE_NAMES.get(role)
        if role_name is None:
            return None
        name = role_name.decode("utf-8")
        return node.roles.get(name, ROLE_DEFAULTS[name])

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        node = self._node_for_index(index)
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if bool(node.roles.get("canDrag", ROLE_DEFAULTS["canDrag"])):
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        if bool(node.roles.get("canEdit", ROLE_DEFAULTS["canEdit"])):
            flags |= Qt.ItemFlag.ItemIsEditable
        if node.node_type in {"section", "subsection"}:
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        return flags

    @Property(str, notify=treeIdChanged)
    def treeId(self) -> str:  # noqa: N802 - QML API
        return self._tree_id

    @Property(int, notify=revisionChanged)
    def revision(self) -> int:
        return self._revision

    @Property(int, notify=mediaCountChanged)
    def mediaCount(self) -> int:  # noqa: N802 - QML API
        return self._media_count

    @Property(bool, notify=interactionActiveChanged)
    def interactionActive(self) -> bool:  # noqa: N802 - QML API
        return self._interaction_depth > 0

    @Slot()
    def beginInteraction(self) -> None:  # noqa: N802 - QML API
        was_active = self._interaction_depth > 0
        self._interaction_depth += 1
        if not was_active:
            self.interactionActiveChanged.emit()

    @Slot()
    def endInteraction(self) -> None:  # noqa: N802 - QML API
        if self._interaction_depth == 0:
            return
        self._interaction_depth -= 1
        if self._interaction_depth > 0:
            return
        self.interactionActiveChanged.emit()
        pending = self._pending_snapshot
        self._pending_snapshot = None
        if pending is not None:
            self.apply_snapshot(pending)

    def activate_snapshot(self, snapshot: MediaTreeSnapshot) -> ReconcileResult:
        """Explicitly activate another document while retaining model identity."""

        self._assert_model_thread()
        if snapshot.tree_id == self._tree_id:
            return self.apply_snapshot(snapshot)
        was_interacting = self._interaction_depth > 0
        self._interaction_depth = 0
        self._pending_snapshot = None
        if was_interacting:
            self.interactionActiveChanged.emit()
        if self._root.children:
            last = len(self._root.children) - 1
            self.beginRemoveRows(_INVALID_INDEX, 0, last)
            removed = tuple(self._root.children)
            self._root.children.clear()
            for node in removed:
                self._forget_subtree(node)
                node.parent = None
            self.endRemoveRows()
        previous_count = self._media_count
        self._media_count = 0
        if previous_count:
            self.mediaCountChanged.emit()
        self._tree_id = snapshot.tree_id
        self._revision = -1
        self.treeIdChanged.emit()
        self.revisionChanged.emit()
        return self.apply_snapshot(snapshot)

    def apply_snapshot(self, snapshot: MediaTreeSnapshot) -> ReconcileResult:
        """Apply the newest full snapshot with precise Qt model notifications."""

        self._assert_model_thread()
        if snapshot.tree_id != self._tree_id:
            return ReconcileResult.TREE_MISMATCH
        newest_revision = max(
            self._revision,
            self._pending_snapshot.revision if self._pending_snapshot else -1,
        )
        if snapshot.revision <= newest_revision:
            return ReconcileResult.STALE
        self._assert_stable_node_types(snapshot)

        if self._interaction_depth and not self._topology_matches(snapshot):
            self._pending_snapshot = snapshot
            self._apply_common_data(snapshot)
            return ReconcileResult.DEFERRED

        operations = self._plan_reconciliation(snapshot)
        for operation in operations:
            self._apply_structural_operation(operation)
        self._apply_common_data(snapshot)
        self._assert_matches(snapshot)
        self._revision = snapshot.revision
        media_count = sum(node.node_type == "media" for node in self._nodes.values())
        if media_count != self._media_count:
            self._media_count = media_count
            self.mediaCountChanged.emit()
        self.revisionChanged.emit()
        self.snapshotApplied.emit(self._tree_id, self._revision)
        return ReconcileResult.APPLIED

    @Slot(str, result=QModelIndex)
    def index_for_id(self, node_id: str) -> QModelIndex:
        node = self._nodes.get(node_id)
        if node is None or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.row(), 0, node)

    @Slot(str, result=bool)
    def contains(self, node_id: str) -> bool:
        return node_id in self._nodes

    @Slot(str, result=str)
    def parentId(self, node_id: str) -> str:  # noqa: N802 - QML API
        node = self._nodes.get(node_id)
        if node is None or node.parent is None or node.parent is self._root:
            return ""
        return node.parent.node_id

    @Slot(str, result=int)
    def childRow(self, node_id: str) -> int:  # noqa: N802 - QML API
        node = self._nodes.get(node_id)
        return node.row() if node is not None and node.parent is not None else -1

    def _node_for_index(self, index: QModelIndex) -> _TreeNode:
        if not index.isValid():
            return self._root
        node = index.internalPointer()
        if not isinstance(node, _TreeNode):
            raise RuntimeError("Foreign QModelIndex passed to MediaTreeModel")
        return node

    def _assert_model_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("MediaTreeModel snapshots must be applied on its Qt thread")

    def _desired_maps(
        self,
        snapshot: MediaTreeSnapshot,
    ) -> tuple[dict[str, MediaTreeNodeSnapshot], dict[str, str], dict[str, list[str]]]:
        nodes: dict[str, MediaTreeNodeSnapshot] = {}
        parents: dict[str, str] = {}
        children: dict[str, list[str]] = {_ROOT_ID: [node.node_id for node in snapshot.roots]}
        stack = [(node, _ROOT_ID) for node in reversed(snapshot.roots)]
        while stack:
            node, parent_id = stack.pop()
            nodes[node.node_id] = node
            parents[node.node_id] = parent_id
            children[node.node_id] = [child.node_id for child in node.children]
            stack.extend((child, node.node_id) for child in reversed(node.children))
        return nodes, parents, children

    def _topology_matches(self, snapshot: MediaTreeSnapshot) -> bool:
        _nodes, desired_parents, desired_children = self._desired_maps(snapshot)
        if set(desired_parents) != set(self._nodes):
            return False
        shadow = _ShadowTree.from_model(self._root)
        return shadow.parents == desired_parents and shadow.children == desired_children

    def _assert_stable_node_types(self, snapshot: MediaTreeSnapshot) -> None:
        desired_nodes, _parents, _children = self._desired_maps(snapshot)
        for node_id in set(desired_nodes) & set(self._nodes):
            current_type = self._nodes[node_id].node_type
            desired_type = desired_nodes[node_id].node_type.value
            if current_type != desired_type:
                raise ValueError(
                    f"Stable node ID {node_id!r} changed type from "
                    f"{current_type!r} to {desired_type!r}"
                )
            if desired_nodes[node_id].source_revision < self._nodes[node_id].source_revision:
                raise ValueError(f"Node {node_id!r} source revision regressed")

    def _plan_reconciliation(
        self,
        snapshot: MediaTreeSnapshot,
    ) -> tuple[_StructuralOperation, ...]:
        desired_nodes, desired_parents, desired_children = self._desired_maps(snapshot)
        desired_ids = set(desired_nodes)
        shadow = _ShadowTree.from_model(self._root)
        operations: list[_StructuralOperation] = []

        for parent_id, target_ids in desired_children.items():
            current_ids = shadow.children.get(parent_id)
            if current_ids is None or current_ids == target_ids:
                continue
            if len(current_ids) < 16 or set(current_ids) != set(target_ids):
                continue
            displaced = sum(
                current_id != target_id
                for current_id, target_id in zip(current_ids, target_ids, strict=True)
            )
            if displaced < 16:
                continue
            node_ids = tuple(target_ids)
            operations.append(_Layout(parent_id, node_ids))
            shadow.children[parent_id] = list(node_ids)

        mismatched = [
            node_id
            for node_id in desired_ids & set(shadow.parents)
            if shadow.parents[node_id] != desired_parents[node_id]
            and shadow.parents[node_id] != _ROOT_ID
        ]
        mismatched.sort(key=shadow.depth, reverse=True)
        for node_id in mismatched:
            if shadow.parents[node_id] == _ROOT_ID:
                continue
            operations.append(_Park(node_id))
            shadow.move(node_id, _ROOT_ID, len(shadow.children[_ROOT_ID]))

        def plan_removals(parent_id: str) -> None:
            child_ids = shadow.children[parent_id]
            ranges: list[tuple[int, int]] = []
            start = -1
            for row, child_id in enumerate(child_ids):
                if child_id not in desired_ids and start < 0:
                    start = row
                elif child_id in desired_ids and start >= 0:
                    ranges.append((start, row))
                    start = -1
            if start >= 0:
                ranges.append((start, len(child_ids)))
            for first, end in reversed(ranges):
                node_ids = tuple(shadow.children[parent_id][first:end])
                operations.append(_Remove(parent_id, node_ids))
                shadow.remove(parent_id, node_ids)
            for child_id in tuple(shadow.children[parent_id]):
                plan_removals(child_id)

        plan_removals(_ROOT_ID)

        def plan_children(parent_id: str) -> None:
            target_ids = desired_children[parent_id]
            target_row = 0
            while target_row < len(target_ids):
                node_id = target_ids[target_row]
                if node_id not in shadow.parents:
                    first = target_row
                    inserted: list[MediaTreeNodeSnapshot] = []
                    while target_row < len(target_ids):
                        candidate_id = target_ids[target_row]
                        if candidate_id in shadow.parents:
                            break
                        inserted.append(desired_nodes[candidate_id])
                        target_row += 1
                    nodes = tuple(inserted)
                    operations.append(_Insert(parent_id, first, nodes))
                    shadow.insert(parent_id, first, nodes)
                    continue
                current_parent = shadow.parents[node_id]
                current_row = shadow.children[current_parent].index(node_id)
                if current_parent != parent_id or current_row != target_row:
                    operations.append(_Move(node_id, parent_id, target_row))
                    shadow.move(node_id, parent_id, target_row)
                target_row += 1
            for child_id in target_ids:
                plan_children(child_id)

        plan_children(_ROOT_ID)
        if shadow.parents != desired_parents or shadow.children != desired_children:
            raise RuntimeError("Media-tree reconciliation planner produced an invalid topology")
        return tuple(operations)

    def _apply_structural_operation(self, operation: _StructuralOperation) -> None:
        if isinstance(operation, _Park):
            self._move_node(operation.node_id, self._root, len(self._root.children))
            return
        if isinstance(operation, _Remove):
            parent = self._parent_node(operation.parent_id)
            first = parent.children.index(self._nodes[operation.node_ids[0]])
            last = first + len(operation.node_ids) - 1
            self.beginRemoveRows(self._index_for_node(parent), first, last)
            removed = parent.children[first:last + 1]
            del parent.children[first:last + 1]
            self._reindex_children(parent, first)
            for node in removed:
                self._forget_subtree(node)
                node.parent = None
            self.endRemoveRows()
            return
        if isinstance(operation, _Insert):
            parent = self._parent_node(operation.parent_id)
            last = operation.row + len(operation.nodes) - 1
            self.beginInsertRows(self._index_for_node(parent), operation.row, last)
            inserted = [_TreeNode.from_snapshot(snapshot) for snapshot in operation.nodes]
            for node in inserted:
                node.parent = parent
                self._nodes[node.node_id] = node
            parent.children[operation.row:operation.row] = inserted
            self._reindex_children(parent, operation.row)
            self.endInsertRows()
            return
        if isinstance(operation, _Move):
            self._move_node(
                operation.node_id,
                self._parent_node(operation.parent_id),
                operation.row,
            )
            return
        if isinstance(operation, _Layout):
            self._reorder_children(operation.parent_id, operation.node_ids)
            return
        raise AssertionError(f"Unknown media-tree operation {operation!r}")

    def _move_node(self, node_id: str, destination: _TreeNode, row: int) -> None:
        node = self._nodes[node_id]
        source = node.parent
        if source is None:
            raise RuntimeError(f"Detached media-tree node {node_id!r}")
        source_row = source.children.index(node)
        destination_child = row + 1 if source is destination and source_row < row else row
        allowed = self.beginMoveRows(
            self._index_for_node(source),
            source_row,
            source_row,
            self._index_for_node(destination),
            destination_child,
        )
        if not allowed:
            raise RuntimeError(
                f"Qt rejected move of {node_id!r} to {destination.node_id!r}:{row}"
            )
        source.children.pop(source_row)
        self._reindex_children(source, source_row)
        destination.children.insert(row, node)
        node.parent = destination
        self._reindex_children(destination, row)
        self.endMoveRows()

    def _reorder_children(self, parent_id: str, node_ids: tuple[str, ...]) -> None:
        parent = self._parent_node(parent_id)
        old_indexes = [self.index_for_id(node.node_id) for node in parent.children]
        self.layoutAboutToBeChanged.emit()
        parent.children = [self._nodes[node_id] for node_id in node_ids]
        self._reindex_children(parent)
        new_indexes = [self.index_for_id(node.node_id) for node in parent.children]
        self.changePersistentIndexList(old_indexes, new_indexes)
        self.layoutChanged.emit()

    @staticmethod
    def _reindex_children(parent: _TreeNode, first: int = 0) -> None:
        for row in range(first, len(parent.children)):
            parent.children[row].row_index = row

    def _apply_common_data(self, snapshot: MediaTreeSnapshot) -> None:
        desired_nodes, _parents, _children = self._desired_maps(snapshot)
        changed_nodes: dict[tuple[str, tuple[int, ...]], list[_TreeNode]] = {}
        for node_id, desired in desired_nodes.items():
            node = self._nodes.get(node_id)
            if node is None:
                continue
            desired_roles = desired.thawed_roles()
            changed_roles: list[int] = []
            if node.node_type != desired.node_type.value:
                changed_roles.append(int(MediaTreeRole.NODE_TYPE))
            if node.source_revision != desired.source_revision:
                changed_roles.append(int(MediaTreeRole.SOURCE_REVISION))
            role_names = set(node.roles) | set(desired_roles)
            for name in role_names - IDENTITY_ROLE_NAMES:
                old_value = node.roles.get(name, ROLE_DEFAULTS[name])
                new_value = desired_roles.get(name, ROLE_DEFAULTS[name])
                if old_value != new_value:
                    changed_roles.append(int(ROLE_BY_NAME[name]))
            node.node_type = desired.node_type.value
            node.source_revision = desired.source_revision
            node.roles = desired_roles
            if changed_roles:
                roles = tuple(sorted(changed_roles))
                parent_id = node.parent.node_id if node.parent is not None else _ROOT_ID
                changed_nodes.setdefault((parent_id, roles), []).append(node)
        for (_parent_id, roles), nodes in changed_nodes.items():
            nodes.sort(key=_TreeNode.row)
            first = nodes[0]
            previous = first
            for node in nodes[1:]:
                if node.row() != previous.row() + 1:
                    self.dataChanged.emit(
                        self.index_for_id(first.node_id),
                        self.index_for_id(previous.node_id),
                        list(roles),
                    )
                    first = node
                previous = node
            self.dataChanged.emit(
                self.index_for_id(first.node_id),
                self.index_for_id(previous.node_id),
                list(roles),
            )

    def _assert_matches(self, snapshot: MediaTreeSnapshot) -> None:
        _nodes, desired_parents, desired_children = self._desired_maps(snapshot)
        shadow = _ShadowTree.from_model(self._root)
        if shadow.parents != desired_parents or shadow.children != desired_children:
            raise RuntimeError("Applied media-tree topology differs from the snapshot")

    def _parent_node(self, node_id: str) -> _TreeNode:
        return self._root if node_id == _ROOT_ID else self._nodes[node_id]

    def _index_for_node(self, node: _TreeNode) -> QModelIndex:
        if node is self._root or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.row(), 0, node)

    def _forget_subtree(self, node: _TreeNode) -> None:
        for child in node.children:
            self._forget_subtree(child)
        del self._nodes[node.node_id]
