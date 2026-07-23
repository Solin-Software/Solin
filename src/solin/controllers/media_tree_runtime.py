"""Application-owned runtime shared by declarative media-tree screens."""

from __future__ import annotations

from pathlib import Path
import uuid

from PySide6.QtCore import QObject

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.controllers.media_probe_controller import MediaProbeController
from solin.controllers.thumbnail_persistence_controller import (
    ThumbnailPersistenceController,
)
from solin.controllers.snapshot_write_coordinator import SnapshotWriteCoordinator
from solin.core.foundation.resource_lanes import ResourceLaneRegistry
from solin.core.foundation.resource_keys import ResourceClaim
from solin.core.foundation.thread_workers import ThreadedWorkerPool
from solin.ui.qml.media_tree.state import MediaStateRegistry


class MediaTreeRuntime(QObject):
    """Own bounded media work and presentation state at window scope.

    Views may come and go while a copy or probe is running. Keeping this runtime
    above individual pages prevents view reconstruction from cancelling useful
    work or losing its latest state.
    """

    def __init__(
        self,
        media_cache_dir: str | Path,
        *,
        max_workers: int = 2,
        resource_lanes: ResourceLaneRegistry | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.resource_lanes = resource_lanes or ResourceLaneRegistry()
        self.presentation_workers = ThreadedWorkerPool()
        self.registry = MediaStateRegistry(self)
        self.operations = MediaOperationCoordinator(
            max_workers=max_workers,
            resource_lanes=self.resource_lanes,
            parent=self,
        )
        self.probes = MediaProbeController(
            coordinator=self.operations,
            registry=self.registry,
            media_cache_dir=media_cache_dir,
            parent=self,
        )
        self.thumbnails = ThumbnailPersistenceController(
            self.operations,
            parent=self,
        )
        self.snapshots = SnapshotWriteCoordinator(
            resource_lanes=self.resource_lanes,
            parent=self,
        )
        self._closed = False

    def schedule_artifact_cleanup(
        self,
        paths: tuple[str | Path, ...],
        *,
        conflict_key: str | ResourceClaim,
    ) -> bool:
        """Delete explicit uncommitted artifacts on the shared background lane."""
        targets = tuple(Path(path) for path in paths if path)
        if self._closed or not targets:
            return False

        def cleanup() -> None:
            for target in targets:
                target.unlink(missing_ok=True)

        return bool(
            self.snapshots.request(
                f"artifact-cleanup:{uuid.uuid4().hex}",
                cleanup,
                conflict_key=conflict_key,
            )
        )

    def shutdown(self, wait_ms: int = 8_000) -> tuple[str, ...]:
        if self._closed:
            return ()
        self._closed = True
        unfinished_snapshots = self.snapshots.shutdown(wait_ms=min(wait_ms, 1_500))
        self.probes.shutdown()
        unfinished_operations = self.operations.shutdown(wait_ms)
        unfinished_presentations = self.presentation_workers.shutdown(
            timeout=min(wait_ms / 1_000, 1.5)
        )
        return (
            *unfinished_snapshots,
            *unfinished_operations,
            *unfinished_presentations,
        )


__all__ = ["MediaTreeRuntime"]
