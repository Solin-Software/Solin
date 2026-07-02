"""Destination sessions for inserting external media into a meeting tree."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal

from solin.core.meetings.catalog_placement import MeetingCatalogPlaylistRef


class MeetingDestinationSession(QObject):
    """Keep a live or headless meeting controller stable through one insertion."""

    completed = Signal(object)
    failed = Signal(str)
    closed = Signal(object)

    def __init__(self, controller, *, owned_controller: bool, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._owned_controller = owned_controller
        self._closed = False

    def placement_ref(self) -> MeetingCatalogPlaylistRef:
        return self.controller.placement_playlist_ref()

    def add_items(
        self,
        items: list[dict[str, Any]],
        *,
        list_id: str,
        insert_index: int,
    ) -> None:
        if self._closed:
            return
        result = self.controller.add_external_media_items(
            items,
            list_id=list_id,
            insert_index=insert_index,
        )
        if result.added_count or result.duplicate_count:
            self.completed.emit(result)
        else:
            self.failed.emit(self.tr("No media could be added."))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owned_controller:
            self.controller.cleanup()
            self.controller.deleteLater()
        self.closed.emit(self)
        self.deleteLater()


__all__ = ["MeetingDestinationSession"]
