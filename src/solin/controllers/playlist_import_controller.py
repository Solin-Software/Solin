from __future__ import annotations

import os
import zipfile
from collections.abc import Callable
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
from ..core.media.formats import media_type_from_path, mime_to_ext
from ..core.playlists.items import (
    create_playlist_item,
    playlist_items_from_jwpub,
)

if TYPE_CHECKING:
    from ..core.jw.publication_reader import JwpubImportThreadFactory
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

    def add_current_to_playlist(self, url: str, title: str, meta: object) -> None:
        if not url:
            return
        target = self._choose_target(title)
        if target is None:
            return

        context = self._context
        meta_dict = dict(meta) if isinstance(meta, dict) else {}
        media_type = meta_dict.get("type") or media_type_from_path(
            url,
            default="video",
        )
        attributes = {
            key: value
            for key, value in meta_dict.items()
            if key != "type"
        }
        item = create_playlist_item(
            title=title,
            url=url,
            type=media_type,
            **attributes,
        )

        if target.create_new:
            context.playlist_widget.create_playlist_with_item(
                target.playlist_name,
                item,
            )
            context.notifications.success(
                context.translate('Playlist "%1"\ncreated successfully!').replace(
                    "%1",
                    target.playlist_name,
                )
            )
            return

        added = context.playlist_widget.add_item_to_playlist(
            target.playlist_id,
            item,
        )
        if added:
            context.notifications.success(
                context.translate('Added to playlist\n"%1"').replace(
                    "%1",
                    target.playlist_name,
                )
            )
        else:
            context.notifications.warning(
                context.translate(
                    'This media is already in\nplaylist "%1"'
                ).replace("%1", target.playlist_name)
            )

    @Slot(str, str)
    def browser_download_failed(self, title: str, error: str) -> None:
        context = self._context
        context.notifications.error(
            context.translate("Could not download %1:\n%2")
            .replace("%1", title)
            .replace("%2", error),
            title=context.translate("Download failed"),
            dedupe_key=f"browser-download:{title}:{error}",
        )

    @Slot(str, str, str)
    def add_browser_downloaded_file(
        self,
        path: str,
        title: str,
        kind: str,
    ) -> None:
        if not path or not os.path.isfile(path):
            return

        target = self._choose_target(title or os.path.basename(path))
        if target is None:
            return

        ext = os.path.splitext(path)[1].lower()
        if kind == "jwlplaylist" or ext in PLAYLIST_EXTS:
            items = self.items_from_jwlplaylist_for_playlist(path)
            self.add_items_to_playlist_target(target, items, os.path.basename(path))
        elif kind == "pdf" or ext in PDF_EXTS:
            self.add_pdf_file_to_playlist_target(path, target)
        elif kind == "jwpub" or ext in JWPUB_EXTS:
            self.add_jwpub_file_to_playlist_target(path, target)

    def items_from_jwlplaylist_for_playlist(self, jwl_path: str) -> list:
        from ..core.playlists.reader import read_jwlplaylist

        context = self._context
        try:
            data = read_jwlplaylist(
                jwl_path,
                fallback_lang_code=self._media_language_context().fallback_code,
            )
        except zipfile.BadZipFile:
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

        new_items = []
        skipped = []
        for raw in data.get("items", []):
            url = raw.get("url") or raw.get("jworg_url") or ""
            item = create_playlist_item(
                title=raw.get("title", "") or os.path.basename(jwl_path),
                url=url,
                type=raw.get("type", "video"),
                key_symbol=raw.get("key_symbol"),
                track=raw.get("track"),
                issue_tag=raw.get("issue_tag"),
                doc_id=raw.get("doc_id"),
                meps_language=raw.get("language", 0),
            )

            if raw.get("data") and not url:
                try:
                    item["url"] = context.profile_media_store.save_embedded(
                        raw["data"],
                        raw.get("filename", "media"),
                        identifier=item["id"],
                        default_suffix=mime_to_ext(raw.get("mime_type", "")),
                    )
                except (OSError, ValueError):
                    skipped.append(item.get("title", "Item"))
                    continue
                item["type"] = raw.get("type", "video")
            elif not url:
                skipped.append(item.get("title", "Item"))
                continue

            new_items.append(item)

        if skipped:
            names = "\n".join(f"  • {name}" for name in skipped)
            QMessageBox.warning(
                context.dialog_parent,
                "JW Library Playlist",
                f"Itens não resolvidos (sem conexão com a internet?):\n\n{names}",
            )
        return new_items

    def add_items_to_playlist_target(self, target, items: list, source_name: str) -> None:
        context = self._context
        playlist_widget = context.playlist_widget
        if not items:
            context.notifications.warning(
                context.translate("No media found in {name}").replace(
                    "{name}",
                    source_name,
                )
            )
            return

        if target.create_new:
            playlist_id = playlist_widget.create_playlist_with_item(
                target.playlist_name,
                items[0],
            )
            for item in items[1:]:
                playlist_widget.add_item_to_playlist(playlist_id, item)
            context.notifications.success(
                context.translate('Playlist "%1"\ncreated successfully!').replace(
                    "%1",
                    target.playlist_name,
                )
            )
            return

        added = sum(
            bool(playlist_widget.add_item_to_playlist(target.playlist_id, item))
            for item in items
        )
        if added:
            context.notifications.success(
                context.translate('%1 file(s) added\nto playlist "%2"')
                .replace("%1", str(added))
                .replace("%2", target.playlist_name)
            )
        else:
            context.notifications.warning(
                context.translate(
                    'This media is already in\nplaylist "%1"'
                ).replace("%1", target.playlist_name)
            )

    def add_pdf_file_to_playlist_target(self, pdf_path: str, target) -> None:
        context = self._context
        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        pages = context.document_conversion_service.cached_pdf_pages(pdf_path)
        if pages:
            self.add_items_to_playlist_target(
                target,
                self._items_from_pages(pages, pdf_stem),
                os.path.basename(pdf_path),
            )
            return

        context.notifications.information(
            context.translate("Opening {name}...").replace("{name}", pdf_stem)
        )
        thread = context.document_conversion_service.create_pdf_thread(
            pdf_path,
            parent=context.dialog_parent,
        )
        context.thread_registry.track(thread)
        thread.pages_ready.connect(
            lambda pages_ready, stem: self.add_items_to_playlist_target(
                target,
                self._items_from_pages(pages_ready, stem),
                os.path.basename(pdf_path),
            )
        )
        thread.conversion_failed.connect(
            lambda error: context.notifications.error(
                context.translate("Error converting PDF: {error}").replace(
                    "{error}",
                    str(error),
                ),
                title=context.translate("Error opening PDF"),
            )
        )
        thread.start()

    def add_jwpub_file_to_playlist_target(self, jwpub_path: str, target) -> None:
        context = self._context
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

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str) -> None:
            self.add_items_to_playlist_target(
                target,
                playlist_items_from_jwpub(items, file_stem),
                file_stem,
            )

        @thread.failed.connect
        def _on_fail(error: str) -> None:
            context.notifications.error(
                context.translate("Could not open .jwpub: {err}").replace(
                    "{err}",
                    error[:120],
                ),
                title=context.translate("Error opening .jwpub"),
            )

        thread.start()

    @Slot(list)
    def send_to_temp_playlist(self, items: list) -> None:
        if not items:
            return
        self._handlers.switch_to_playlist()
        self._context.playlist_widget.open_temp_playlist(
            items,
            self._context.language_manager,
        )

    def _choose_target(self, title: str):
        from ..widgets.playlist.target_dialog import choose_playlist_target

        context = self._context
        return choose_playlist_target(
            context.dialog_parent,
            title,
            context.playlist_widget.get_playlist_names(),
            context.translate("Add to Playlist"),
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
