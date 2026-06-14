from __future__ import annotations

import os
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import (
    DOCX_EXTS,
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
    PPTX_EXTS,
)
from ..core.foundation.qt_threads import OwnedQThreadRegistry
from ..core.foundation.runtime_paths import ProfilePaths, RuntimePaths
from ..core.jw.language_context import (
    JWMediaLanguageContext,
    jw_media_language_context,
)
from ..core.media.formats import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    mime_to_ext,
)
from ..core.playlists.items import create_playlist_item, playlist_items_from_jwpub


@dataclass(frozen=True, slots=True)
class OpenMediaContext:
    """Stable services and paths used by file-open workflows."""

    dialog_parent: Any
    runtime_paths: RuntimePaths
    profile_paths: ProfilePaths
    language_manager: Any
    notifications: Any
    thread_registry: OwnedQThreadRegistry
    temp_files: set[str]
    translate: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class OpenMediaHandlers:
    """Presentation actions triggered by file-open workflows."""

    switch_to_playlist: Callable[[], None]
    project_media_at_index: Callable[..., None]
    expand_projection_overlay: Callable[[], None]
    send_to_temp_playlist: Callable[[list], None]
    open_pdf_temp_playlist: Callable[[list, str], str]
    open_named_temp_playlist: Callable[[list, str], str]
    append_temp_playlist_items: Callable[[str, list[dict]], bool]


