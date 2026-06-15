from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject

from .libreoffice import (
    LoConvertThread,
    cached_pages as cached_office_pages,
    libreoffice_available,
)
from .pdf import PdfConvertThread, cached_pages as cached_pdf_pages


@dataclass(frozen=True, slots=True)
class DocumentConversionService:
    """Binds document conversion workers to application-owned cache roots."""

    pdf_pages_dir: Path
    pptx_pages_dir: Path
    docx_pages_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "pdf_pages_dir", Path(self.pdf_pages_dir))
        object.__setattr__(self, "pptx_pages_dir", Path(self.pptx_pages_dir))
        object.__setattr__(self, "docx_pages_dir", Path(self.docx_pages_dir))

    def office_conversion_available(self) -> bool:
        return libreoffice_available()

    def cached_pdf_pages(
        self,
        path: str | os.PathLike[str],
    ) -> list[str] | None:
        return cached_pdf_pages(path, self.pdf_pages_dir)

    def create_pdf_thread(
        self,
        path: str | os.PathLike[str],
        *,
        parent: QObject | None = None,
    ) -> PdfConvertThread:
        return PdfConvertThread(path, self.pdf_pages_dir, parent=parent)

    def cached_office_pages(
        self,
        path: str | os.PathLike[str],
    ) -> list[str] | None:
        return cached_office_pages(
            path,
            pptx_pages_dir=self.pptx_pages_dir,
            docx_pages_dir=self.docx_pages_dir,
        )

    def create_office_thread(
        self,
        path: str | os.PathLike[str],
        *,
        parent: QObject | None = None,
    ) -> LoConvertThread:
        return LoConvertThread(
            path,
            pptx_pages_dir=self.pptx_pages_dir,
            docx_pages_dir=self.docx_pages_dir,
            pdf_pages_dir=self.pdf_pages_dir,
            parent=parent,
        )
