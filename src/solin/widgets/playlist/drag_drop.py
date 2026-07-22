from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent

from ...core.foundation.constants import (
    DOCX_EXTS,
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
    PPTX_EXTS,
)
from ...core.media.formats import MEDIA_EXTS


__all__ = ("PlaylistDragDropMixin",)


class PlaylistDragDropMixin:
    def _has_valid_urls(self, mime) -> bool:
        if not mime or not mime.hasUrls():
            return False

        accepted = MEDIA_EXTS | PDF_EXTS | JWPUB_EXTS | PLAYLIST_EXTS
        if self._document_conversion_service.office_conversion_available():
            accepted = accepted | PPTX_EXTS | DOCX_EXTS
        return any(
            Path(u.toLocalFile()).suffix.lower() in accepted
            for u in mime.urls()
        )

    def map_delegate_idx_to_item_idx(self, delegate_idx: int) -> int:
        return -1

    def get_drop_context(self, delegate_idx: int) -> tuple[int, str]:
        return -1, ""

    def get_drop_context_for_list(self, list_id: str, insert_idx: int) -> tuple[int, str]:
        item_idx = self._tree_session.flat_insert_index(list_id or "root", insert_idx)
        section_id = ""
        if list_id.startswith("section:") or list_id.startswith("subsection:"):
            section_id = list_id.split(":", 1)[1]
        return item_idx, section_id

    def dragEnterEvent(self, event) -> None:
        if self._has_valid_urls(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def eventFilter(self, obj, event):
        if obj is getattr(self, "qml_widget", None) and event.type() == QEvent.Type.Leave:
            self._reset_qml_pointer_cursor()
        return super().eventFilter(obj, event)

    def dragMoveEvent(self, event) -> None:
        if self._has_valid_urls(event.mimeData()):
            event.acceptProposedAction()
            pos = event.position()
            local_pos = self.qml_widget.mapFrom(self, pos.toPoint())
            root_obj = self.qml_widget.rootObject()
            if root_obj:
                delegate_idx = root_obj.getIndexAt(local_pos.y())
                root_obj.setProperty("dropIndicatorIndex", delegate_idx)
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:
        root_obj = self.qml_widget.rootObject()
        if root_obj:
            root_obj.clearExternalDropPreview()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        if self._has_valid_urls(event.mimeData()):
            all_paths = [u.toLocalFile() for u in event.mimeData().urls()]
            media_paths = [p for p in all_paths if Path(p).suffix.lower() in MEDIA_EXTS]
            pdf_paths = [p for p in all_paths if Path(p).suffix.lower() in PDF_EXTS]
            jwpub_paths = [p for p in all_paths if Path(p).suffix.lower() in JWPUB_EXTS]
            office_conversion_available = (
                self._document_conversion_service.office_conversion_available()
            )
            pptx_paths = [
                p for p in all_paths
                if Path(p).suffix.lower() in PPTX_EXTS
                and office_conversion_available
            ]
            docx_paths = [
                p for p in all_paths
                if Path(p).suffix.lower() in DOCX_EXTS
                and office_conversion_available
            ]
            jwl_paths = [p for p in all_paths if Path(p).suffix.lower() in PLAYLIST_EXTS]

            target_idx = -1
            section_id = ""
            list_id = "root"
            insert_idx = -1
            pos = event.position()
            local_pos = self.qml_widget.mapFrom(self, pos.toPoint())
            root_obj = self.qml_widget.rootObject()
            if root_obj:
                delegate_idx = root_obj.getIndexAt(local_pos.y())
                list_id = root_obj.property("externalDropListId") or "root"
                insert_idx = root_obj.property("externalDropIndex")
                drop_tree_id = root_obj.property("externalDropTreeId") or ""
                drop_revision = root_obj.property("externalDropStructureRevision")
                target_is_current = (
                    drop_tree_id == self._tree_session.tree_id
                    and isinstance(drop_revision, int)
                    and drop_revision == self._tree_session.structure_revision
                )
                if not target_is_current:
                    root_obj.clearExternalDropPreview()
                    event.ignore()
                    return
                if isinstance(insert_idx, int) and insert_idx >= 0:
                    target_idx, section_id = self.get_drop_context_for_list(list_id, insert_idx)
                else:
                    target_idx, section_id = self.get_drop_context(delegate_idx)
                root_obj.clearExternalDropPreview()

            if media_paths:
                self._add_files(
                    media_paths,
                    target_idx,
                    section_id,
                    target_list_id=list_id,
                    target_list_index=insert_idx if isinstance(insert_idx, int) else -1,
                )
            if pdf_paths:
                self._import_pdfs(pdf_paths, target_idx, section_id)
            if jwpub_paths:
                self._import_jwpubs(jwpub_paths, target_idx, section_id)
            if pptx_paths:
                self._import_lo_files(pptx_paths, target_idx, section_id)
            if docx_paths:
                self._import_lo_files(docx_paths, target_idx, section_id)
            if jwl_paths:
                self._import_jwlplaylists_drop(jwl_paths, target_idx, section_id)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)
