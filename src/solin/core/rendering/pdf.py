"""
pdf.py — Solin
==============
Convert PDF pages to JPEG images with a persistent cache.

Cache:
    {pdf_pages_dir}/{sha256_12}_{stem}/page_001.jpg … page_NNN.jpg

Engine:
    PySide6.QtPdf.QPdfDocument, using the application's bundled Qt runtime.

Default DPI: 150 (good projector quality, JPEG ~100–300 KB per page).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, QThread, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtPdf import QPdfDocument

log = logging.getLogger(__name__)

# ── Constantes ─────────────────────────────────────────────────────────────────
_DEFAULT_DPI = 150
_JPEG_QUALITY = 85
_PAGE_FMT = "page_{n:03d}.jpg"  # page_001.jpg, page_002.jpg …


# Utility functions


def _pdf_hash(path: str | Path) -> str:
    """SHA-256 of the PDF contents (first 12 characters), used for the cache name."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()[:12]


def _cache_dir(
    pdf_path: str | os.PathLike[str],
    pdf_pages_dir: str | os.PathLike[str],
) -> Path:
    """
    Return the cache directory for a specific PDF.
    ``pdf_pages_dir`` is supplied by the composition root.
    Example: cache/pdf_pages/a3f8c201ee9d_report
    """
    pdf_path = Path(pdf_path)
    stem = pdf_path.stem[:40]  # limit name length
    h = _pdf_hash(pdf_path)
    return Path(pdf_pages_dir) / f"{h}_{stem}"


def cached_pages(
    pdf_path: str | os.PathLike[str],
    pdf_pages_dir: str | os.PathLike[str],
) -> list[str] | None:
    """
    Return JPEG paths if the PDF was converted and its cache is complete.
    Return None if conversion has not yet been performed.
    """
    d = _cache_dir(pdf_path, pdf_pages_dir)
    marker = d / ".done"
    if not marker.exists():
        return None
    pages = sorted(d.glob("page_*.jpg"), key=lambda p: p.name)
    if not pages:
        return None
    return [str(p) for p in pages]


def _render_size_for_page(doc: QPdfDocument, page_index: int, dpi: int) -> QSize:
    point_size = doc.pagePointSize(page_index)
    width = max(1, int(round(point_size.width() * dpi / 72.0)))
    height = max(1, int(round(point_size.height() * dpi / 72.0)))
    return QSize(width, height)


def _flatten_to_rgb(image: QImage) -> QImage:
    if image.isNull():
        return image
    if not image.hasAlphaChannel() and image.format() == QImage.Format.Format_RGB32:
        return image
    out = QImage(image.size(), QImage.Format.Format_RGB32)
    out.fill(QColor("#ffffff"))
    painter = QPainter(out)
    try:
        painter.drawImage(0, 0, image)
    finally:
        painter.end()
    return out


