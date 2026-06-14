from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent

from ...core.foundation.constants import (
    DOCX_EXTS as _DOCX_EXTS,
    JWPUB_EXTS as _JWPUB_EXTS,
    PDF_EXTS as _PDF_EXTS,
    PPTX_EXTS as _PPTX_EXTS,
)
from ...core.media.formats import MEDIA_EXTS as _MEDIA_EXTS
from ...core.rendering.libreoffice import libreoffice_available


class _PlaylistDragDropMixin:
    def _has_valid_urls(self, mime) -> bool:
        if not mime or not mime.hasUrls():
            return False
        from ...core.foundation.constants import PLAYLIST_EXTS as _PLAYLIST_EXTS

        accepted = _MEDIA_EXTS | _PDF_EXTS | _JWPUB_EXTS | _PLAYLIST_EXTS
        if libreoffice_available():
            accepted = accepted | _PPTX_EXTS | _DOCX_EXTS
        return any(
            Path(u.toLocalFile()).suffix.lower() in accepted
            for u in mime.urls()
        )

    def map_delegate_idx_to_item_idx(self, delegate_idx: int) -> int:
        if delegate_idx < 0:
            return -1
        item_count = 0
        entries = self.model._entries
        for i in range(min(delegate_idx, len(entries))):
            if entries[i]["type"] == "item":
                item_count += 1
        return item_count

    def get_drop_context(self, delegate_idx: int) -> tuple[int, str]:
        entries = self.model._entries
        if not entries:
            return -1, ""
        if delegate_idx < 0:
            return -1, ""

        insert_idx = max(0, min(delegate_idx, len(entries)))
        item_idx = sum(1 for e in entries[:insert_idx] if e["type"] == "item")
        context = self.model._drop_context_for_slot(entries, insert_idx, "item")
        section_id = ""
        if context["target_type"] == "subsection" and context.get("subsection"):
            section_id = context["subsection"]["id"]
        elif context["target_type"] == "section" and context.get("section"):
            section_id = context["section"]["id"]
        return item_idx, section_id

    def get_drop_context_for_list(self, list_id: str, insert_idx: int) -> tuple[int, str]:
        item_idx = self.model.flat_insert_index_for_list(list_id or "root", insert_idx)
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
            from ...core.foundation.constants import PLAYLIST_EXTS as _PLAYLIST_EXTS

            all_paths = [u.toLocalFile() for u in event.mimeData().urls()]
            media_paths = [p for p in all_paths if Path(p).suffix.lower() in _MEDIA_EXTS]
            pdf_paths = [p for p in all_paths if Path(p).suffix.lower() in _PDF_EXTS]
            jwpub_paths = [p for p in all_paths if Path(p).suffix.lower() in _JWPUB_EXTS]
            pptx_paths = [
                p for p in all_paths
                if Path(p).suffix.lower() in _PPTX_EXTS and libreoffice_available()
            ]
            docx_paths = [
                p for p in all_paths
                if Path(p).suffix.lower() in _DOCX_EXTS and libreoffice_available()
            ]
            jwl_paths = [p for p in all_paths if Path(p).suffix.lower() in _PLAYLIST_EXTS]

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
