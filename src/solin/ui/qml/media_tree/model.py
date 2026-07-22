"""Versioned snapshot source for the single QML media-tree reconciler."""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QObject, Property, QThread, Signal, Slot

from solin.ui.qml.media_tree.snapshot import (
    MediaTreeNodeSnapshot,
    MediaTreeSnapshot,
)


class SnapshotPublishResult(StrEnum):
    PUBLISHED = "published"
    DEFERRED = "deferred"
    STALE = "stale"
    TREE_MISMATCH = "tree_mismatch"


class MediaTreeSource(QObject):
    """Expose the newest complete tree while QML owns visual reconciliation."""

    snapshotPublished = Signal(str, int)
    treeIdChanged = Signal()
    revisionChanged = Signal()
    mediaCountChanged = Signal()
    interactionActiveChanged = Signal()
    transitioningChanged = Signal()
    errorChanged = Signal()
    retryRequested = Signal()

    def __init__(self, tree_id: str, parent: QObject | None = None) -> None:
        if not tree_id.strip():
            raise ValueError("tree_id must not be empty")
        super().__init__(parent)
        self._tree_id = tree_id
        self._revision = -1
        self._snapshot: MediaTreeSnapshot | None = None
        self._interaction_depth = 0
        self._pending_snapshot: MediaTreeSnapshot | None = None
        self._media_count = 0
        self._transitioning = False
        self._target_tree_id = ""
        self._error = ""
        self._presented_nodes: dict[str, MediaTreeNodeSnapshot] = {}
        self._presentation_revisions: dict[str, int] = {}

    @Property(str, notify=treeIdChanged)
    def treeId(self) -> str:  # noqa: N802 - QML API
        return self._tree_id

    @Property(int, notify=revisionChanged)
    def revision(self) -> int:
        return self._revision

    @Property(int, notify=mediaCountChanged)
    def mediaCount(self) -> int:  # noqa: N802 - QML API
        return self._media_count

    @Property("QVariantList", notify=revisionChanged)
    def treeData(self) -> list[dict[str, object]]:  # noqa: N802 - QML API
        snapshot = self._snapshot
        if snapshot is None:
            return []
        return [self._project_node(node) for node in snapshot.roots]

    @Property(bool, notify=interactionActiveChanged)
    def interactionActive(self) -> bool:  # noqa: N802 - QML API
        return self._interaction_depth > 0

    @Property(bool, notify=transitioningChanged)
    def transitioning(self) -> bool:
        return self._transitioning

    @Property(str, notify=errorChanged)
    def error(self) -> str:
        return self._error

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
            self.publish_snapshot(pending)

    def invalidate_pending_snapshot(self) -> None:
        """Discard presentation work captured before a topology mutation."""

        self._assert_source_thread()
        self._pending_snapshot = None

    @Slot()
    def retry(self) -> None:
        """Request a fresh complete snapshot after a visible build failure."""

        self._assert_source_thread()
        if not self._error:
            return
        self.begin_transition(self._tree_id)
        self.retryRequested.emit()

    def activate_snapshot(
        self,
        snapshot: MediaTreeSnapshot,
    ) -> SnapshotPublishResult:
        """Switch logical trees without reconstructing the QML surface."""

        self._assert_source_thread()
        if snapshot.tree_id == self._tree_id and not self._transitioning:
            return self.publish_snapshot(snapshot)
        if self._transitioning and snapshot.tree_id != self._target_tree_id:
            return SnapshotPublishResult.TREE_MISMATCH
        was_interacting = self._interaction_depth > 0
        self._interaction_depth = 0
        self._pending_snapshot = None
        if was_interacting:
            self.interactionActiveChanged.emit()
        tree_changed = snapshot.tree_id != self._tree_id
        previous_count = self._media_count
        if tree_changed:
            self._snapshot = None
            self._presented_nodes = {}
            self._presentation_revisions = {}
        self._tree_id = snapshot.tree_id
        self._revision = snapshot.revision
        self._media_count = self._update_presentation_revisions(snapshot)
        self._snapshot = snapshot
        was_transitioning = self._transitioning
        self._transitioning = False
        self._target_tree_id = ""
        self._set_error("")
        if tree_changed:
            self.treeIdChanged.emit()
        if self._media_count != previous_count:
            self.mediaCountChanged.emit()
        if was_transitioning:
            self.transitioningChanged.emit()
        self.revisionChanged.emit()
        self.snapshotPublished.emit(self._tree_id, self._revision)
        return SnapshotPublishResult.PUBLISHED

    def activate_error(
        self,
        tree_id: str,
        revision: int,
        error: str,
    ) -> SnapshotPublishResult:
        """Commit a failed target atomically and expose a retryable UI state."""

        detail = error.strip() or "Media-tree snapshot construction failed"
        result = self.activate_snapshot(MediaTreeSnapshot.create(tree_id, revision))
        if result != SnapshotPublishResult.PUBLISHED:
            return result
        self._set_error(detail)
        return result

    def begin_transition(self, tree_id: str) -> None:
        """Freeze interaction until a target tree can be committed atomically."""

        self._assert_source_thread()
        if not tree_id.strip():
            raise ValueError("tree_id must not be empty")
        changed = not self._transitioning or tree_id != self._target_tree_id
        self._target_tree_id = tree_id
        self._transitioning = True
        self._pending_snapshot = None
        self._set_error("")
        if self._interaction_depth:
            self._interaction_depth = 0
            self.interactionActiveChanged.emit()
        if changed:
            self.transitioningChanged.emit()

    def publish_snapshot(
        self,
        snapshot: MediaTreeSnapshot,
    ) -> SnapshotPublishResult:
        """Publish a complete snapshot, rejecting stale or foreign state."""

        self._assert_source_thread()
        if self._transitioning or snapshot.tree_id != self._tree_id:
            return SnapshotPublishResult.TREE_MISMATCH
        newest_revision = max(
            self._revision,
            self._pending_snapshot.revision if self._pending_snapshot else -1,
        )
        if snapshot.revision <= newest_revision:
            return SnapshotPublishResult.STALE
        self._assert_stable_node_contract(snapshot)
        if self._interaction_depth:
            self._pending_snapshot = snapshot
            return SnapshotPublishResult.DEFERRED
        media_count = self._update_presentation_revisions(snapshot)
        self._snapshot = snapshot
        self._revision = snapshot.revision
        self._set_error("")
        if media_count != self._media_count:
            self._media_count = media_count
            self.mediaCountChanged.emit()
        self.revisionChanged.emit()
        self.snapshotPublished.emit(self._tree_id, self._revision)
        return SnapshotPublishResult.PUBLISHED

    def _set_error(self, error: str) -> None:
        if error == self._error:
            return
        self._error = error
        self.errorChanged.emit()

    def _assert_source_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Media-tree snapshots must be published on the Qt thread")

    def _assert_stable_node_contract(self, snapshot: MediaTreeSnapshot) -> None:
        if self._snapshot is None or self._snapshot.tree_id != self._tree_id:
            return
        previous = self._presented_nodes
        for node in _iter_snapshot_nodes(snapshot.roots):
            current = previous.get(node.node_id)
            if current is None:
                continue
            if current.node_type != node.node_type:
                raise ValueError(
                    f"Stable node ID {node.node_id!r} changed type from "
                    f"{current.node_type.value!r} to {node.node_type.value!r}"
                )
            if node.source_revision < current.source_revision:
                raise ValueError(f"Node {node.node_id!r} source revision regressed")

    def _update_presentation_revisions(self, snapshot: MediaTreeSnapshot) -> int:
        same_tree = (
            self._snapshot is not None
            and self._snapshot.tree_id == snapshot.tree_id
        )
        previous_nodes = self._presented_nodes if same_tree else {}
        previous_revisions = self._presentation_revisions if same_tree else {}
        next_nodes: dict[str, MediaTreeNodeSnapshot] = {}
        next_revisions: dict[str, int] = {}
        media_count = 0
        for node in _iter_snapshot_nodes(snapshot.roots):
            next_nodes[node.node_id] = node
            if node.node_type.value == "media":
                media_count += 1
            previous_revision = previous_revisions.get(node.node_id, -1)
            next_revisions[node.node_id] = (
                previous_revision
                if previous_nodes.get(node.node_id) is node
                else previous_revision + 1
            )
        self._presented_nodes = next_nodes
        self._presentation_revisions = next_revisions
        return media_count

    def _project_node(self, node: MediaTreeNodeSnapshot) -> dict[str, object]:
        projected = node.thawed_roles()
        projected.update(
            {
                "id": node.node_id,
                "type": node.node_type.value,
                "sourceRevision": node.source_revision,
                "presentationRevision": self._presentation_revisions[node.node_id],
                "children": [self._project_node(child) for child in node.children],
            }
        )
        return projected


def _iter_snapshot_nodes(roots: tuple[MediaTreeNodeSnapshot, ...]):
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


__all__ = ["MediaTreeSource", "SnapshotPublishResult"]
