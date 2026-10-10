"""
libreoffice.py — Solin
=====================
Convert presentations and documents to JPEG images using headless LibreOffice
and the existing PDF pipeline (QtPdf).

Supported formats:
    Presentations: .pptx .ppt .odp
    Documents: .docx .doc .odt .rtf

Workflow (all formats):
    file
      └─► LibreOffice --headless --convert-to pdf (temporary .pdf)
          └─► rendering.pdf.render_pdf_pages_sync() (JPEG per page/slide)

Cache:
    {pptx_pages_dir}/{sha256_12}_{stem}/page_001.jpg … (presentations)
    {docx_pages_dir}/{sha256_12}_{stem}/page_001.jpg … (documents)
    Same layout as the central PDF pipeline, with .done as a marker.

Availability:
    This module is always importable. `libreoffice_available()` checks for the
    executable at runtime. If unavailable, LoConvertThread emits conversion_failed
    with an explanatory message, without hanging or crashing.

Dependencies:
    - LibreOffice installed (soffice / libreoffice on PATH).
    - PySide6.QtPdf (included in the application's Qt runtime).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal

from solin.core.foundation.constants import DOCX_EXTS

log = logging.getLogger(__name__)

# ── Constantes ─────────────────────────────────────────────────────────────────
_DEFAULT_DPI = 150

# Possible LibreOffice executable names by platform
_SOFFICE_CANDIDATES: list[str] = [
    "soffice",
    "libreoffice",
    # macOS (default installation)
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    # Windows (most common paths)
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


# Availability detection


def libreoffice_path() -> str | None:
    """
    Return the soffice/libreoffice executable path if found,
    or None if LibreOffice is not installed.
    Cache the result after the first call.
    """
    if hasattr(libreoffice_path, "_cached"):
        return libreoffice_path._cached  # type: ignore[attr-defined]

    result: str | None = None
    for candidate in _SOFFICE_CANDIDATES:
        found = shutil.which(candidate) or (Path(candidate).exists() and candidate or None)
        if found:
            result = found
            break

    libreoffice_path._cached = result  # type: ignore[attr-defined]
    return result


def libreoffice_available() -> bool:
    """True if LibreOffice is available on the system."""
    return libreoffice_path() is not None


# Utility functions


def _file_hash(path: str | Path) -> str:
    """SHA-256 of the file contents (first 12 characters)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()[:12]


def _pages_dir_root(
    path: str | os.PathLike[str],
    *,
    pptx_pages_dir: str | os.PathLike[str],
    docx_pages_dir: str | os.PathLike[str],
) -> Path:
    """
    Return the cache root appropriate for the file type:
      - Presentations (.pptx/.ppt/.odp) → ``pptx_pages_dir``.
      - Documents (.docx/.doc/.odt/.rtf) → ``docx_pages_dir``.
    """
    ext = Path(path).suffix.lower()
    if ext in DOCX_EXTS:
        return Path(docx_pages_dir)
    return Path(pptx_pages_dir)  # default for presentations


def _cache_dir(
    lo_path: str | os.PathLike[str],
    *,
    pptx_pages_dir: str | os.PathLike[str],
    docx_pages_dir: str | os.PathLike[str],
) -> Path:
    """
    Return the cache directory for a specific file.
    Examples:
        data/pptx_pages/a3f8c201ee9d_presentation
        data/docx_pages/b7e1d903cc21_outline
    """
    lo_path = Path(lo_path)
    stem = lo_path.stem[:40]
    h = _file_hash(lo_path)
    root = _pages_dir_root(
        lo_path,
        pptx_pages_dir=pptx_pages_dir,
        docx_pages_dir=docx_pages_dir,
    )
    return root / f"{h}_{stem}"


