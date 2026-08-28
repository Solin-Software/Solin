from __future__ import annotations

import os
from zipfile import BadZipFile
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Slot, QT_TRANSLATE_NOOP
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import JWPUB_EXTS, PDF_EXTS, PLAYLIST_EXTS
from ..core.foundation.qt_threads import OwnedQThreadRegistry
from ..core.i18n.strings import (
    tr_document_page_title,
    tr_jw_playlist_title,
    tr_jw_playlist_unresolved,
)
from ..core.jw.language_context import (
    JWMediaLanguageContext,
    jw_media_language_context,
)
from ..core.playlists.jwl_files import read_jwlplaylist_document
from ..core.playlists.jwl_import import playlist_items_from_jwl_document_items
from ..core.playlists.items import (
    create_playlist_item,
    playlist_items_from_jwpub,
)
from ..core.playlists.names import PlaylistNameConflictError
from ..core.media.destinations import (
    MediaDestinationAsset,
    MediaDestinationOutcome,
    PlaylistDestinationTarget,
    PreparedMediaBatch,
)
from ..core.media.placement import END_OF_LIST_INDEX
from ..ui.media_insertion_feedback import tr_media_already_added_count

if TYPE_CHECKING:
    from ..core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from ..core.rendering.document_conversion import DocumentConversionService


_NOTIFICATION_CONTEXT = "PlaylistImportNotifications"
_FILES_ADDED_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "{playlist}: %n file(s) added",
)
_FILE_NOT_FOUND_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "File not found: {name}",
)
_UNSUPPORTED_FILE_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Unsupported file: {name}",
)
_UNSUPPORTED_FILE_TITLE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Unsupported file",
)
_INVALID_PLAYLIST_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Invalid or corrupted .jwlplaylist file:\n{name}",
)
_READ_PLAYLIST_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Could not read {name}.\n{error}",
)
_NO_MEDIA_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "No media found in {name}",
)
_PLAYLIST_EXISTS_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    'A playlist named "{name}" already exists.',
)
_PLAYLIST_CREATED_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    'Playlist "{name}"\ncreated successfully!',
)
_LOCATION_MISSING_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    'Could not add media to playlist "{name}" because the selected location '
    "no longer exists.",
)
_ALREADY_IN_PLAYLIST_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    'This media is already in\nplaylist "{name}"',
)
_OPENING_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Opening {name}...",
)
_PDF_CONVERSION_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Error converting PDF: {error}",
)
_PDF_ERROR_TITLE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Error opening PDF",
)
_PDF_NO_PAGES_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "PDF conversion finished without any pages.",
)
_JWPUB_OPEN_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Could not open .jwpub: {error}",
)
_JWPUB_ERROR_TITLE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "Error opening .jwpub",
)
_JWPUB_NO_MEDIA_SOURCE = QT_TRANSLATE_NOOP(
    "PlaylistImportNotifications",
    "The .jwpub file contained no usable media.",
)
def _tr(source: str, *, n: int = -1) -> str:
    return QCoreApplication.translate(_NOTIFICATION_CONTEXT, source, "", n)


def _tr_files_added(playlist_name: str, count: int) -> str:
    return _tr(
        _FILES_ADDED_SOURCE,
        n=max(0, int(count)),
    ).format(playlist=playlist_name)


@dataclass(frozen=True, slots=True)
class PlaylistImportContext:
    """Stable services used by playlist import workflows."""

    dialog_parent: Any
    document_conversion_service: DocumentConversionService
    profile_media_store: Any
    jwpub_import_thread_factory: JwpubImportThreadFactory
    language_manager: Any
    notifications: Any
    playlist_widget: Any
    thread_registry: OwnedQThreadRegistry


@dataclass(frozen=True, slots=True)
class PlaylistImportHandlers:
    """Presentation actions triggered by playlist import workflows."""

    switch_to_playlist: Callable[[], None]


