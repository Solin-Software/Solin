from __future__ import annotations

import os
import tempfile
from zipfile import BadZipFile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import urlparse

from PySide6.QtCore import QCoreApplication, QObject, QTimer, QT_TRANSLATE_NOOP
from PySide6.QtWidgets import QMessageBox

from ..core.foundation.constants import (
    DOCX_EXTS,
    JWL_PLAYLIST_EXTS,
    JWPUB_EXTS,
    PDF_EXTS,
    PPTX_EXTS,
    SOLIN_PLAYLIST_EXTS,
)
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
from ..core.media.formats import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
)
from ..core.playlists.jwl_files import read_jwlplaylist_document
from ..core.playlists.jwl_import import playlist_items_from_jwl_document_items
from ..core.playlists.items import create_playlist_item, playlist_items_from_jwpub

if TYPE_CHECKING:
    from ..core.jw.jwpub_import_thread import JwpubImportThreadFactory
    from ..core.rendering.document_conversion import DocumentConversionService


class OpenMediaNotifications(QObject):
    """Qt translation context for file-open notifications."""

    @staticmethod
    def items_added(name: str, count: int) -> str:
        return OpenMediaNotifications.tr(
            "{name}: %n item(s) added",
            "",
            max(0, int(count)),
        ).format(name=name)


_NOTIFICATION_CONTEXT = OpenMediaNotifications.__name__
_UNSUPPORTED_FILE_TITLE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "Unsupported file",
)
_UNSUPPORTED_FILES_SOURCE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "File format not supported. Use videos (mp4, mkv, mov…) "
    "or images (jpg, png, webp…).\n\n{files}",
)
_NO_MEDIA_TITLE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "No media found",
)
_NO_MEDIA_SOURCE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "No media items found in {name}.",
)
_JWPUB_ERROR_TITLE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "Error opening .jwpub",
)
_OPEN_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "Could not open {name}.\n{error}",
)
_INVALID_PLAYLIST_SOURCE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "Invalid or corrupted .jwlplaylist file:\n{name}",
)
_READ_PLAYLIST_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "Could not read {name}.\n{error}",
)
_PDF_ERROR_TITLE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "Error opening PDF",
)
_PDF_CONVERSION_ERROR_SOURCE = QT_TRANSLATE_NOOP(
    "OpenMediaNotifications",
    "Could not convert PDF.\n{error}",
)


def _tr(source: str) -> str:
    return QCoreApplication.translate(_NOTIFICATION_CONTEXT, source)


