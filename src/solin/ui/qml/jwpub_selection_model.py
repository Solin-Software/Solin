"""Virtualized publication rows and document-scoped media selection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal


class JwpubSelectionModel(QAbstractListModel):
    """Keep selection independent of filtering, expansion, and preview delivery."""

    selectionChanged = Signal()
    ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._documents: dict[str, Any] = {}
        self._selected: dict[str, set[str]] = {}
        self._expanded: set[str] = set()
        self._query = ""
        self._rows: list[tuple[str, str, str]] = []
        self._previews: Mapping[str, str] = {}

    def roleNames(self):  # noqa: N802 - Qt API
        return {self.ENTRY_ROLE: b"entry"}

    def rowCount(self, parent=QModelIndex()):  # noqa: N802, B008 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        if role != self.ENTRY_ROLE:
            return None
        kind, document_id, media_id = self._rows[index.row()]
        document = self._documents[document_id]
        selected = self._selected.get(document_id, set())
        if kind == "document":
            media = document.media
            return {
                "kind": kind,
                "key": document_id,
                "documentId": document_id,
                "title": document.title or self.tr("Publication media"),
                "subtitle": self._summary(media),
                "checkState": 0 if not selected else 2 if len(selected) == len(media) else 1,
                "expanded": document_id in self._expanded,
                "selected": bool(selected),
                "thumbnail": "",
                "hasPreview": False,
                "mediaType": "",
            }
        item = next(item for item in document.media if item.id == media_id)
        key = self.media_key(document_id, media_id)
        return {
            "kind": kind,
            "key": key,
            "documentId": document_id,
            "mediaId": media_id,
            "title": item.title,
            "subtitle": "",
            "checkState": 2 if media_id in selected else 0,
            "selected": media_id in selected,
            "expanded": False,
            "thumbnail": self._previews.get(key, ""),
            "hasPreview": bool(item.preview_member_path),
            "mediaType": item.media_type,
        }

    @staticmethod
    def media_key(document_id: str, media_id: str) -> str:
        return f"{document_id}/{media_id}"

    def set_catalog(self, catalog) -> None:
        self.beginResetModel()
        self._documents = {document.id: document for document in catalog.documents}
        self._selected.clear()
        self._expanded.clear()
        self._previews = {}
        self._rows = self._filtered_rows()
        self.endResetModel()
        self.selectionChanged.emit()

    def set_query(self, query: str) -> None:
        self._query = query.casefold().strip()
        self._publish()

    def toggle_document(self, document_id: str) -> None:
        document = self._documents.get(document_id)
        if document is None:
            return
        selected = self._selected.get(document_id, set())
        if len(selected) == len(document.media):
            self._selected.pop(document_id, None)
            self._expanded.discard(document_id)
        else:
            self._selected[document_id] = {item.id for item in document.media}
        self._publish()
        self.selectionChanged.emit()

    def toggle_media(self, document_id: str, media_id: str) -> None:
        document = self._documents.get(document_id)
        if document is None or not any(item.id == media_id for item in document.media):
            return
        selected = self._selected.setdefault(document_id, set())
        if media_id in selected:
            selected.remove(media_id)
        else:
            selected.add(media_id)
        if not selected:
            self._expanded.discard(document_id)
        self._publish()
        self.selectionChanged.emit()

    def toggle_expanded(self, document_id: str) -> None:
        if not self._selected.get(document_id):
            return
        if document_id in self._expanded:
            self._expanded.remove(document_id)
        else:
            self._expanded.add(document_id)
        self._publish()

    def select_all(self) -> None:
        self._selected = {
            document.id: {item.id for item in document.media}
            for document in self._documents.values()
        }
        self._publish()
        self.selectionChanged.emit()

    def clear_selection(self) -> None:
        self._selected.clear()
        self._expanded.clear()
        self._publish()
        self.selectionChanged.emit()

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.id
                for document in self._documents.values()
                for item in document.media
                if item.id in self._selected.get(document.id, set())
            )
        )

    def preview_media_id(self, key: str) -> str:
        for kind, document_id, media_id in self._rows:
            if kind == "media" and self.media_key(document_id, media_id) == key:
                return media_id
        return ""

    def set_previews(self, previews: Mapping[str, str]) -> None:
        self._previews = previews
        if self._rows:
            self.dataChanged.emit(self.index(0), self.index(len(self._rows) - 1), [self.ENTRY_ROLE])

    def _summary(self, media) -> str:
        counts = {
            kind: sum(item.media_type == kind for item in media)
            for kind in ("image", "video", "audio")
        }
        return self.tr("Images: {images} · Videos: {videos} · Audio: {audio}").format(
            **{
                "images": counts["image"],
                "videos": counts["video"],
                "audio": counts["audio"],
            }
        )

    def _filtered_rows(self) -> list[tuple[str, str, str]]:
        terms = self._query.split()
        rows = []
        for document in self._documents.values():
            if any(term not in document.title.casefold() for term in terms):
                continue
            rows.append(("document", document.id, ""))
            if document.id in self._expanded and self._selected.get(document.id):
                rows.extend(("media", document.id, item.id) for item in document.media)
        return rows

    def _publish(self) -> None:
        rows = self._filtered_rows()
        prefix = 0
        while prefix < min(len(rows), len(self._rows)) and rows[prefix] == self._rows[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < min(len(rows), len(self._rows)) - prefix
            and rows[-1 - suffix] == self._rows[-1 - suffix]
        ):
            suffix += 1
        removed = len(self._rows) - prefix - suffix
        if removed:
            self.beginRemoveRows(QModelIndex(), prefix, prefix + removed - 1)
            del self._rows[prefix : prefix + removed]
            self.endRemoveRows()
        inserted = len(rows) - prefix - suffix
        if inserted:
            self.beginInsertRows(QModelIndex(), prefix, prefix + inserted - 1)
            self._rows[prefix:prefix] = rows[prefix : prefix + inserted]
            self.endInsertRows()
        if self._rows:
            self.dataChanged.emit(self.index(0), self.index(len(self._rows) - 1), [self.ENTRY_ROLE])
