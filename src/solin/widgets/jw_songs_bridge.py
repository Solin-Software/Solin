"""QML bridge for adding JW video songs to playlists and meetings."""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QModelIndex,
    QObject,
    Property,
    Qt,
    Signal,
    Slot,
)

from ..core.jw.media_api import song_publication_symbol
from ..core.jw.metadata import MEPS_FROM_LANG, parse_jworg_url
from ..core.jw.songs import JWSongsStore
from .jw_media_catalog_bridge import build_jw_media_placement_options

_BIG_INDEX = 2**31 - 1


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return ""
    total = int(seconds)
    minutes = total // 60
    secs = total % 60
    return f"{minutes}:{secs:02d}"


def _song_number(item: dict[str, Any]) -> int:
    try:
        return int(item.get("number") or 0)
    except (TypeError, ValueError):
        return 0


def _display_title(item: dict[str, Any]) -> str:
    number = _song_number(item)
    title = str(item.get("title") or "").strip()
    return f"{number}. {title}" if number else title


def _language_from_url(url: str, fallback: str) -> str:
    basename = os.path.basename(url.split("?", 1)[0])
    name = basename.rsplit(".", 1)[0]
    match = re.match(
        r"^(?:pub-)?[A-Za-z][A-Za-z0-9]{1,11}_([A-Z0-9]{1,8})_\d+",
        name,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper()
    return (fallback or "").upper()


class JWSongsModel(QAbstractListModel):
    NumberRole = Qt.ItemDataRole.UserRole + 1
    NumberTextRole = Qt.ItemDataRole.UserRole + 2
    TitleRole = Qt.ItemDataRole.UserRole + 3
    DurationTextRole = Qt.ItemDataRole.UserRole + 4
    DownloadUrlRole = Qt.ItemDataRole.UserRole + 5

    _ROLE_NAMES: dict[int, bytes] = {
        NumberRole: b"number",
        NumberTextRole: b"numberText",
        TitleRole: b"title",
        DurationTextRole: b"durationText",
        DownloadUrlRole: b"downloadUrl",
    }
    _ROLE_KEY: dict[int, str] = {
        NumberRole: "number",
        NumberTextRole: "_number_text",
        TitleRole: "title",
        DurationTextRole: "_duration_text",
        DownloadUrlRole: "url",
    }
    _SIGNATURE_KEYS = tuple(_ROLE_KEY.values())

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._items: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self._items)

    def roleNames(self) -> dict[int, bytes]:
        return self._ROLE_NAMES

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._items):
            return None
        key = self._ROLE_KEY.get(role)
        return self._items[index.row()].get(key) if key else None

    def set_items_if_changed(self, items: list[dict[str, Any]]) -> bool:
        enriched = [self._enrich(item) for item in items]
        if self._items_match(enriched):
            return False
        self.beginResetModel()
        self._items = enriched
        self.endResetModel()
        return True

    def clear(self) -> None:
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()

    def item_at(self, index: int) -> dict[str, Any] | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    @staticmethod
    def _enrich(item: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(item)
        number = _song_number(enriched)
        enriched["_number_text"] = str(number) if number else ""
        enriched["_duration_text"] = _format_duration(float(enriched.get("duration") or 0))
        return enriched

    def _items_match(self, items: list[dict[str, Any]]) -> bool:
        if len(items) != len(self._items):
            return False
        return all(
            self._item_signature(left) == self._item_signature(right)
            for left, right in zip(self._items, items, strict=False)
        )

    @classmethod
    def _item_signature(cls, item: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(item.get(key) for key in cls._SIGNATURE_KEYS)


class JWSongsBridge(QObject):
    """Bridge used by the add-song modal in playlist and meeting screens."""

    modelChanged = Signal()
    isLoadingChanged = Signal()
    errorMessageChanged = Signal()
    searchQueryChanged = Signal()
    resultCountChanged = Signal()
    statusTextChanged = Signal()
    showPlacementChanged = Signal()
    placementOptionsChanged = Signal()
    pendingItemChanged = Signal()

    itemAddedSuccessfully = Signal(str)
    modalShouldClose = Signal()
    jwMediaConfirmed = Signal(dict, str, int)

    def __init__(
        self,
        store: JWSongsStore,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._store.songs_ready.connect(self._on_songs_ready)
        self._store.songs_failed.connect(self._on_songs_failed)
        self._store.loading_changed.connect(self._on_loading_changed)

        self._model = JWSongsModel(self)
        self._all_items: list[dict[str, Any]] = []
        self._filtered_items: list[dict[str, Any]] = []
        self._search_query = ""
        self._is_loading = False
        self._error_message = ""
        self._status_text = ""
        self._active_key = ""
        self._api_code = "E"
        self._fallback_code = ""
        self._is_sign_language = False
        self._pl: dict[str, Any] | None = None
        self._pending_item: dict[str, Any] | None = None
        self._placement_options: list[dict[str, Any]] = []
        self._show_placement = False

    @Property(QObject, notify=modelChanged)
    def model(self) -> QObject:
        return self._model

    @Property(bool, notify=isLoadingChanged)
    def isLoading(self) -> bool:
        return self._is_loading

    @Property(str, notify=errorMessageChanged)
    def errorMessage(self) -> str:
        return self._error_message

    @Property(str, notify=searchQueryChanged)
    def searchQuery(self) -> str:
        return self._search_query

    @Property(int, notify=resultCountChanged)
    def resultCount(self) -> int:
        return len(self._filtered_items)

    @Property(str, notify=statusTextChanged)
    def statusText(self) -> str:
        return self._status_text

    @Property(bool, notify=showPlacementChanged)
    def showPlacement(self) -> bool:
        return self._show_placement

    @Property("QVariant", notify=placementOptionsChanged)
    def placementOptions(self) -> list[dict[str, Any]]:
        return self._placement_options

    @Property(str, notify=pendingItemChanged)
    def pendingItemTitle(self) -> str:
        return _display_title(self._pending_item) if self._pending_item else ""

    @Property(str, notify=pendingItemChanged)
    def pendingItemThumb(self) -> str:
        return ""

    @Slot(str)
    def setSearchQuery(self, query: str) -> None:
        if query == self._search_query:
            return
        self._search_query = query
        self.searchQueryChanged.emit()
        self._apply_filter()

    @Slot()
    def openModal(self) -> None:
        self.ensureLoaded()

    @Slot()
    def ensureLoaded(self) -> None:
        request = self._store.request_for(
            api_code=self._api_code,
            fallback_code=self._fallback_code,
            is_sign_language=self._is_sign_language,
            audio_mode=False,
        )
        self._active_key = request.key
        self._store.ensure_loaded(request)
        self._sync_snapshot()

    @Slot()
    def refresh(self) -> None:
        request = self._store.request_for(
            api_code=self._api_code,
            fallback_code=self._fallback_code,
            is_sign_language=self._is_sign_language,
            audio_mode=False,
        )
        self._active_key = request.key
        self._store.ensure_loaded(request, force=True)
        self._sync_snapshot()

    @Slot(int)
    def selectItem(self, index: int) -> None:
        item = self._model.item_at(index)
        if not item:
            return

        self._pending_item = dict(item)
        self.pendingItemChanged.emit()

        options = build_jw_media_placement_options(
            self._pl,
            translate_context="JWSongsBridge",
        )
        if not options:
            self._emit_confirmed("root", _BIG_INDEX)
            return

        self._placement_options = options
        self.placementOptionsChanged.emit()
        self._show_placement = True
        self.showPlacementChanged.emit()

    @Slot(str)
    def confirmPlacement(self, placement_id: str) -> None:
        if not self._pending_item:
            return

        if placement_id == "top":
            target_list_id = "root"
            target_index = 0
        elif placement_id == "bottom":
            target_list_id = "root"
            target_index = _BIG_INDEX
        elif placement_id.startswith("section:"):
            target_list_id = placement_id
            target_index = 0
        else:
            target_list_id = "root"
            target_index = _BIG_INDEX

        self._emit_confirmed(target_list_id, target_index)
        self.modalShouldClose.emit()

    @Slot()
    def cancelSelection(self) -> None:
        self._pending_item = None
        self.pendingItemChanged.emit()
        self._show_placement = False
        self.showPlacementChanged.emit()
        self._placement_options = []
        self.placementOptionsChanged.emit()

    @Slot()
    def reset(self) -> None:
        self.cancelSelection()
        self._search_query = ""
        self.searchQueryChanged.emit()
        self._apply_filter()

    def set_playlist_ref(self, pl: dict[str, Any] | None) -> None:
        self._pl = pl

    def set_language_context(
        self,
        *,
        api_code: str,
        fallback_code: str = "",
        is_sign_language: bool = False,
    ) -> None:
        api_code = api_code or "E"
        fallback_code = fallback_code or ""
        is_sign_language = bool(is_sign_language)
        changed = (
            api_code != self._api_code
            or fallback_code != self._fallback_code
            or is_sign_language != self._is_sign_language
        )
        if not changed:
            return
        self._api_code = api_code
        self._fallback_code = fallback_code
        self._is_sign_language = is_sign_language
        self._all_items.clear()
        self._filtered_items.clear()
        self._model.clear()
        self._active_key = ""
        self._error_message = ""
        self._is_loading = False
        self.errorMessageChanged.emit()
        self.isLoadingChanged.emit()
        self.resultCountChanged.emit()

    def cleanup(self) -> None:
        try:
            self._store.songs_ready.disconnect(self._on_songs_ready)
            self._store.songs_failed.disconnect(self._on_songs_failed)
            self._store.loading_changed.disconnect(self._on_loading_changed)
        except (RuntimeError, TypeError):
            pass

    def _sync_snapshot(self) -> None:
        snapshot = self._store.snapshot(self._active_key)
        self._all_items = list(snapshot.items)
        self._error_message = snapshot.error
        self._set_loading(snapshot.is_loading)
        self.errorMessageChanged.emit()
        self._apply_filter()

    @Slot(str, list, str, float, bool)
    def _on_songs_ready(
        self,
        key: str,
        items: list,
        _pub_name: str,
        _fetched_at: float,
        _from_cache: bool,
    ) -> None:
        if key != self._active_key:
            return
        self._all_items = list(items or [])
        self._error_message = ""
        self.errorMessageChanged.emit()
        self._set_loading(False)
        self._apply_filter()

    @Slot(str, str)
    def _on_songs_failed(self, key: str, error: str) -> None:
        if key != self._active_key:
            return
        self._all_items = []
        self._error_message = error
        self.errorMessageChanged.emit()
        self._set_loading(False)
        self._apply_filter()

    @Slot(str, bool)
    def _on_loading_changed(self, key: str, loading: bool) -> None:
        if key != self._active_key:
            return
        self._set_loading(loading)

    def _set_loading(self, value: bool) -> None:
        if self._is_loading == value:
            return
        self._is_loading = value
        self._status_text = (
            QCoreApplication.translate("JWSongsBridge", "Loading songs...")
            if value else ""
        )
        self.isLoadingChanged.emit()
        self.statusTextChanged.emit()

    def _apply_filter(self) -> None:
        query = self._search_query.strip().lower()
        if not query:
            filtered = sorted(self._all_items, key=_song_number)
        else:
            terms = [term for term in query.split() if term]
            filtered = [
                item for item in sorted(self._all_items, key=_song_number)
                if self._matches(item, terms, query)
            ]
        self._filtered_items = filtered
        self._model.set_items_if_changed(filtered)
        self.resultCountChanged.emit()

    @staticmethod
    def _matches(item: dict[str, Any], terms: list[str], query: str) -> bool:
        number = str(_song_number(item))
        title = str(item.get("title") or "").lower()
        if query.isdigit():
            return number.startswith(query)
        haystack = f"{number} {title}"
        return all(term in haystack for term in terms)

    def _emit_confirmed(self, target_list_id: str, target_index: int) -> None:
        if not self._pending_item:
            return

        url = str(self._pending_item.get("url") or "")
        parsed = parse_jworg_url(url) or {}
        number = _song_number(self._pending_item)
        pub = parsed.get("key_symbol") or song_publication_symbol(self._is_sign_language)
        language = _language_from_url(url, self._api_code)
        meps_language = parsed.get("meps_language") or MEPS_FROM_LANG.get(language, 0)
        duration = float(self._pending_item.get("duration") or 0)

        item_data: dict[str, Any] = {
            "title": _display_title(self._pending_item),
            "download_url": url,
            "media_type": "video",
            "duration_seconds": duration,
            "pub": pub,
            "track": parsed.get("track") or number,
            "issue": parsed.get("issue_tag"),
            "docid": parsed.get("doc_id"),
            "language": language,
            "meps_language": meps_language,
        }
        self.jwMediaConfirmed.emit(item_data, target_list_id, target_index)
        self.itemAddedSuccessfully.emit(
            QCoreApplication.translate("JWSongsBridge", "{title} added").format(
                title=item_data["title"]
            )
        )
        self.cancelSelection()


__all__ = ["JWSongsBridge", "JWSongsModel"]
