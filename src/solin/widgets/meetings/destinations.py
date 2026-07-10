"""Destination sessions for inserting external media into a meeting tree."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal

from solin.core.meetings.catalog_placement import MeetingCatalogPlaylistRef
from solin.core.meetings.tree_store import parse_meeting_tree_key


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

    @property
    def tree_key(self) -> str:
        return str(getattr(self.controller, "_tree_key", "") or "")

    def update_snapshot(self, snapshot) -> None:
        current = parse_meeting_tree_key(self.tree_key)
        incoming = parse_meeting_tree_key(snapshot.tree_key)
        if (
            self._closed
            or current is None
            or incoming is None
            or (
                current.pub_type,
                current.monday,
                current.language,
                current.is_sign_language,
            )
            != (
                incoming.pub_type,
                incoming.monday,
                incoming.language,
                incoming.is_sign_language,
            )
        ):
            return
        self.controller.load_saved_tree(snapshot)

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
