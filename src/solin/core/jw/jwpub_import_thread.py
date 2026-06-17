"""
jwpub_import_thread.py - Solin
====================================
Qt worker adapter for the .jwpub playlist import service.
"""

from __future__ import annotations

import logging
import os

from solin.core.jw.jwpub_import import (
    JwpubImportRequest,
    JwpubPlaylistImportService,
)

log = logging.getLogger(__name__)


class JwpubImportThread:
    """Factory that returns a QThread subclass for importing a .jwpub file."""

    @staticmethod
    def create(
        jwpub_path: str,
        *,
        lang: str = "T",
        dest_images_dir: str | os.PathLike[str] | None = None,
        service: JwpubPlaylistImportService | None = None,
        parent=None,
    ):
        """Create and return a ready-to-start QThread instance."""
        from PySide6.QtCore import QThread, Signal

        import_service = service or JwpubPlaylistImportService()

        class _Thread(QThread):
            items_ready = Signal(list, str)   # (items, stem)
            failed = Signal(str)              # error_msg

            def __init__(
                self,
                path: str,
                language: str,
                destination: str | None,
                parent,
            ) -> None:
                super().__init__(parent)
                self._path = path
                self._language = language
                self._destination = destination

            def run(self) -> None:
                try:
                    items, stem = import_service.read(
                        JwpubImportRequest(
                            jwpub_path=self._path,
                            language=self._language,
                            dest_images_dir=self._destination,
                            resolve_urls=True,
                        )
                    )
                    self.items_ready.emit(items, stem)
                except Exception as exc:  # noqa: BLE001 - QThread error-delivery boundary
                    log.exception("JWPUB import worker failed")
                    self.failed.emit(str(exc))

        return _Thread(
            jwpub_path,
            lang,
            os.fspath(dest_images_dir) if dest_images_dir is not None else None,
            parent,
        )


class JwpubImportThreadFactory:
    """Create JWPUB import workers for one profile image destination."""

    def __init__(
        self,
        dest_images_dir: str | os.PathLike[str] | None,
        *,
        service: JwpubPlaylistImportService | None = None,
    ) -> None:
        self._dest_images_dir = (
            os.fspath(dest_images_dir) if dest_images_dir is not None else None
        )
        self._service = service or JwpubPlaylistImportService()

    def create(
        self,
        jwpub_path: str,
        *,
        lang: str = "T",
        dest_images_dir: str | os.PathLike[str] | None = None,
        parent=None,
    ):
        return JwpubImportThread.create(
            jwpub_path,
            lang=lang,
            dest_images_dir=(
                os.fspath(dest_images_dir)
                if dest_images_dir is not None
                else self._dest_images_dir
            ),
            service=self._service,
            parent=parent,
        )


__all__ = ["JwpubImportThread", "JwpubImportThreadFactory"]