def cached_pages(
    lo_path: str | os.PathLike[str],
    *,
    pptx_pages_dir: str | os.PathLike[str],
    docx_pages_dir: str | os.PathLike[str],
) -> list[str] | None:
    """
    Return JPEG paths if the file was converted and its cache is complete.
    Return None if conversion has not yet been performed.
    """
    d = _cache_dir(
        lo_path,
        pptx_pages_dir=pptx_pages_dir,
        docx_pages_dir=docx_pages_dir,
    )
    marker = d / ".done"
    if not marker.exists():
        return None
    pages = sorted(d.glob("page_*.jpg"), key=lambda p: p.name)
    if not pages:
        return None
    return [str(p) for p in pages]


def convert_lo_sync(
    lo_path: str | os.PathLike[str],
    *,
    pptx_pages_dir: str | os.PathLike[str],
    docx_pages_dir: str | os.PathLike[str],
    pdf_pages_dir: str | os.PathLike[str],
    dpi: int = _DEFAULT_DPI,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[str]:
    """
    Convert any LibreOffice-supported file to JPEG images.
    Support presentations (.pptx/.ppt/.odp) and documents (.docx/.doc/.odt/.rtf).

    Workflow:
      1. Check the cache and return immediately if already converted.
      2. Convert to PDF with headless LibreOffice in a temporary directory.
      3. Use the central PDF renderer to save JPEGs in the cache.

    Args:
        lo_path: Source file path.
        pptx_pages_dir: Explicit presentation cache root directory.
        docx_pages_dir: Explicit document cache root directory.
        pdf_pages_dir: Explicit cache root containing the temporary
            workspace for the intermediate PDF.
        dpi: Rendering resolution (default 150).
        progress_cb: Optional callback (current_page, total_pages).

    Returns:
        Ordered JPEG paths (page_001.jpg …).

    Raises:
        RuntimeError: LibreOffice unavailable or conversion failed.
    """
    from .pdf import render_pdf_pages_sync

    soffice = libreoffice_path()
    if not soffice:
        raise RuntimeError(
            "LibreOffice não encontrado. Instale o LibreOffice para abrir "
            "arquivos de apresentação ou documento (.pptx, .ppt, .odp, .docx, .doc, .odt, .rtf)."
        )

    lo_path = Path(lo_path)
    cache_dir = _cache_dir(
        lo_path,
        pptx_pages_dir=pptx_pages_dir,
        docx_pages_dir=docx_pages_dir,
    )
    marker = cache_dir / ".done"

    # Cache hit
    if marker.exists():
        pages = sorted(cache_dir.glob("page_*.jpg"), key=lambda p: p.name)
        if pages:
            return [str(p) for p in pages]

    cache_dir.mkdir(parents=True, exist_ok=True)

    pdf_pages_root = Path(pdf_pages_dir)
    pdf_pages_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="solin_lo_",
        dir=pdf_pages_root,
        ignore_cleanup_errors=True,
    ) as tmp_dir:
        tmp_path = Path(tmp_dir)
        lo_profile_uri = (tmp_path / "lo_profile").as_uri()
        try:
            result = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    "--nolockcheck",
                    "--nofirststartwizard",
                    f"-env:UserInstallation={lo_profile_uri}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    tmp_dir,
                    str(lo_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(str(cache_dir), ignore_errors=True)
            raise RuntimeError(
                f"LibreOffice timed out while converting '{lo_path.name}'. "
                "The file may be corrupted."
            ) from exc
        except FileNotFoundError as exc:
            shutil.rmtree(str(cache_dir), ignore_errors=True)
            raise RuntimeError(f"LibreOffice executable not found: {soffice}") from exc

        if result.returncode != 0:
            shutil.rmtree(str(cache_dir), ignore_errors=True)
            raise RuntimeError(
                f"LibreOffice failed to convert '{lo_path.name}':\n"
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        pdf_out = Path(tmp_dir) / (lo_path.stem + ".pdf")
        if not pdf_out.exists():
            pdfs = list(Path(tmp_dir).glob("*.pdf"))
            if not pdfs:
                shutil.rmtree(str(cache_dir), ignore_errors=True)
                raise RuntimeError(
                    f"LibreOffice did not generate a PDF for '{lo_path.name}'."
                )
            pdf_out = pdfs[0]

        for stale_page in cache_dir.glob("page_*.jpg"):
            stale_page.unlink(missing_ok=True)

        try:
            pdf_pages = render_pdf_pages_sync(
                pdf_out,
                cache_dir,
                dpi=dpi,
                image_format="JPEG",
                progress_cb=progress_cb,
            )
        except Exception as exc:  # noqa: BLE001 - conversion API normalizes subprocess/Qt failures
            shutil.rmtree(str(cache_dir), ignore_errors=True)
            raise RuntimeError(
                f"Could not render pages from '{lo_path.name}': {exc}"
            ) from exc

    marker.touch()
    return pdf_pages


# Asynchronous thread


class LoConvertThread(QThread):
    """
    Convert LibreOffice-supported files to JPEG in the background.
    Support presentations (.pptx/.ppt/.odp) and documents (.docx/.doc/.odt/.rtf).

    Signals:
        progress(current, total) — progress update
        pages_ready(paths, stem) — conversion completed successfully
        conversion_failed(error_msg) — error or LibreOffice unavailable
    """

    progress = Signal(int, int)  # (current_page, total)
    pages_ready = Signal(list, str)  # (list[str] paths, stem)
    conversion_failed = Signal(str)  # mensagem de erro

    def __init__(
        self,
        lo_path: str | os.PathLike[str],
        *,
        pptx_pages_dir: str | os.PathLike[str],
        docx_pages_dir: str | os.PathLike[str],
        pdf_pages_dir: str | os.PathLike[str],
        dpi: int = _DEFAULT_DPI,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._lo_path = Path(lo_path)
        self._pptx_pages_dir = Path(pptx_pages_dir)
        self._docx_pages_dir = Path(docx_pages_dir)
        self._pdf_pages_dir = Path(pdf_pages_dir)
        self._dpi = dpi

    def run(self) -> None:
        try:
            paths = convert_lo_sync(
                self._lo_path,
                pptx_pages_dir=self._pptx_pages_dir,
                docx_pages_dir=self._docx_pages_dir,
                pdf_pages_dir=self._pdf_pages_dir,
                dpi=self._dpi,
                progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
            )
            stem = Path(self._lo_path).stem
            self.pages_ready.emit(paths, stem)
        except RuntimeError as exc:
            self.conversion_failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - QThread reports unexpected failures via signal
            log.exception("LibreOffice conversion worker failed")
            self.conversion_failed.emit(f"Erro inesperado: {exc}")


# Orphaned cache cleanup


def _flush_lo_dir(pages_root: Path, referenced_paths: set[str]) -> None:
    """Remove orphaned subdirectories from a LibreOffice cache directory."""
    if not pages_root.is_dir():
        return
    norm_ref = {os.path.normpath(p) for p in referenced_paths}
    for sub in pages_root.iterdir():
        if not sub.is_dir():
            continue
        images = list(sub.glob("page_*.jpg"))
        if not images:
            continue
        if any(os.path.normpath(str(img)) in norm_ref for img in images):
            continue
        shutil.rmtree(str(sub), ignore_errors=True)


def flush_pptx_pages_dir(
    referenced_paths: set[str],
    pptx_pages_dir: str | os.PathLike[str],
) -> None:
    """Remove orphaned subdirectories from the explicit presentation cache."""
    _flush_lo_dir(Path(pptx_pages_dir), referenced_paths)


def flush_docx_pages_dir(
    referenced_paths: set[str],
    docx_pages_dir: str | os.PathLike[str],
) -> None:
    """Remove orphaned subdirectories from the explicit document cache."""
    _flush_lo_dir(Path(docx_pages_dir), referenced_paths)