@dataclass(frozen=True, slots=True)
class OpenMediaContext:
    """Stable services and paths used by file-open workflows."""

    dialog_parent: Any
    document_conversion_service: DocumentConversionService
    jwpub_import_thread_factory: JwpubImportThreadFactory
    language_manager: Any
    notifications: Any
    thread_registry: OwnedQThreadRegistry
    temp_files: set[str]


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
    import_native_playlists: Callable[[list[str], bool], None]


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
        native_playlist_paths = []

        for path in paths:
            if path.startswith(("http://", "https://")):
                url_path = urlparse(path).path
                title = url_path.split("/")[-1] or path
                ext = os.path.splitext(url_path)[1].lower()
                media_type = "audio" if ext in AUDIO_EXTS else "video"
                playlist.append({"url": path, "title": title or path, "type": media_type})
                continue

            ext = os.path.splitext(path)[1].lower()
            if ext in SOLIN_PLAYLIST_EXTS:
                native_playlist_paths.append(path)
            elif ext in JWL_PLAYLIST_EXTS:
                playlist.extend(self.expand_jwlplaylist(path))
            elif ext in JWPUB_EXTS:
                jwpub_paths.append(path)
            elif ext in PDF_EXTS:
                pdf_paths.append(path)
            elif ext in (PPTX_EXTS | DOCX_EXTS):
                if self._context.document_conversion_service.office_conversion_available():
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
        if native_playlist_paths:
            self._handlers.import_native_playlists(native_playlist_paths, True)

        if unsupported:
            names = "\n".join(f"  • {os.path.basename(path)}" for path in unsupported)
            QMessageBox.warning(
                self._context.dialog_parent,
                _tr(_UNSUPPORTED_FILE_TITLE),
                _tr(_UNSUPPORTED_FILES_SOURCE).format(files=names),
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
        context = self._context
        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        pages = context.document_conversion_service.cached_pdf_pages(pdf_path)
        if pages:
            self.on_pdf_ready(pages, pdf_stem)
            return

        self._handlers.switch_to_playlist()
        thread = context.document_conversion_service.create_pdf_thread(
            pdf_path,
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
        context = self._context
        stem = Path(jwpub_path).stem
        self._handlers.switch_to_playlist()
        playlist_id = self._handlers.open_named_temp_playlist([], f"📖  {stem}")
        thread = context.jwpub_import_thread_factory.create(
            jwpub_path,
            lang=self._media_language_context().api_code,
            parent=context.dialog_parent,
        )
        context.thread_registry.track(thread)

        @thread.items_ready.connect
        def _on_ready(items: list, file_stem: str) -> None:
            new_items = playlist_items_from_jwpub(items, file_stem)
            if not new_items:
                QMessageBox.information(
                    context.dialog_parent,
                    _tr(_NO_MEDIA_TITLE),
                    _tr(_NO_MEDIA_SOURCE).format(name=file_stem),
                )
                return
            if not self._handlers.append_temp_playlist_items(
                playlist_id,
                new_items,
            ):
                return
            loaded_count = sum(1 for item in new_items if item.get("url"))
            context.notifications.success(
                OpenMediaNotifications.items_added(file_stem, loaded_count)
            )

        @thread.failed.connect
        def _on_fail(error: str) -> None:
            QMessageBox.warning(
                context.dialog_parent,
                _tr(_JWPUB_ERROR_TITLE),
                _tr(_OPEN_ERROR_SOURCE).format(name=stem, error=error),
            )

        thread.start()

    def open_lo_as_temp(self, lo_path: str) -> None:
        context = self._context
        lo_stem = os.path.splitext(os.path.basename(lo_path))[0]
        pages = context.document_conversion_service.cached_office_pages(lo_path)
        if pages:
            self.on_lo_ready(pages, lo_stem)
            return

        self._handlers.switch_to_playlist()
        thread = context.document_conversion_service.create_office_thread(
            lo_path,
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
        context = self._context
        try:
            document = read_jwlplaylist_document(
                path,
                fallback_lang_code=self._media_language_context().fallback_code,
            )
        except BadZipFile:
            QMessageBox.warning(
                context.dialog_parent,
                _tr(_UNSUPPORTED_FILE_TITLE),
                _tr(_INVALID_PLAYLIST_SOURCE).format(name=os.path.basename(path)),
            )
            return []
        except (OSError, ValueError) as error:
            QMessageBox.warning(
                context.dialog_parent,
                _tr(_UNSUPPORTED_FILE_TITLE),
                _tr(_READ_PLAYLIST_ERROR_SOURCE).format(
                    name=os.path.basename(path),
                    error=error,
                ),
            )
            return []

        result = playlist_items_from_jwl_document_items(
            document.items,
            source_name=os.path.basename(path),
            save_embedded=self._save_temp_embedded,
            mark_embedded_tmp=True,
        )

        if result.skipped_titles:
            names = "\n".join(f"  • {title}" for title in result.skipped_titles)
            QMessageBox.warning(
                context.dialog_parent,
                tr_jw_playlist_title(),
                tr_jw_playlist_unresolved(names),
            )
        return [self._temp_playlist_entry(item) for item in result.items]

    def _show_conversion_error(self, error: object) -> None:
        context = self._context
        QMessageBox.warning(
            context.dialog_parent,
            _tr(_PDF_ERROR_TITLE),
            _tr(_PDF_CONVERSION_ERROR_SOURCE).format(error=error),
        )

    @staticmethod
    def _page_items(pages: list, stem: str) -> list[dict]:
        return [
            create_playlist_item(
                title=tr_document_page_title(stem, index + 1),
                url=page_path,
                type="image",
            )
            for index, page_path in enumerate(pages)
        ]

    @staticmethod
    def _temp_playlist_entry(item: dict) -> dict:
        entry = {
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "type": item.get("type", "video"),
        }
        if item.get("_tmp"):
            entry["_tmp"] = True
        return entry

    def _save_temp_embedded(
        self,
        data: bytes,
        filename: str,
        _identifier: str,
        default_suffix: str,
    ) -> str:
        suffix = os.path.splitext(filename)[1].lower() or default_suffix
        return self._write_tmp(data, suffix)

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
