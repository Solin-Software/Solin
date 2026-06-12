from __future__ import annotations

import os
import tempfile
import zipfile
from urllib.parse import urlparse

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import (
    AUDIO_EXTS,
    DOCX_EXTS,
    IMAGE_EXTS,
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
    PPTX_EXTS,
    VIDEO_EXTS,
)
from ..core.jw.language_context import (
    JWMediaLanguageContext,
    jw_media_language_context,
)
from ..core.media.mime import mime_to_ext
from ..widgets.playlist.items import _new_item


class OpenMediaController:
    """Handles files/URLs opened through argv, drag-to-exe, or IPC."""

    def __init__(self, window) -> None:
        self._window = window

    def open_media_files(self, paths: list) -> None:
        playlist = []
        unsupported = []
        pdf_paths = []
        lo_paths = []
        jwpub_paths = []

        for path in paths:
            if path.startswith(("http://", "https://")):
                url_path = urlparse(path).path
                title = url_path.split("/")[-1] or path
                ext = os.path.splitext(url_path)[1].lower()
                media_type = "audio" if ext in AUDIO_EXTS else "video"
                playlist.append({"url": path, "title": title or path, "type": media_type})
                continue

            ext = os.path.splitext(path)[1].lower()

            if ext in PLAYLIST_EXTS:
                playlist.extend(self.expand_jwlplaylist(path))
                continue
            if ext in JWPUB_EXTS:
                jwpub_paths.append(path)
                continue
            if ext in PDF_EXTS:
                pdf_paths.append(path)
                continue
            if ext in (PPTX_EXTS | DOCX_EXTS):
                from ..core.rendering.libreoffice import libreoffice_available

                if libreoffice_available():
                    lo_paths.append(path)
                else:
                    unsupported.append(path)
                continue
            if ext in VIDEO_EXTS:
                playlist.append({
                    "url": path,
                    "title": os.path.basename(path),
                    "type": "video",
                })
            elif ext in AUDIO_EXTS:
                playlist.append({
                    "url": path,
                    "title": os.path.basename(path),
                    "type": "audio",
                })
            elif ext in IMAGE_EXTS:
                playlist.append({
                    "url": path,
                    "title": os.path.basename(path),
                    "type": "image",
                })
            else:
                unsupported.append(path)

        for jwpub_path in jwpub_paths:
            self.open_jwpub_as_temp(jwpub_path)
        for pdf_path in pdf_paths:
            self.open_pdf_as_temp(pdf_path)
        for lo_path in lo_paths:
            self.open_lo_as_temp(lo_path)

        if unsupported:
            names = "\n".join(f"  • {os.path.basename(path)}" for path in unsupported)
            QMessageBox.warning(
                self._window,
                self._window.tr("Unsupported file"),
                (
                    self._window.tr(
                        "File format not supported. Use videos (mp4, mkv, mov…) "
                        "or images (jpg, png, webp…)."
                    )
                    + f"\n\n{names}"
                ),
            )

        if not playlist:
            return

        if len(playlist) == 1:
            self._window._project_media_at_index(
                playlist,
                index=0,
                keep_expanded=False,
                playback_order=None,
            )
            QTimer.singleShot(50, self._window.proj_bar.expand_overlay)
        else:
            self._window._playlist_imports.send_to_temp_playlist(playlist)

    def open_pdf_as_temp(self, pdf_path: str) -> None:
        from ..core.rendering.pdf import PdfConvertThread, cached_pages

        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]

        pages = cached_pages(pdf_path)
        if pages:
            self.on_pdf_ready(pages, pdf_stem)
            return

        self._window._navigation.switch_page(7)

        thread = PdfConvertThread(pdf_path, parent=self._window)
        if not hasattr(self._window, "_pdf_argv_threads"):
            self._window._pdf_argv_threads = []
        self._window._pdf_argv_threads.append(thread)

        thread.pages_ready.connect(self.on_pdf_ready)
        thread.conversion_failed.connect(
            lambda err: QMessageBox.warning(
                self._window,
                self._window.tr("Error opening PDF"),
                self._window.tr("⚠  Error converting PDF: {error}").replace(
                    "{error}",
                    str(err),
                ),
            )
        )
        thread.finished.connect(
            lambda thread_ref=thread: self._window._pdf_argv_threads.remove(thread_ref)
            if thread_ref in self._window._pdf_argv_threads else None
        )
        thread.start()

    def on_pdf_ready(self, pages: list, pdf_stem: str) -> None:
        items = [
            _new_item(
                title=f"{pdf_stem} — p. {index + 1}",
                url=page_path,
                type="image",
            )
            for index, page_path in enumerate(pages)
        ]
        self._window._navigation.switch_page(7)
        self._window.playlist_widget.open_pdf_as_temp_playlist(items, pdf_stem)

    def open_jwpub_as_temp(self, jwpub_path: str) -> None:
        from pathlib import Path

        from ..core.foundation import paths as _paths_local
        from ..core.jw.publication_reader import JwpubImportThread

        stem = Path(jwpub_path).stem
        lang = self._media_language_context().api_code

        self._window._navigation.switch_page(7)
        self._window.playlist_widget.open_pdf_as_temp_playlist([], f"📖  {stem}")

        thread = JwpubImportThread.create(
            jwpub_path,
            lang=lang,
            dest_images_dir=_paths_local.IMAGES_DIR,
            parent=self._window,
        )

        if not hasattr(self._window, "_jwpub_argv_threads"):
            self._window._jwpub_argv_threads = []
        self._window._jwpub_argv_threads.append(thread)
        thread.finished.connect(
            lambda thread_ref=thread: self._window._jwpub_argv_threads.remove(thread_ref)
            if thread_ref in self._window._jwpub_argv_threads else None
        )

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str):
            playlist_widget = self._window.playlist_widget
            edit_view = playlist_widget._edit_view
            playlist = edit_view._pl
            if playlist is None:
                return

            new_items = []
            for raw in items:
                item = _new_item(
                    title=raw.get("title", file_stem),
                    url=raw.get("url", ""),
                    type=raw.get("type", "video"),
                    key_symbol=raw.get("key_symbol"),
                    track=raw.get("track"),
                    issue_tag=raw.get("issue_tag"),
                    doc_id=raw.get("doc_id"),
                )
                if raw.get("url") and raw.get("type") != "image":
                    item["auto_title"] = False
                new_items.append(item)

            if not new_items:
                QMessageBox.information(
                    self._window,
                    self._window.tr("No media found"),
                    self._window.tr("No media items found in {name}.").replace(
                        "{name}",
                        file_stem,
                    ),
                )
                return

            playlist.setdefault("items", []).extend(new_items)
            edit_view._rebuild_list()
            loaded_count = sum(1 for item in new_items if item.get("url"))
            self._window.notifications.success(
                f"{file_stem} — {loaded_count} items"
            )

        @thread.failed.connect
        def _on_fail(err: str):
            QMessageBox.warning(
                self._window,
                self._window.tr("Error opening .jwpub"),
                f"⚠  {stem}: {err}",
            )

        thread.start()

    def open_lo_as_temp(self, lo_path: str) -> None:
        from ..core.rendering.libreoffice import LoConvertThread, cached_pages

        lo_stem = os.path.splitext(os.path.basename(lo_path))[0]

        pages = cached_pages(lo_path)
        if pages:
            self.on_lo_ready(pages, lo_stem)
            return

        self._window._navigation.switch_page(7)

        thread = LoConvertThread(lo_path, parent=self._window)
        if not hasattr(self._window, "_lo_argv_threads"):
            self._window._lo_argv_threads = []
        self._window._lo_argv_threads.append(thread)

        thread.pages_ready.connect(self.on_lo_ready)
        thread.conversion_failed.connect(
            lambda err: QMessageBox.warning(
                self._window,
                self._window.tr("Error opening PDF"),
                self._window.tr("⚠  Error converting PDF: {error}").replace(
                    "{error}",
                    str(err),
                ),
            )
        )
        thread.finished.connect(
            lambda thread_ref=thread: self._window._lo_argv_threads.remove(thread_ref)
            if thread_ref in self._window._lo_argv_threads else None
        )
        thread.start()

    # Alias for compatibility with the old PPTX-specific name.
    open_pptx_as_temp = open_lo_as_temp

    def on_lo_ready(self, pages: list, stem: str) -> None:
        items = [
            _new_item(
                title=f"{stem} — p. {index + 1}",
                url=page_path,
                type="image",
            )
            for index, page_path in enumerate(pages)
        ]
        self._window._navigation.switch_page(7)
        self._window.playlist_widget.open_pdf_as_temp_playlist(items, stem)

    # Alias for compatibility with the old PPTX-specific signal handler.
    on_pptx_ready = on_lo_ready

    def expand_jwlplaylist(self, path: str) -> list:
        from ..core.playlists.reader import read_jwlplaylist

        try:
            parsed = read_jwlplaylist(
                path,
                fallback_lang_code=self._media_language_context().fallback_code,
            )
        except zipfile.BadZipFile:
            QMessageBox.warning(
                self._window,
                self._window.tr("Unsupported file"),
                self._window.tr("Invalid or corrupted .jwlplaylist file:\n%1").replace(
                    "%1",
                    os.path.basename(path),
                ),
            )
            return []
        except (OSError, ValueError) as err:
            QMessageBox.warning(
                self._window,
                self._window.tr("Unsupported file"),
                self._window.tr("Error reading %1:\n%2")
                .replace("%1", os.path.basename(path))
                .replace("%2", str(err)),
            )
            return []

        result = []
        skipped = []
        for item in parsed.get("items", []):
            media_type = item.get("type", "video")
            source = item.get("source", "")
            title = item.get("title", "Item")

            if source == "embedded" and media_type == "image":
                data = item.get("data")
                if not data:
                    continue
                result.append({
                    "url": self._write_tmp(data, self._best_ext(item, "image/jpeg")),
                    "title": title,
                    "type": "image",
                    "_tmp": True,
                })
            elif source == "embedded" and media_type in ("video", "audio"):
                data = item.get("data")
                if not data:
                    skipped.append(title)
                    continue
                result.append({
                    "url": self._write_tmp(data, self._best_ext(item, "video/mp4")),
                    "title": title,
                    "type": media_type,
                    "_tmp": True,
                })
            elif source == "jworg":
                url = item.get("jworg_url") or item.get("url")
                if not url:
                    skipped.append(title)
                    continue
                result.append({"url": url, "title": title, "type": media_type})
            else:
                url = item.get("url")
                if url:
                    result.append({"url": url, "title": title, "type": media_type})

        if skipped:
            names = "\n".join(f"  • {title}" for title in skipped)
            QMessageBox.warning(
                self._window,
                "JW Library Playlist",
                f"Itens não resolvidos (sem conexão com a internet?):\n\n{names}",
            )

        return result

    def _best_ext(self, item: dict, default_mime: str) -> str:
        filename = item.get("filename", "")
        if filename:
            ext = os.path.splitext(filename)[1].lower()
            if ext:
                return ext
        return mime_to_ext(item.get("mime_type", default_mime))

    def _write_tmp(self, data: bytes, suffix: str) -> str:
        tmp = tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
            prefix="solin_jwl_",
        )
        tmp.write(data)
        tmp.close()
        self._window._jwl_tmp_files.add(tmp.name)
        return tmp.name

    def _media_language_context(self) -> JWMediaLanguageContext:
        lang_manager = getattr(
            self._window,
            "_lang_mgr",
            getattr(self._window, "_lang_manager", getattr(self._window, "lang", None)),
        )
        return jw_media_language_context(lang_manager)
