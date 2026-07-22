"""Asynchronous thumbnail publication for declarative media trees."""

from __future__ import annotations

from pathlib import Path
import uuid

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from solin.controllers.media_operation_coordinator import MediaOperationCoordinator
from solin.core.foundation.resource_keys import file_resource_key
from solin.core.media.operations import (
    MediaOperationPresentation,
    MediaOperationProgress,
    MediaOperationSpec,
    MediaOperationState,
)
from solin.core.media.thumbnail_store import ThumbnailStore
from solin.ui.thumbnail_images import image_to_jpeg_bytes


class ThumbnailPersistenceController(QObject):
    """Publish thumbnails atomically without filesystem or codec work on Qt."""

    thumbnailStored = Signal(str, str)

    def __init__(
        self,
        coordinator: MediaOperationCoordinator,
        *,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator

    def save_image(
        self,
        *,
        owner_id: str,
        node_id: str,
        storage_id: str,
        store: ThumbnailStore,
        image: QImage,
        source_signature: str = "",
    ) -> bool:
        if not owner_id or not node_id or not storage_id or image.isNull():
            return False
        owned_image = image.copy()

        def run(report, cancellation):
            report(
                MediaOperationProgress(
                    state=MediaOperationState.PROCESSING,
                    stage="Saving thumbnail",
                    cancellable=False,
                )
            )
            if cancellation.is_set():
                return None
            data = image_to_jpeg_bytes(owned_image)
            if not data:
                raise OSError("Could not encode thumbnail")
            return store.save_bytes(
                storage_id,
                data,
                source_signature=source_signature,
            )

        return self._submit(owner_id, node_id, store, storage_id, run)

    def copy_file(
        self,
        *,
        owner_id: str,
        node_id: str,
        storage_id: str,
        store: ThumbnailStore,
        source: str | Path,
        source_signature: str = "",
    ) -> bool:
        if not owner_id or not node_id or not storage_id or not source:
            return False
        source_path = Path(source)

        def run(report, cancellation):
            report(
                MediaOperationProgress(
                    state=MediaOperationState.COPYING,
                    stage="Saving thumbnail",
                    cancellable=False,
                )
            )
            if cancellation.is_set():
                return None
            return store.copy_from(
                storage_id,
                source_path,
                source_signature=source_signature,
            )

        return self._submit(owner_id, node_id, store, storage_id, run)

    def _submit(
        self,
        owner_id: str,
        node_id: str,
        store: ThumbnailStore,
        storage_id: str,
        runner,
    ) -> bool:
        operation_id = f"thumbnail-store:{uuid.uuid4().hex}"

        def commit(value: object) -> None:
            if not isinstance(value, Path):
                raise TypeError("Thumbnail publication returned an invalid path")
            self.thumbnailStored.emit(owner_id, node_id)

        return self._coordinator.submit(
            MediaOperationSpec(
                operation_id=operation_id,
                scope_id=owner_id,
                operation_type="thumbnail_persistence",
                conflict_key=file_resource_key(store.path(storage_id)),
                presentation=MediaOperationPresentation.BACKGROUND,
                runner=runner,
                commit=commit,
                priority=-20,
                initial_stage="Saving thumbnail",
            )
        )


__all__ = ["ThumbnailPersistenceController"]
