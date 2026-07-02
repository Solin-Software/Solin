from __future__ import annotations

import os
from zipfile import BadZipFile
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import JWPUB_EXTS, PDF_EXTS, PLAYLIST_EXTS
from ..core.foundation.qt_threads import OwnedQThreadRegistry
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
from ..core.media.destinations import (
    MediaDestinationAsset,
    MediaDestinationOutcome,
    PlaylistDestinationTarget,
    PreparedMediaBatch,
)
from ..core.media.placement import END_OF_LIST_INDEX

if TYPE_CHECKING:
    from ..core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from ..core.rendering.document_conversion import DocumentConversionService


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
    translate: Callable[[str], str]


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
                        self._context.translate("File not found: {name}").replace(
                            "{name}", asset.title
                        )
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
                    self._context.translate("Unsupported file: {name}").replace(
                        "{name}", asset.title
                    )
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
                context.translate("Unsupported file"),
                context.translate(
                    "Invalid or corrupted .jwlplaylist file:\n%1"
                ).replace("%1", os.path.basename(jwl_path)),
            )
            return []
        except (OSError, ValueError) as error:
            QMessageBox.warning(
                context.dialog_parent,
                context.translate("Unsupported file"),
                context.translate("Error reading %1:\n%2")
                .replace("%1", os.path.basename(jwl_path))
                .replace("%2", str(error)),
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
                "JW Library Playlist",
                f"Itens não resolvidos (sem conexão com a internet?):\n\n{names}",
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
                context.translate("No media found in {name}").replace(
                    "{name}",
                    source_name,
                )
            )
            return MediaDestinationOutcome(0)

        if target.create_new:
            playlist_id = playlist_widget.create_playlist_with_items(
                target.playlist_name,
                [items[0]],
            )
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
                context.translate('Playlist "%1"\ncreated successfully!').replace(
                    "%1",
                    target.playlist_name,
                )
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
                context.translate('%1 file(s) added\nto playlist "%2"')
                .replace("%1", str(added))
                .replace("%2", target.playlist_name)
            )
            if result.duplicate_count:
                context.notifications.warning(
                    context.translate(
                        "{count} media item(s) were already added"
                    ).replace("{count}", str(result.duplicate_count))
                )
        elif not result.target_valid:
            context.notifications.error(
                context.translate(
                    'Could not add media to playlist "%1" because the selected location no longer exists.'
                ).replace("%1", target.playlist_name)
            )
            return MediaDestinationOutcome(0, destination_accepted=False)
        else:
            context.notifications.warning(
                context.translate(
                    'This media is already in\nplaylist "%1"'
                ).replace("%1", target.playlist_name)
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
            context.translate("Opening {name}...").replace("{name}", pdf_stem)
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
                context.translate("Error converting PDF: {error}").replace(
                    "{error}", str(error)
                ),
                title=context.translate("Error opening PDF"),
            )
            finish([])

        @thread.finished.connect
        def on_finished() -> None:
            if resolved:
                return
            self._notify_import_error(
                context.translate("PDF conversion finished without any pages."),
                title=context.translate("Error opening PDF"),
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
            context.translate("Opening {name}...").replace("{name}", stem)
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
                context.translate("Could not open .jwpub: {err}").replace(
                    "{err}",
                    error[:120],
                ),
                title=context.translate("Error opening .jwpub"),
            )
            finish([])

        @thread.finished.connect
        def _on_finished() -> None:
            if resolved:
                return
            self._notify_import_error(
                context.translate("The .jwpub file contained no usable media."),
                title=context.translate("Error opening .jwpub"),
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
                title=f"{stem} - p. {index + 1}",
                url=page_path,
                type="image",
            )
            for index, page_path in enumerate(pages)
        ]
