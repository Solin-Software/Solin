from __future__ import annotations

import os
import zipfile

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import JWPUB_EXTS, PDF_EXTS, PLAYLIST_EXTS
from ..core.foundation.runtime_paths import ProfilePaths
from ..core.jw.language_context import (
    JWMediaLanguageContext,
    jw_media_language_context,
)
from ..core.media.formats import media_type_from_path, mime_to_ext
from ..core.playlists.items import create_playlist_item


class PlaylistImportController:
    """Handles adding projected, downloaded, and imported media to playlists."""

    def __init__(self, window, profile_paths: ProfilePaths) -> None:
        self._window = window
        self._profile_paths = profile_paths

    def add_current_to_playlist(self, url: str, title: str, meta: object) -> None:
        if not url:
            return
        target = self._choose_target(title)
        if target is None:
            return

        meta_dict = dict(meta) if isinstance(meta, dict) else {}
        mtype = meta_dict.get("type") or media_type_from_path(url, default="video")
        kw = {k: value for k, value in meta_dict.items() if k != "type"}
        item = create_playlist_item(title=title, url=url, type=mtype, **kw)

        if target.create_new:
            self._window.playlist_widget.create_playlist_with_item(
                target.playlist_name,
                item,
            )
            self._window.notifications.success(
                self._window.tr('Playlist "%1"\ncreated successfully!').replace(
                    "%1",
                    target.playlist_name,
                )
            )
            return

        added = self._window.playlist_widget.add_item_to_playlist(
            target.playlist_id,
            item,
        )
        if added:
            self._window.notifications.success(
                self._window.tr('Added to playlist\n"%1"').replace(
                    "%1",
                    target.playlist_name,
                )
            )
        else:
            self._window.notifications.warning(
                self._window.tr('This media is already in\nplaylist "%1"').replace(
                    "%1",
                    target.playlist_name,
                )
            )

    @Slot(str, str)
    def browser_download_failed(self, title: str, error: str) -> None:
        self._window.notifications.error(
            self._window.tr("Could not download %1:\n%2")
            .replace("%1", title)
            .replace("%2", error),
            title=self._window.tr("Download failed"),
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
            return

        if kind == "pdf" or ext in PDF_EXTS:
            self.add_pdf_file_to_playlist_target(path, target)
            return

        if kind == "jwpub" or ext in JWPUB_EXTS:
            self.add_jwpub_file_to_playlist_target(path, target)

    def items_from_jwlplaylist_for_playlist(self, jwl_path: str) -> list:
        from ..core.playlists.reader import read_jwlplaylist

        fallback_lang = self._media_language_context().fallback_code

        try:
            data = read_jwlplaylist(jwl_path, fallback_lang_code=fallback_lang)
        except zipfile.BadZipFile:
            QMessageBox.warning(
                self._window,
                self._window.tr("Unsupported file"),
                self._window.tr("Invalid or corrupted .jwlplaylist file:\n%1").replace(
                    "%1",
                    os.path.basename(jwl_path),
                ),
            )
            return []
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self._window,
                self._window.tr("Unsupported file"),
                self._window.tr("Error reading %1:\n%2")
                .replace("%1", os.path.basename(jwl_path))
                .replace("%2", str(exc)),
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
                self._profile_paths.embedded_dir.mkdir(parents=True, exist_ok=True)
                ext = os.path.splitext(raw.get("filename", ""))[1].lower()
                if not ext:
                    ext = mime_to_ext(raw.get("mime_type", ""))
                embedded_path = self._profile_paths.embedded_dir / f"{item['id']}{ext}"
                try:
                    with embedded_path.open("wb") as fh:
                        fh.write(raw["data"])
                except OSError:
                    skipped.append(item.get("title", "Item"))
                    continue
                item["url"] = os.fspath(embedded_path)
                item["type"] = raw.get("type", "video")
            elif not url:
                skipped.append(item.get("title", "Item"))
                continue

            new_items.append(item)

        if skipped:
            names = "\n".join(f"  • {name}" for name in skipped)
            QMessageBox.warning(
                self._window,
                "JW Library Playlist",
                f"Itens não resolvidos (sem conexão com a internet?):\n\n{names}",
            )

        return new_items

    def add_items_to_playlist_target(self, target, items: list, source_name: str) -> None:
        if not items:
            self._window.notifications.warning(
                self._window.tr("No media found in {name}").replace("{name}", source_name)
            )
            return

        if target.create_new:
            playlist_id = self._window.playlist_widget.create_playlist_with_item(
                target.playlist_name,
                items[0],
            )
            for item in items[1:]:
                self._window.playlist_widget.add_item_to_playlist(playlist_id, item)
            self._window.notifications.success(
                self._window.tr('Playlist "%1"\ncreated successfully!').replace(
                    "%1",
                    target.playlist_name,
                )
            )
            return

        added = 0
        for item in items:
            if self._window.playlist_widget.add_item_to_playlist(target.playlist_id, item):
                added += 1

        if added:
            self._window.notifications.success(
                self._window.tr('%1 file(s) added\nto playlist "%2"')
                .replace("%1", str(added))
                .replace("%2", target.playlist_name)
            )
        else:
            self._window.notifications.warning(
                self._window.tr('This media is already in\nplaylist "%1"').replace(
                    "%1",
                    target.playlist_name,
                )
            )

    def add_pdf_file_to_playlist_target(self, pdf_path: str, target) -> None:
        from ..core.rendering.pdf import PdfConvertThread, cached_pages

        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        runtime_paths = self._window.runtime_paths

        pages = cached_pages(pdf_path, runtime_paths.pdf_pages_dir)
        if pages:
            self.add_items_to_playlist_target(
                target,
                self._items_from_pages(pages, pdf_stem),
                os.path.basename(pdf_path),
            )
            return

        self._window.notifications.information(
            self._window.tr("Opening {name}...").replace("{name}", pdf_stem)
        )

        thread = PdfConvertThread(
            pdf_path,
            runtime_paths.pdf_pages_dir,
            parent=self._window,
        )
        if not hasattr(self._window, "_browser_pdf_threads"):
            self._window._browser_pdf_threads = []
        self._window._browser_pdf_threads.append(thread)

        thread.pages_ready.connect(
            lambda pages_ready, stem: self.add_items_to_playlist_target(
                target,
                self._items_from_pages(pages_ready, stem),
                os.path.basename(pdf_path),
            )
        )
        thread.conversion_failed.connect(
            lambda err: self._window.notifications.error(
                self._window.tr("Error converting PDF: {error}").replace(
                    "{error}",
                    str(err),
                ),
                title=self._window.tr("Error opening PDF"),
            )
        )
        thread.finished.connect(
            lambda thread_ref=thread: self._window._browser_pdf_threads.remove(thread_ref)
            if thread_ref in self._window._browser_pdf_threads else None
        )
        thread.start()

    def add_jwpub_file_to_playlist_target(self, jwpub_path: str, target) -> None:
        from pathlib import Path

        from ..core.jw.publication_reader import JwpubImportThread

        stem = Path(jwpub_path).stem
        lang = self._media_language_context().api_code

        self._window.notifications.information(
            self._window.tr("Opening {name}...").replace("{name}", stem)
        )

        thread = JwpubImportThread.create(
            jwpub_path,
            lang=lang,
            dest_images_dir=os.fspath(self._profile_paths.images_dir),
            parent=self._window,
        )
        if not hasattr(self._window, "_browser_jwpub_threads"):
            self._window._browser_jwpub_threads = []
        self._window._browser_jwpub_threads.append(thread)
        thread.finished.connect(
            lambda thread_ref=thread: self._window._browser_jwpub_threads.remove(thread_ref)
            if thread_ref in self._window._browser_jwpub_threads else None
        )

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str):
            new_items = []
            for raw in items:
                item = create_playlist_item(
                    title=raw.get("title", file_stem),
                    url=raw.get("url", ""),
                    type=raw.get("type", "video"),
                    key_symbol=raw.get("key_symbol"),
                    track=raw.get("track"),
                    issue_tag=raw.get("issue_tag"),
                    doc_id=raw.get("doc_id"),
                    meps_language=raw.get("meps_language", 0),
                )
                if raw.get("url") and raw.get("type") != "image":
                    item["auto_title"] = False
                new_items.append(item)

            self.add_items_to_playlist_target(target, new_items, file_stem)

        @thread.failed.connect
        def _on_fail(err: str):
            self._window.notifications.error(
                self._window.tr("Could not open .jwpub: {err}").replace(
                    "{err}",
                    err[:120],
                ),
                title=self._window.tr("Error opening .jwpub"),
            )

        thread.start()

    @Slot(list)
    def send_to_temp_playlist(self, items: list) -> None:
        if not items:
            return
        self._window._navigation.switch_page(7)
        self._window.playlist_widget.open_temp_playlist(items, self._window.lang)

    def _choose_target(self, title: str):
        from ..widgets.playlist.target_dialog import choose_playlist_target

        return choose_playlist_target(
            self._window,
            title,
            self._window.playlist_widget.get_playlist_names(),
            self._window.tr("Add to Playlist"),
        )

    def _media_language_context(self) -> JWMediaLanguageContext:
        lang_manager = getattr(
            self._window,
            "_lang_mgr",
            getattr(self._window, "_lang_manager", getattr(self._window, "lang", None)),
        )
        return jw_media_language_context(lang_manager)

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
