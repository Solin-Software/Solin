"""GUI-thread session that publishes complete playlist snapshots."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from solin.controllers.media_tree_runtime import MediaTreeRuntime
from solin.core.media.operations import MediaOperationRecord, MediaOperationState
from solin.core.media.thumbnail_identity import thumbnail_storage_id
from solin.core.playlists.tree_editing import (
    build_playlist_tree,
    can_drop_playlist_node,
    flat_media_insert_index,
    insert_playlist_media,
    move_playlist_node,
)
from solin.ui.qml.media_tree.model import MediaTreeModel
from solin.ui.qml.media_tree.playlist_presenter import (
    PendingPlaylistMedia,
    PlaylistTreePresenter,
)
from solin.ui.qml.media_tree.topology import TopologySignature, topology_signature


class PlaylistTreeSession(QObject):
    """Single source of truth between playlist domain state and QML."""

    snapshotPublished = Signal(str, int)

    def __init__(
        self,
        runtime: MediaTreeRuntime,
        thumbnail_path: Callable[[str], Path],
        *,
        thumbnail_ready: Callable[[str, QImage | None], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.model = MediaTreeModel("playlist:inactive", self)
        self._runtime = runtime
        self._thumbnail_path = thumbnail_path
        self._thumbnail_ready = thumbnail_ready
        self._presenter = PlaylistTreePresenter()
        self._playlist: dict[str, Any] | None = None
        self._owner_id = ""
        self._revision = 0
        self._generation = 0
        self._structure_revision = 0
        self._topology_signature: TopologySignature | None = None
        self._known_sources: dict[str, str] = {}
        self._source_revisions: dict[str, int] = {}
        self._pending: dict[str, PendingPlaylistMedia] = {}
        self._operations: dict[str, MediaOperationRecord] = {}
        self._publish_timer = QTimer(self)
        self._publish_timer.setSingleShot(True)
        self._publish_timer.setInterval(0)
        self._publish_timer.timeout.connect(self._publish_now)
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
        return self.model.treeId

    @property
    def playlist(self) -> dict[str, Any] | None:
        return self._playlist

    @property
    def structure_revision(self) -> int:
        return self._structure_revision

    @property
    def generation(self) -> int:
        return self._generation

    def activate(self, playlist: dict[str, Any]) -> None:
        playlist_id = str(playlist.get("id") or "")
        if not playlist_id:
            raise ValueError("PlaylistTreeSession requires a stable playlist ID")
        previous_owner = self._owner_id
        next_owner = f"playlist:{playlist_id}"
        owner_changed = previous_owner != next_owner
        if previous_owner:
            self._runtime.operations.cancel_scope(previous_owner)
        self._generation += 1
        self._playlist = playlist
        self._owner_id = next_owner
        if owner_changed:
            self._revision = 0
            self._structure_revision = 0
            self._topology_signature = None
            self._known_sources.clear()
            self._source_revisions.clear()
        if self._pending:
            self._structure_revision += 1
        self._pending.clear()
        self._operations.clear()
        if previous_owner and owner_changed:
            self._runtime.probes.clear_owner(previous_owner)
        self._refresh_structure_revision()
        self._sync_sources(request_all=True)
        self._publish_now(activate=True)

    def refresh(self, *, probe_changed_sources: bool = True) -> None:
        if self._playlist is None:
            return
        self._refresh_structure_revision()
        self._sync_sources(request_all=False, request_changed=probe_changed_sources)
        self._schedule_publish()

    def update_playlist(
        self,
        playlist: dict[str, Any],
        *,
        probe_changed_sources: bool = True,
    ) -> None:
        """Replace data for the active logical playlist without reactivation."""

        playlist_id = str(playlist.get("id") or "")
        if self._playlist is None or f"playlist:{playlist_id}" != self._owner_id:
            raise ValueError("Playlist update must target the active playlist")
        self._playlist = playlist
        self.refresh(probe_changed_sources=probe_changed_sources)

    def add_pending(
        self,
        item: dict[str, Any],
        *,
        target_list_id: str,
        insert_index: int,
    ) -> None:
        item_id = str(item.get("id") or "")
        if not item_id:
            raise ValueError("Pending playlist media requires a stable item ID")
        self._pending[item_id] = PendingPlaylistMedia(
            item=item,
            target_list_id=target_list_id or "root",
            insert_index=insert_index,
        )
        self._structure_revision += 1
        self._known_sources[item_id] = str(item.get("url") or "")
        self._source_revisions.setdefault(item_id, 0)
        self._schedule_publish()

    def remove_pending(self, item_id: str) -> None:
        if self._pending.pop(item_id, None) is None:
            return
        self._structure_revision += 1
        self._operations.pop(item_id, None)
        self._known_sources.pop(item_id, None)
        self._source_revisions.pop(item_id, None)
        self._schedule_publish()

    def pending_item(self, item_id: str) -> dict[str, Any] | None:
        pending = self._pending.get(item_id)
        return pending.item if pending is not None else None

    def pending_items(self) -> tuple[dict[str, Any], ...]:
        return tuple(pending.item for pending in self._pending.values())

    def operation_for(self, item_id: str) -> MediaOperationRecord | None:
        return self._operations.get(item_id)

    def can_drop(self, node_id: str, node_type: str, target_list_id: str) -> bool:
        return bool(
            self._playlist
            and can_drop_playlist_node(
                self._playlist,
                node_id,
                node_type,
                target_list_id,
            )
        )

    def move(
        self,
        node_id: str,
        target_list_id: str,
        insert_index: int,
        expected_tree_id: str,
        expected_structure_revision: int,
    ) -> bool:
        if (
            self._playlist is None
            or node_id in self._pending
            or expected_tree_id != self.model.treeId
            or expected_structure_revision != self._structure_revision
        ):
            return False
        moved = move_playlist_node(
            self._playlist,
            node_id,
            target_list_id,
            insert_index,
        )
        if moved:
            self.refresh(probe_changed_sources=False)
        return moved

    def insert_media(
        self,
        target_list_id: str,
        insert_index: int,
        items: list[dict[str, Any]],
    ) -> bool:
        if self._playlist is None:
            return False
        inserted = insert_playlist_media(
            self._playlist,
            target_list_id,
            insert_index,
            items,
        )
        if inserted:
            self.refresh()
        return inserted

    def flat_insert_index(self, target_list_id: str, insert_index: int) -> int:
        if self._playlist is None:
            return -1
        return flat_media_insert_index(self._playlist, target_list_id, insert_index)

    def request_source(self, source: str) -> None:
        if self._playlist is None:
            return
        for item_id, current_source in self._known_sources.items():
            if current_source == source and item_id not in self._pending:
                self._request_node(item_id, current_source)

    def request_nodes(self, node_ids: Iterable[str]) -> None:
        """Re-probe known persisted nodes without rescanning unrelated sources."""

        if self._playlist is None:
            return
        for item_id in dict.fromkeys(node_ids):
            source = self._known_sources.get(item_id)
            if source is not None and item_id not in self._pending:
                self._request_node(item_id, source)

    def patch_source_progress(self, source: str, completed: int, total: int) -> None:
        ratio = -1.0 if total <= 0 else min(1.0, max(0.0, completed / total))
        for item_id, current_source in self._known_sources.items():
            if current_source == source:
                self._runtime.registry.patch(
                    self._owner_id,
                    item_id,
                    cloud_progress=ratio,
                )

    def close(self) -> None:
        for operation_id in {
            operation.operation_id for operation in self._operations.values()
        }:
            self._runtime.operations.cancel(operation_id)
            self._runtime.operations.discard(operation_id)
        if self._owner_id:
            self._runtime.probes.clear_owner(self._owner_id)
        self._publish_timer.stop()
        self._pending.clear()
        self._operations.clear()
        self._playlist = None
        self._owner_id = ""

    def _schedule_publish(self) -> None:
        if self._playlist is not None and not self._publish_timer.isActive():
            self._publish_timer.start()

    def _refresh_structure_revision(self) -> None:
        if self._playlist is None:
            return
        signature = topology_signature(build_playlist_tree(self._playlist))
        if signature == self._topology_signature:
            return
        self._topology_signature = signature
        self._structure_revision += 1

    def _publish_now(self, *, activate: bool = False) -> None:
        if self._playlist is None:
            return
        self._revision += 1
        states = {
            node_id: self._runtime.registry.state(self._owner_id, node_id)
            for node_id in self._known_sources
        }
        snapshot = self._presenter.build(
            self._playlist,
            revision=self._revision,
            runtime_states=states,
            operations=self._operations,
            source_revisions=self._source_revisions,
            pending_media=tuple(self._pending.values()),
        )
        if activate or snapshot.tree_id != self.model.treeId:
            self.model.activate_snapshot(snapshot)
        else:
            self.model.apply_snapshot(snapshot)
        self.snapshotPublished.emit(snapshot.tree_id, snapshot.revision)

    def _sync_sources(
        self,
        *,
        request_all: bool,
        request_changed: bool = True,
    ) -> None:
        if self._playlist is None:
            return
        current: dict[str, str] = {
            str(item.get("id") or ""): str(item.get("url") or "")
            for item in self._playlist.get("items", [])
            if item.get("id")
        }
        for item_id in set(self._known_sources) - set(current) - set(self._pending):
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
                self._request_node(item_id, source)

    def _request_node(self, item_id: str, source: str) -> None:
        self._runtime.probes.request(
            owner_id=self._owner_id,
            node_id=item_id,
            source=source,
            thumbnail_path=self._thumbnail_path(
                thumbnail_storage_id(item_id, source)
            ),
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
        node_id = value.subject_id
        if not node_id:
            return
        if value.state == MediaOperationState.CANCELLED:
            self.remove_pending(node_id)
            return
        if value.state == MediaOperationState.READY:
            self._operations.pop(node_id, None)
            self._schedule_publish()
            return
        self._operations[node_id] = value
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
        source = self._known_sources.get(node_id)
        if source is not None:
            self._runtime.probes.request(
                owner_id=owner_id,
                node_id=node_id,
                source=source,
                thumbnail_path=self._thumbnail_path(
                    thumbnail_storage_id(node_id, source)
                ),
                thumbnail_source=f"image://playlistthumbs/{node_id}",
            )


__all__ = ["PlaylistTreeSession"]