class PlaylistImportController:
    """Handles adding projected, downloaded, and imported media to playlists."""

    def __init__(
        self,
        context: PlaylistImportContext,
        handlers: PlaylistImportHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers

    def prepare_destination_assets(
        self,
        assets: list[MediaDestinationAsset],
        completed: Callable[[PreparedMediaBatch], None],
    ) -> None:
        """Resolve ordered direct/import assets into canonical playlist items."""

        pending = deque(assets)
        prepared: list[dict[str, Any]] = []
        handled_sources: list[str] = []

        def finish_asset(
            asset: MediaDestinationAsset,
            items: list[dict[str, Any]],
        ) -> None:
            if items:
                prepared.extend(items)
                handled_sources.append(asset.source_id)
            prepare_next()

        def prepare_next() -> None:
            while pending:
                asset = pending.popleft()
                if asset.item is not None:
                    prepared.append(dict(asset.item))
                    handled_sources.append(asset.source_id)
                    continue

                path = asset.import_path
                if not path or not os.path.isfile(path):
                    self._notify_import_error(
                        _tr(_FILE_NOT_FOUND_SOURCE).format(name=asset.title)
                    )
                    continue

                ext = os.path.splitext(path)[1].lower()
                kind = asset.import_kind
                if kind == "jwlplaylist" or ext in PLAYLIST_EXTS:
                    items = [
                        dict(item)
                        for item in self.items_from_jwlplaylist_for_playlist(path)
                    ]
                    if items:
                        prepared.extend(items)
                        handled_sources.append(asset.source_id)
                    continue
                if kind == "pdf" or ext in PDF_EXTS:
                    cached_items = self._prepare_pdf_asset(asset, finish_asset)
                    if cached_items is None:
                        return
                    if cached_items:
                        prepared.extend(cached_items)
                        handled_sources.append(asset.source_id)
                    continue
                if kind == "jwpub" or ext in JWPUB_EXTS:
                    self._prepare_jwpub_asset(asset, finish_asset)
                    return

                self._notify_import_error(
                    _tr(_UNSUPPORTED_FILE_SOURCE).format(name=asset.title)
                )

            completed(
                PreparedMediaBatch(
                    tuple(prepared),
                    tuple(handled_sources),
                )
            )

        prepare_next()

    def items_from_jwlplaylist_for_playlist(self, jwl_path: str) -> list:
        context = self._context
        try:
            document = read_jwlplaylist_document(
                jwl_path,
                fallback_lang_code=self._media_language_context().fallback_code,
            )
        except BadZipFile:
            QMessageBox.warning(
                context.dialog_parent,
                _tr(_UNSUPPORTED_FILE_TITLE),
                _tr(_INVALID_PLAYLIST_SOURCE).format(
                    name=os.path.basename(jwl_path)
                ),
            )
            return []
        except (OSError, ValueError) as error:
            QMessageBox.warning(
                context.dialog_parent,
                _tr(_UNSUPPORTED_FILE_TITLE),
                _tr(_READ_PLAYLIST_ERROR_SOURCE).format(
                    name=os.path.basename(jwl_path),
                    error=error,
                ),
            )
            return []

        result = playlist_items_from_jwl_document_items(
            document.items,
            source_name=os.path.basename(jwl_path),
            save_embedded=lambda data, filename, identifier, default_suffix: (
                context.profile_media_store.save_embedded(
                    data,
                    filename,
                    identifier=identifier,
                    default_suffix=default_suffix,
                )
            ),
        )

        if result.skipped_titles:
            names = "\n".join(f"  • {name}" for name in result.skipped_titles)
            QMessageBox.warning(
                context.dialog_parent,
                tr_jw_playlist_title(),
                tr_jw_playlist_unresolved(names),
            )
        return result.items

    def add_items_to_playlist_target(
        self,
        target: PlaylistDestinationTarget,
        items: list[dict[str, Any]],
        source_name: str,
    ) -> MediaDestinationOutcome:
        context = self._context
        playlist_widget = context.playlist_widget
        if not items:
            context.notifications.warning(
                _tr(_NO_MEDIA_SOURCE).format(name=source_name)
            )
            return MediaDestinationOutcome(0)

        if target.create_new:
            try:
                playlist_id = playlist_widget.create_playlist_with_items(
                    target.playlist_name,
                    [items[0]],
                )
            except PlaylistNameConflictError as exc:
                context.notifications.warning(
                    _tr(_PLAYLIST_EXISTS_SOURCE).format(name=exc.name)
                )
                return MediaDestinationOutcome(0, destination_accepted=False)
            referenced_urls = [str(items[0].get("url") or "")]
            added = 1
            if len(items) > 1:
                result = playlist_widget.add_items_to_playlist(
                    playlist_id,
                    items[1:],
                    list_id="root",
                    insert_index=END_OF_LIST_INDEX,
                )
                added += len(result.added_items)
                referenced_urls.extend(
                    str(item.get("url") or "") for item in result.added_items
                )
            context.notifications.success(
                _tr(_PLAYLIST_CREATED_SOURCE).format(name=target.playlist_name)
            )
            return MediaDestinationOutcome(
                added,
                tuple(url for url in referenced_urls if url),
            )

        result = playlist_widget.add_items_to_playlist(
            target.playlist_id,
            items,
            list_id=target.list_id,
            insert_index=target.insert_index,
        )
        referenced_urls = [
            str(item.get("url") or "")
            for item in result.added_items
            if item.get("url")
        ]
        added = len(result.added_items)
        if added:
            context.notifications.success(
                _tr_files_added(target.playlist_name, added)
            )
            if result.duplicate_count:
                context.notifications.warning(
                    tr_media_already_added_count(result.duplicate_count)
                )
        elif not result.target_valid:
            context.notifications.error(
                _tr(_LOCATION_MISSING_SOURCE).format(name=target.playlist_name)
            )
            return MediaDestinationOutcome(0, destination_accepted=False)
        else:
            context.notifications.warning(
                _tr(_ALREADY_IN_PLAYLIST_SOURCE).format(name=target.playlist_name)
            )
        return MediaDestinationOutcome(added, tuple(referenced_urls))

    def _prepare_pdf_asset(
        self,
        asset: MediaDestinationAsset,
        completed: Callable[[MediaDestinationAsset, list[dict[str, Any]]], None],
    ) -> list[dict[str, Any]] | None:
        context = self._context
        pdf_path = asset.import_path
        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        pages = context.document_conversion_service.cached_pdf_pages(pdf_path)
        if pages:
            return self._items_from_pages(pages, pdf_stem)

        context.notifications.information(
            _tr(_OPENING_SOURCE).format(name=pdf_stem)
        )
        thread = context.document_conversion_service.create_pdf_thread(
            pdf_path,
            parent=context.dialog_parent,
        )
        context.thread_registry.track(thread)
        resolved = False

        def finish(items: list[dict[str, Any]]) -> None:
            nonlocal resolved
            if resolved:
                return
            resolved = True
            completed(asset, items)

        thread.pages_ready.connect(
            lambda pages_ready, stem: finish(
                self._items_from_pages(pages_ready, stem)
            )
        )

        @thread.conversion_failed.connect
        def on_conversion_failed(error: str) -> None:
            self._notify_import_error(
                _tr(_PDF_CONVERSION_ERROR_SOURCE).format(error=error),
                title=_tr(_PDF_ERROR_TITLE),
            )
            finish([])

        @thread.finished.connect
        def on_finished() -> None:
            if resolved:
                return
            self._notify_import_error(
                _tr(_PDF_NO_PAGES_SOURCE),
                title=_tr(_PDF_ERROR_TITLE),
            )
            finish([])

        thread.start()
        return None

    def _prepare_jwpub_asset(
        self,
        asset: MediaDestinationAsset,
        completed: Callable[[MediaDestinationAsset, list[dict[str, Any]]], None],
    ) -> None:
        context = self._context
        jwpub_path = asset.import_path
        stem = Path(jwpub_path).stem
        context.notifications.information(
            _tr(_OPENING_SOURCE).format(name=stem)
        )
        thread = context.jwpub_import_thread_factory.create(
            jwpub_path,
            lang=self._media_language_context().api_code,
            parent=context.dialog_parent,
        )
        context.thread_registry.track(thread)
        resolved = False

        def finish(items: list[dict[str, Any]]) -> None:
            nonlocal resolved
            if resolved:
                return
            resolved = True
            completed(asset, items)

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str) -> None:
            finish(
                [
                    dict(item)
                    for item in playlist_items_from_jwpub(items, file_stem)
                ]
            )

        @thread.failed.connect
        def _on_fail(error: str) -> None:
            self._notify_import_error(
                _tr(_JWPUB_OPEN_ERROR_SOURCE).format(error=error[:120]),
                title=_tr(_JWPUB_ERROR_TITLE),
            )
            finish([])

        @thread.finished.connect
        def _on_finished() -> None:
            if resolved:
                return
            self._notify_import_error(
                _tr(_JWPUB_NO_MEDIA_SOURCE),
                title=_tr(_JWPUB_ERROR_TITLE),
            )
            finish([])

        thread.start()

    def _notify_import_error(self, message: str, *, title: str = "") -> None:
        kwargs = {"title": title} if title else {}
        self._context.notifications.error(message, **kwargs)

    @Slot(list)
    def send_to_temp_playlist(self, items: list) -> None:
        if not items:
            return
        self._handlers.switch_to_playlist()
        self._context.playlist_widget.open_temp_playlist(
            items,
            self._context.language_manager,
        )

    def _media_language_context(self) -> JWMediaLanguageContext:
        return jw_media_language_context(self._context.language_manager)

    @staticmethod
    def _items_from_pages(pages: list[str], stem: str) -> list:
        return [
            create_playlist_item(
                title=tr_document_page_title(stem, index + 1),
                url=page_path,
                type="image",
            )
            for index, page_path in enumerate(pages)
        ]