def render_pdf_pages_sync(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    dpi: int = _DEFAULT_DPI,
    page_name_format: str = _PAGE_FMT,
    page_stem: str | None = None,
    image_format: str = "JPEG",
    quality: int | None = _JPEG_QUALITY,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Render a PDF into image files using QtPdf.

    ``page_name_format`` accepts ``{stem}`` and ``{n}`` placeholders, e.g.
    ``"{stem}-page_{n:03d}.png"`` or ``"page_{n:03d}.jpg"``.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_stem = pdf_path.stem if page_stem is None else page_stem

    doc = QPdfDocument()
    try:
        error = doc.load(str(pdf_path))
        if error != QPdfDocument.Error.None_:
            raise RuntimeError(f"Could not open '{pdf_path.name}' ({error.name}).")

        total = doc.pageCount()
        if total <= 0:
            raise RuntimeError(f"PDF has no pages: '{pdf_path.name}'.")

        paths: list[str] = []
        fmt = image_format.upper() if image_format else None
        save_quality = -1 if quality is None else int(quality)
        for idx in range(total):
            if progress_cb:
                progress_cb(idx + 1, total)

            image = doc.render(idx, _render_size_for_page(doc, idx, dpi))
            image = _flatten_to_rgb(image)
            if image.isNull():
                raise RuntimeError(
                    f"Could not render page {idx + 1} of '{pdf_path.name}'."
                )

            out_path = output_dir / page_name_format.format(stem=output_stem, n=idx + 1)
            if not image.save(str(out_path), fmt, save_quality):
                raise RuntimeError(
                    f"Could not save page {idx + 1} of '{pdf_path.name}'."
                )
            paths.append(str(out_path))
        return paths
    finally:
        doc.close()


def convert_pdf_sync(
    pdf_path: str | os.PathLike[str],
    pdf_pages_dir: str | os.PathLike[str],
    dpi: int = _DEFAULT_DPI,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[str]:
    """
    Convert a PDF to JPEG images synchronously.

    Args:
        pdf_path: PDF file path.
        pdf_pages_dir: Explicit PDF page cache root directory.
        dpi: Rendering resolution (default 150).
        progress_cb: Optional callback (current_page, total_pages).

    Returns:
        Ordered JPEG paths (page_001.jpg …).

    Raises:
        RuntimeError: PDF could not be opened or converted.
    """
    pdf_path = Path(pdf_path)
    cache_dir = _cache_dir(pdf_path, pdf_pages_dir)
    marker = cache_dir / ".done"

    # Cache hit: return immediately.
    if marker.exists():
        pages = sorted(cache_dir.glob("page_*.jpg"), key=lambda p: p.name)
        if pages:
            return [str(p) for p in pages]

    cache_dir.mkdir(parents=True, exist_ok=True)
    for stale_page in cache_dir.glob("page_*.jpg"):
        stale_page.unlink(missing_ok=True)

    try:
        paths = render_pdf_pages_sync(
            pdf_path,
            cache_dir,
            dpi=dpi,
            page_name_format=_PAGE_FMT,
            image_format="JPEG",
            quality=_JPEG_QUALITY,
            progress_cb=progress_cb,
        )

        # Marca cache como completo
        marker.touch()
        return paths

    except Exception as exc:  # noqa: BLE001 - rendering API normalizes Qt/native failures
        # Remove partial cache to avoid corrupted state.
        import shutil

        shutil.rmtree(str(cache_dir), ignore_errors=True)
        raise RuntimeError(f"Could not convert PDF: {exc}") from exc


# Asynchronous thread


class PdfConvertThread(QThread):
    """
    Convert PDF to JPEG in the background.

    Signals:
        progress(current, total) — progress update
        pages_ready(paths, pdf_stem) — conversion completed successfully
        conversion_failed(error_msg) — conversion error
    """

    progress = Signal(int, int)  # (current_page, total)
    pages_ready = Signal(list, str)  # (list[str] paths, pdf_stem)
    conversion_failed = Signal(str)  # mensagem de erro

    def __init__(
        self,
        pdf_path: str | os.PathLike[str],
        pdf_pages_dir: str | os.PathLike[str],
        dpi: int = _DEFAULT_DPI,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._pdf_path = Path(pdf_path)
        self._pdf_pages_dir = Path(pdf_pages_dir)
        self._dpi = dpi

    def run(self) -> None:
        try:
            paths = convert_pdf_sync(
                self._pdf_path,
                self._pdf_pages_dir,
                dpi=self._dpi,
                progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
            )
            stem = Path(self._pdf_path).stem
            self.pages_ready.emit(paths, stem)
        except RuntimeError as exc:
            self.conversion_failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - QThread reports unexpected failures via signal
            log.exception("PDF conversion worker failed")
            self.conversion_failed.emit(f"Erro inesperado: {exc}")


# Orphaned cache cleanup


def flush_pdf_pages_dir(
    referenced_paths: set[str],
    pdf_pages_dir: str | os.PathLike[str],
) -> None:
    """
    Remove subdirectories of data/pdf_pages whose images are not referenced
    by any playlist.

    Args:
        referenced_paths: Image paths present in at least one playlist
            (``item["url"]``).
        pdf_pages_dir: Explicit PDF page cache root directory.
    """
    pages_root = Path(pdf_pages_dir)
    if not pages_root.is_dir():
        return

    norm_ref = {os.path.normpath(p) for p in referenced_paths}

    for sub in pages_root.iterdir():
        if not sub.is_dir():
            continue
        images = list(sub.glob("page_*.jpg"))
        if not images:
            continue
        # Keep the subdirectory if ANY of its images is referenced.
        if any(os.path.normpath(str(img)) in norm_ref for img in images):
            continue
        # Orphaned: remove.
        import shutil

        shutil.rmtree(str(sub), ignore_errors=True)