class OpenMediaController:
    """Handles files and URLs opened through argv, drag-to-exe, or IPC."""

    def __init__(
        self,
        context: OpenMediaContext,
        handlers: OpenMediaHandlers,
    ) -> None:
        self._context = context
        self._handlers = handlers

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
            elif ext in JWPUB_EXTS:
                jwpub_paths.append(path)
            elif ext in PDF_EXTS:
                pdf_paths.append(path)
            elif ext in (PPTX_EXTS | DOCX_EXTS):
                from ..core.rendering.libreoffice import libreoffice_available

                if libreoffice_available():
                    lo_paths.append(path)
                else:
                    unsupported.append(path)
            elif ext in VIDEO_EXTS:
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
            translate = self._context.translate
            QMessageBox.warning(
                self._context.dialog_parent,
                translate("Unsupported file"),
                (
                    translate(
                        "File format not supported. Use videos (mp4, mkv, mov…) "
                        "or images (jpg, png, webp…)."
                    )
                    + f"\n\n{names}"
                ),
            )

        if not playlist:
            return

        if len(playlist) == 1:
            self._handlers.project_media_at_index(
                playlist,
                index=0,
                keep_expanded=False,
                playback_order=None,
            )
            QTimer.singleShot(50, self._handlers.expand_projection_overlay)
        else:
            self._handlers.send_to_temp_playlist(playlist)

    def open_pdf_as_temp(self, pdf_path: str) -> None:
        from ..core.rendering.pdf import PdfConvertThread, cached_pages

        context = self._context
        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        pages = cached_pages(pdf_path, context.runtime_paths.pdf_pages_dir)
        if pages:
            self.on_pdf_ready(pages, pdf_stem)
            return

        self._handlers.switch_to_playlist()
        thread = PdfConvertThread(
            pdf_path,
            context.runtime_paths.pdf_pages_dir,
            parent=context.dialog_parent,
        )
        context.thread_registry.track(thread)
        thread.pages_ready.connect(self.on_pdf_ready)
        thread.conversion_failed.connect(
            lambda error: self._show_conversion_error(error)
        )
        thread.start()

    def on_pdf_ready(self, pages: list, pdf_stem: str) -> None:
        items = self._page_items(pages, pdf_stem)
        self._handlers.switch_to_playlist()
        self._handlers.open_pdf_temp_playlist(items, pdf_stem)

    def open_jwpub_as_temp(self, jwpub_path: str) -> None:
        from ..core.jw.publication_reader import JwpubImportThread

        context = self._context
        stem = Path(jwpub_path).stem
        self._handlers.switch_to_playlist()
        playlist_id = self._handlers.open_named_temp_playlist([], f"📖  {stem}")
        thread = JwpubImportThread.create(
            jwpub_path,
            lang=self._media_language_context().api_code,
            dest_images_dir=os.fspath(context.profile_paths.images_dir),
            parent=context.dialog_parent,
        )
        context.thread_registry.track(thread)

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str) -> None:
            new_items = playlist_items_from_jwpub(items, file_stem)
            if not new_items:
                QMessageBox.information(
                    context.dialog_parent,
                    context.translate("No media found"),
                    context.translate("No media items found in {name}.").replace(
                        "{name}",
                        file_stem,
                    ),
                )
                return
            if not self._handlers.append_temp_playlist_items(
                playlist_id,
                new_items,
            ):
                return
            loaded_count = sum(1 for item in new_items if item.get("url"))
            context.notifications.success(f"{file_stem} — {loaded_count} items")

        @thread.failed.connect
        def _on_fail(error: str) -> None:
            QMessageBox.warning(
                context.dialog_parent,
                context.translate("Error opening .jwpub"),
                f"⚠  {stem}: {error}",
            )

        thread.start()

    def open_lo_as_temp(self, lo_path: str) -> None:
        from ..core.rendering.libreoffice import LoConvertThread, cached_pages

        context = self._context
        lo_stem = os.path.splitext(os.path.basename(lo_path))[0]
        runtime_paths = context.runtime_paths
        pages = cached_pages(
            lo_path,
            pptx_pages_dir=runtime_paths.pptx_pages_dir,
            docx_pages_dir=runtime_paths.docx_pages_dir,
        )
        if pages:
            self.on_lo_ready(pages, lo_stem)
            return

        self._handlers.switch_to_playlist()
        thread = LoConvertThread(
            lo_path,
            pptx_pages_dir=runtime_paths.pptx_pages_dir,
            docx_pages_dir=runtime_paths.docx_pages_dir,
            pdf_pages_dir=runtime_paths.pdf_pages_dir,
            parent=context.dialog_parent,
        )
        context.thread_registry.track(thread)
        thread.pages_ready.connect(self.on_lo_ready)
        thread.conversion_failed.connect(
            lambda error: self._show_conversion_error(error)
        )
        thread.start()

    def on_lo_ready(self, pages: list, stem: str) -> None:
        items = self._page_items(pages, stem)
        self._handlers.switch_to_playlist()
        self._handlers.open_pdf_temp_playlist(items, stem)

    def expand_jwlplaylist(self, path: str) -> list:
        from ..core.playlists.reader import read_jwlplaylist

        context = self._context
        try:
            parsed = read_jwlplaylist(
                path,
                fallback_lang_code=self._media_language_context().fallback_code,
            )
        except zipfile.BadZipFile:
            QMessageBox.warning(
                context.dialog_parent,
                context.translate("Unsupported file"),
                context.translate("Invalid or corrupted .jwlplaylist file:\n%1").replace(
                    "%1",
                    os.path.basename(path),
                ),
            )
            return []
        except (OSError, ValueError) as error:
            QMessageBox.warning(
                context.dialog_parent,
                context.translate("Unsupported file"),
                context.translate("Error reading %1:\n%2")
                .replace("%1", os.path.basename(path))
                .replace("%2", str(error)),
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
                if data:
                    result.append({
                        "url": self._write_tmp(
                            data,
                            self._best_ext(item, "image/jpeg"),
                        ),
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
                    "url": self._write_tmp(
                        data,
                        self._best_ext(item, "video/mp4"),
                    ),
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
                context.dialog_parent,
                "JW Library Playlist",
                f"Itens não resolvidos (sem conexão com a internet?):\n\n{names}",
            )
        return result

    def _show_conversion_error(self, error: object) -> None:
        context = self._context
        QMessageBox.warning(
            context.dialog_parent,
            context.translate("Error opening PDF"),
            context.translate("⚠  Error converting PDF: {error}").replace(
                "{error}",
                str(error),
            ),
        )

    @staticmethod
    def _page_items(pages: list, stem: str) -> list[dict]:
        return [
            create_playlist_item(
                title=f"{stem} — p. {index + 1}",
                url=page_path,
                type="image",
            )
            for index, page_path in enumerate(pages)
        ]

    @staticmethod
    def _best_ext(item: dict, default_mime: str) -> str:
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
        self._context.temp_files.add(tmp.name)
        return tmp.name

    def _media_language_context(self) -> JWMediaLanguageContext:
        return jw_media_language_context(self._context.language_manager)
