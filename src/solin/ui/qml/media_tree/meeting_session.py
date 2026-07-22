"""GUI-thread session that publishes complete meeting-tree snapshots."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from solin.controllers.media_tree_runtime import MediaTreeRuntime
from solin.core.media.operations import MediaOperationRecord, MediaOperationState
from solin.core.meetings.tree_types import iter_nodes
from solin.ui.qml.media_tree.meeting_presenter import (
    MeetingTreePresenter,
    PendingMeetingNodes,
)
from solin.ui.qml.media_tree.build_coordinator import (
    SnapshotBuildCoordinator,
    SnapshotBuildRequest,
    SnapshotBuildResult,
)
from solin.ui.qml.media_tree.model import MediaTreeSource
from solin.ui.qml.media_tree.probe_queue import ProbeRequestQueue
from solin.ui.qml.media_tree.topology import TopologySignature, topology_signature
from solin.styles.theme import current_theme_scheme


logger = logging.getLogger(__name__)


class MeetingTreeSession(QObject):
    """Reconcile meeting domain state and asynchronous presentation state."""

    snapshotPublished = Signal(str, int)

    def __init__(
        self,
        runtime: MediaTreeRuntime,
        thumbnail_path: Callable[[dict[str, Any], str, str], str | Path],
        *,
        badge_provider: Callable[[str], str] | None = None,
        thumbnail_ready: Callable[[str, QImage | None], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.source = MediaTreeSource("meeting:inactive", self)
        self.source.snapshotPublished.connect(self.snapshotPublished)
        self._runtime = runtime
        self._thumbnail_path = thumbnail_path
        self._thumbnail_ready = thumbnail_ready
        self._badge_provider = badge_provider
        self._presenter = MeetingTreePresenter(badge_provider)
        self._builds = SnapshotBuildCoordinator(
            runtime.presentation_workers,
            worker_name="meeting-tree-snapshot",
            parent=self,
        )
        self._builds.snapshotReady.connect(self._on_snapshot_ready)
        self._builds.buildFailed.connect(self._on_snapshot_failed)
        self._builds.idle.connect(self._resume_deferred_publish)
        self.source.retryRequested.connect(self._schedule_publish)
        self._tree_id = ""
        self._owner_id = ""
        self._nodes: Sequence[dict[str, Any]] = ()
        self._resolved_urls: Mapping[str, str] = {}
        self._revision = 0
        self._generation = 0
        self._structure_revision = 0
        self._topology_signature: TopologySignature | None = None
        self._known_sources: dict[str, str] = {}
        self._known_nodes: dict[str, dict[str, Any]] = {}
        self._source_revisions: dict[str, int] = {}
        self._operations: dict[str, MediaOperationRecord] = {}
        self._probe_requests = ProbeRequestQueue(
            self._request_queued_node,
            parent=self,
        )
        self._pending: dict[str, PendingMeetingNodes] = {}
        self._publish_timer = QTimer(self)
        self._publish_timer.setSingleShot(True)
        self._publish_timer.setInterval(0)
        self._publish_timer.timeout.connect(self._publish_now)
        self._publish_dirty = False
        runtime.registry.stateChanged.connect(self._on_state_changed)
        runtime.operations.operationChanged.connect(self._on_operation_changed)
        if thumbnail_ready is not None:
            runtime.probes.thumbnailReady.connect(self._on_thumbnail_ready)
        runtime.thumbnails.thumbnailStored.connect(self._on_thumbnail_stored)

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def tree_id(self) -> str:
        return self._owner_id

    @property
    def structure_revision(self) -> int:
        return self._structure_revision

    @property
    def generation(self) -> int:
        return self._generation

    def activate(
        self,
        tree_id: str,
        nodes: Sequence[dict[str, Any]],
        *,
        resolved_urls: Mapping[str, str] | None = None,
    ) -> None:
        if not tree_id:
            raise ValueError("MeetingTreeSession requires a stable tree ID")
        previous_owner = self._owner_id
        next_owner = f"meeting:{tree_id}"
        owner_changed = previous_owner != next_owner
        if previous_owner:
            self._runtime.operations.cancel_scope(previous_owner)
        self._generation += 1
        self._tree_id = tree_id
        self._owner_id = next_owner
        self._nodes = nodes
        self._resolved_urls = resolved_urls or {}
        self._probe_requests.clear()
        if owner_changed:
            self._structure_revision = 0
            self._topology_signature = None
            self._known_sources.clear()
            self._known_nodes.clear()
            self._source_revisions.clear()
        if self._pending:
            self._advance_structure_revision()
        self._operations.clear()
        self._pending.clear()
        if previous_owner and owner_changed:
            self._runtime.probes.clear_owner(previous_owner)
        self._refresh_structure_revision()
        self._sync_sources(request_all=True)
        self.source.begin_transition(next_owner)
        self._schedule_publish()

    def refresh(
        self,
        nodes: Sequence[dict[str, Any]],
        *,
        resolved_urls: Mapping[str, str] | None = None,
        probe_changed_sources: bool = True,
    ) -> None:
        if not self._tree_id:
            return
        self._nodes = nodes
        if resolved_urls is not None:
            self._resolved_urls = resolved_urls
        self._refresh_structure_revision()
        self._sync_sources(request_all=False, request_changed=probe_changed_sources)
        self._schedule_publish()

    def patch_source_progress(self, source: str, completed: int, total: int) -> None:
        ratio = -1.0 if total <= 0 else min(1.0, max(0.0, completed / total))
        for item_id, current_source in self._known_sources.items():
            if current_source == source:
                self._runtime.registry.patch(
                    self._owner_id,
                    item_id,
                    cloud_progress=ratio,
                )

    def add_pending(
        self,
        operation_id: str,
        nodes: Sequence[dict[str, Any]],
        *,
        target_list_id: str,
        insert_index: int,
    ) -> None:
        if not operation_id or not nodes:
            raise ValueError("Pending meeting nodes require an operation and nodes")
        self._pending[operation_id] = PendingMeetingNodes(
            operation_id,
            tuple(nodes),
            target_list_id or "root",
            insert_index,
        )
        self._advance_structure_revision()
        self._schedule_publish()

    def remove_pending(self, operation_id: str) -> None:
        pending = self._pending.pop(operation_id, None)
        if pending is None:
            return
        self._advance_structure_revision()
        for node in iter_nodes(list(pending.nodes)):
            self._operations.pop(str(node.get("id") or ""), None)
        self._schedule_publish()

    def remove_pending_node(self, node_id: str) -> bool:
        operation_id = next(
            (
                candidate_id
                for candidate_id, pending in self._pending.items()
                if any(
                    str(node.get("id") or "") == node_id
                    for node in iter_nodes(list(pending.nodes))
                )
            ),
            "",
        )
        if not operation_id:
            return False
        self._runtime.operations.cancel(operation_id)
        self._runtime.operations.discard(operation_id)
        self.remove_pending(operation_id)
        return True

    def request_source(self, source: str) -> None:
        matching = {
            item_id
            for item_id, current_source in self._known_sources.items()
            if current_source == source
        }
        self.request_nodes(matching)

    def request_nodes(self, node_ids: set[str] | None = None) -> None:
        requested = node_ids if node_ids is not None else set(self._known_sources)
        if not requested:
            return
        by_id = {
            str(node.get("id") or ""): node
            for node in iter_nodes(list(self._nodes))
            if node.get("type") == "media" and node.get("id")
        }
        for item_id in requested:
            if item_id in self._known_sources and item_id in by_id:
                self._probe_requests.enqueue(item_id, priority=True)

    def close(self) -> None:
        operation_ids = set(self._pending) | {
            operation.operation_id for operation in self._operations.values()
        }
        for operation_id in operation_ids:
            self._runtime.operations.cancel(operation_id)
            self._runtime.operations.discard(operation_id)
        if self._owner_id:
            self._runtime.probes.clear_owner(self._owner_id)
        self._publish_timer.stop()
        self._publish_dirty = False
        self._probe_requests.close()
        self._builds.close()
        self._pending.clear()
        self._operations.clear()
        self._tree_id = ""
        self._owner_id = ""
        self._nodes = ()

    def _schedule_publish(self) -> None:
        if not self._tree_id:
            return
        self._publish_dirty = True
        if not self._builds.active and not self._publish_timer.isActive():
            self._publish_timer.start()

    @Slot()
    def _resume_deferred_publish(self) -> None:
        if (
            self._publish_dirty
            and self._tree_id
            and not self._publish_timer.isActive()
        ):
            self._publish_timer.start()

    def _refresh_structure_revision(self) -> None:
        signature = topology_signature(self._nodes)
        if signature == self._topology_signature:
            return
        self._topology_signature = signature
        self._advance_structure_revision()

    def _advance_structure_revision(self) -> None:
        self._structure_revision += 1
        self.source.invalidate_pending_snapshot()

    def _publish_now(self) -> None:
        if not self._tree_id:
            return
        self._publish_timer.stop()
        if self._builds.active:
            self._publish_dirty = True
            return
        self._publish_dirty = False
        self._revision += 1
        revision = self._revision
        generation = self._generation
        owner_id = self._owner_id
        states = {
            node_id: self._runtime.registry.state(self._owner_id, node_id)
            for node_id in self._known_sources
        }
        tree_id = self._tree_id
        nodes = deepcopy(list(self._nodes))
        operations = dict(self._operations)
        source_revisions = dict(self._source_revisions)
        resolved_urls = dict(self._resolved_urls)
        pending_groups = tuple(
            PendingMeetingNodes(
                pending.operation_id,
                tuple(deepcopy(list(pending.nodes))),
                pending.target_list_id,
                pending.insert_index,
            )
            for pending in self._pending.values()
        )
        badge_provider = self._badge_provider
        badges = (
            {
                media_type: badge_provider(media_type)
                for media_type in ("audio", "image", "video")
            }
            if badge_provider is not None
            else {}
        )
        scheme = current_theme_scheme()

        def build():
            return self._presenter.build(
                tree_id,
                nodes,
                revision=revision,
                runtime_states=states,
                operations=operations,
                source_revisions=source_revisions,
                resolved_urls=resolved_urls,
                pending_groups=pending_groups,
                theme_scheme=scheme,
                media_badges=badges,
            )

        self._builds.request(
            SnapshotBuildRequest(
                generation,
                revision,
                owner_id,
                build,
                structure_revision=self._structure_revision,
            )
        )

    @Slot(object)
    def _on_snapshot_ready(self, value: object) -> None:
        if not isinstance(value, SnapshotBuildResult) or value.snapshot is None:
            return
        request = value.request
        if (
            request.generation != self._generation
            or request.tree_id != self._owner_id
            or request.structure_revision != self._structure_revision
        ):
            return
        if self.source.transitioning or value.snapshot.tree_id != self.source.treeId:
            self.source.activate_snapshot(value.snapshot)
        else:
            self.source.publish_snapshot(value.snapshot)

    @Slot(object)
    def _on_snapshot_failed(self, value: object) -> None:
        if not isinstance(value, SnapshotBuildResult):
            return
        request = value.request
        if (
            request.generation != self._generation
            or request.tree_id != self._owner_id
            or request.revision != self._revision
            or request.structure_revision != self._structure_revision
        ):
            return
        logger.error(
            "Meeting tree snapshot %s:%d failed: %s",
            request.tree_id,
            request.revision,
            value.error,
        )
        if self.source.transitioning:
            self.source.activate_error(request.tree_id, request.revision, value.error)

    def _sync_sources(self, *, request_all: bool, request_changed: bool = True) -> None:
        media_nodes = [
            node for node in iter_nodes(list(self._nodes))
            if node.get("type") == "media" and node.get("id")
        ]
        by_id = {str(node["id"]): node for node in media_nodes}
        self._known_nodes = by_id
        current = {
            item_id: str(
                self._resolved_urls.get(item_id)
                or node.get("resolved_url")
                or (node.get("media_ref") or {}).get("file_path")
                or ""
            )
            for item_id, node in by_id.items()
        }
        for item_id in set(self._known_sources) - set(current):
            self._probe_requests.discard(item_id)
            self._runtime.probes.remove(self._owner_id, item_id)
            self._known_sources.pop(item_id, None)
            self._source_revisions.pop(item_id, None)
            self._operations.pop(item_id, None)
        for item_id, source in current.items():
            previous = self._known_sources.get(item_id)
            changed = previous is not None and previous != source
            if changed:
                self._source_revisions[item_id] = self._source_revisions.get(item_id, 0) + 1
            else:
                self._source_revisions.setdefault(item_id, 0)
            self._known_sources[item_id] = source
            if request_all or (request_changed and (previous is None or changed)):
                self._probe_requests.enqueue(item_id)

    def _request_queued_node(self, item_id: str) -> None:
        source = self._known_sources.get(item_id)
        node = self._known_nodes.get(item_id)
        if source is not None and node is not None:
            self._request_node(node, item_id, source)

    def _request_node(self, node: dict[str, Any], item_id: str, source: str) -> None:
        self._runtime.probes.request(
            owner_id=self._owner_id,
            node_id=item_id,
            source=source,
            thumbnail_path=self._thumbnail_path(node, item_id, source),
            thumbnail_source=f"image://playlistthumbs/{item_id}",
        )

    @Slot(str, str)
    def _on_state_changed(self, owner_id: str, _node_id: str) -> None:
        if owner_id == self._owner_id:
            self._schedule_publish()

    @Slot(object)
    def _on_operation_changed(self, value: object) -> None:
        if not isinstance(value, MediaOperationRecord) or value.scope_id != self._owner_id:
            return
        if value.subject_id:
            if value.state in {
                MediaOperationState.READY,
                MediaOperationState.CANCELLED,
            }:
                self._operations.pop(value.subject_id, None)
            else:
                self._operations[value.subject_id] = value
            self._schedule_publish()
            return
        pending = self._pending.get(value.operation_id)
        if pending is None:
            return
        if value.state == MediaOperationState.CANCELLED:
            self.remove_pending(value.operation_id)
        else:
            for node in iter_nodes(list(pending.nodes)):
                if node.get("type") == "media" and node.get("id"):
                    self._operations[str(node["id"])] = value
        self._schedule_publish()

    @Slot(str, str, object)
    def _on_thumbnail_ready(
        self,
        owner_id: str,
        node_id: str,
        image: object,
    ) -> None:
        if owner_id != self._owner_id or self._thumbnail_ready is None:
            return
        self._thumbnail_ready(node_id, image if isinstance(image, QImage) else None)
        self._source_revisions[node_id] = self._source_revisions.get(node_id, 0) + 1
        self._schedule_publish()

    @Slot(str, str)
    def _on_thumbnail_stored(self, owner_id: str, node_id: str) -> None:
        if owner_id != self._owner_id:
            return
        if node_id in self._known_sources and node_id in self._known_nodes:
            self._probe_requests.enqueue(node_id, priority=True)


__all__ = ["MeetingTreeSession"]
