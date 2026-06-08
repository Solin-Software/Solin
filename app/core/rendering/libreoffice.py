"""
lo_converter.py — Solin
============================
Converte apresentações e documentos em imagens JPEG via LibreOffice headless
+ pipeline de PDF já existente (QtPdf).

Formatos suportados:
    Apresentações : .pptx  .ppt  .odp
    Documentos    : .docx  .doc  .odt  .rtf

Fluxo (igual para todos os formatos):
    arquivo
        └─► LibreOffice --headless --convert-to pdf   (arquivo .pdf temporário)
            └─► rendering.pdf.render_pdf_pages_sync()  (JPEG por página/slide)

Cache:
    data/pptx_pages/{sha256_12}_{stem}/page_001.jpg  …  (apresentações)
    data/docx_pages/{sha256_12}_{stem}/page_001.jpg  …  (documentos)
    (mesmo esquema do pipeline central de PDF — .done como marker)

Disponibilidade:
    O módulo é sempre importável. A função `libreoffice_available()` verifica
    em runtime se o executável existe. Se não existir, LoConvertThread emite
    conversion_failed com mensagem explicativa — sem travar nem crashar.

Dependências:
    - LibreOffice instalado no sistema (soffice / libreoffice no PATH)
    - PySide6.QtPdf  (já embarcado com o runtime Qt do app)
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

from app.core.foundation import paths as _paths
from app.core.foundation.constants import DOCX_EXTS

log = logging.getLogger(__name__)

# ── Constantes ─────────────────────────────────────────────────────────────────
_DEFAULT_DPI  = 150

# Nomes possíveis do executável do LibreOffice por plataforma
_SOFFICE_CANDIDATES: list[str] = [
    "soffice",
    "libreoffice",
    # macOS (instalação padrão)
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    # Windows (caminhos mais comuns)
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]


# ── Detecção de disponibilidade ────────────────────────────────────────────────

def libreoffice_path() -> str | None:
    """
    Retorna o caminho do executável soffice/libreoffice se encontrado,
    ou None se o LibreOffice não estiver instalado.
    Resultado é cacheado após a primeira chamada.
    """
    if hasattr(libreoffice_path, "_cached"):
        return libreoffice_path._cached  # type: ignore[attr-defined]

    result: str | None = None
    for candidate in _SOFFICE_CANDIDATES:
        found = shutil.which(candidate) or (
            Path(candidate).exists() and candidate or None
        )
        if found:
            result = found
            break

    libreoffice_path._cached = result  # type: ignore[attr-defined]
    return result


def libreoffice_available() -> bool:
    """True se o LibreOffice estiver disponível no sistema."""
    return libreoffice_path() is not None


# ── Funções utilitárias ────────────────────────────────────────────────────────

def _file_hash(path: str | Path) -> str:
    """SHA-256 (primeiros 12 chars) do conteúdo do arquivo."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()[:12]


def _pages_dir_root(path: str | Path) -> str:
    """
    Retorna o diretório raiz de cache adequado ao tipo de arquivo:
      - Apresentações (.pptx/.ppt/.odp) → _paths.PPTX_PAGES_DIR
      - Documentos (.docx/.doc/.odt/.rtf) → _paths.DOCX_PAGES_DIR
    """
    ext = Path(path).suffix.lower()
    if ext in DOCX_EXTS:
        return _paths.DOCX_PAGES_DIR
    return _paths.PPTX_PAGES_DIR  # default para apresentações


def _cache_dir(lo_path: str | Path) -> Path:
    """
    Retorna o diretório de cache para um arquivo específico.
    Exemplos:
        data/pptx_pages/a3f8c201ee9d_apresentacao
        data/docx_pages/b7e1d903cc21_roteiro
    """
    lo_path = Path(lo_path)
    stem    = lo_path.stem[:40]
    h       = _file_hash(lo_path)
    root    = _pages_dir_root(lo_path)
    return Path(root) / f"{h}_{stem}"


def cached_pages(lo_path: str | Path) -> list[str] | None:
    """
    Retorna lista de caminhos JPEG se o arquivo já foi convertido e o cache
    está completo. Retorna None se a conversão ainda não foi feita.
    """
    d      = _cache_dir(lo_path)
    marker = d / ".done"
    if not marker.exists():
        return None
    pages = sorted(d.glob("page_*.jpg"), key=lambda p: p.name)
    if not pages:
        return None
    return [str(p) for p in pages]


def convert_lo_sync(
    lo_path: str | Path,
    dpi: int = _DEFAULT_DPI,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[str]:
    """
    Converte qualquer arquivo suportado pelo LibreOffice em imagens JPEG.
    Funciona tanto para apresentações (.pptx/.ppt/.odp) quanto para
    documentos (.docx/.doc/.odt/.rtf).

    Fluxo:
        1. Verifica cache — retorna imediatamente se já convertido.
        2. Converte para PDF via LibreOffice headless (em diretório temporário).
        3. Usa o renderizador central de PDF para gravar os JPEGs no cache.

    Args:
        lo_path:     Caminho para o arquivo.
        dpi:         Resolução de renderização (padrão 150).
        progress_cb: Callback opcional (página_atual, total_páginas).

    Returns:
        Lista de caminhos JPEG ordenados (page_001.jpg …).

    Raises:
        RuntimeError: Se LibreOffice não estiver disponível ou conversão falhar.
    """
    from .pdf import render_pdf_pages_sync

    soffice = libreoffice_path()
    if not soffice:
        raise RuntimeError(
            "LibreOffice não encontrado. Instale o LibreOffice para abrir "
            "arquivos de apresentação ou documento (.pptx, .ppt, .odp, .docx, .doc, .odt, .rtf)."
        )

    lo_path   = Path(lo_path)
    cache_dir = _cache_dir(lo_path)
    marker    = cache_dir / ".done"

    # Cache hit
    if marker.exists():
        pages = sorted(cache_dir.glob("page_*.jpg"), key=lambda p: p.name)
        if pages:
            return [str(p) for p in pages]

    cache_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="solin_lo_", ignore_cleanup_errors=True) as tmp_dir:
        tmp_path       = Path(tmp_dir)
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
                    "--convert-to", "pdf",
                    "--outdir", tmp_dir,
                    str(lo_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(str(cache_dir), ignore_errors=True)
            raise RuntimeError(
                f"LibreOffice demorou demais ao converter '{lo_path.name}'. "
                "O arquivo pode estar corrompido."
            ) from exc
        except FileNotFoundError as exc:
            shutil.rmtree(str(cache_dir), ignore_errors=True)
            raise RuntimeError(
                f"Executável do LibreOffice não encontrado: {soffice}"
            ) from exc

        if result.returncode != 0:
            shutil.rmtree(str(cache_dir), ignore_errors=True)
            raise RuntimeError(
                f"LibreOffice retornou erro ao converter '{lo_path.name}':\n"
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        pdf_out = Path(tmp_dir) / (lo_path.stem + ".pdf")
        if not pdf_out.exists():
            pdfs = list(Path(tmp_dir).glob("*.pdf"))
            if not pdfs:
                shutil.rmtree(str(cache_dir), ignore_errors=True)
                raise RuntimeError(
                    f"LibreOffice não gerou PDF para '{lo_path.name}'."
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
        except Exception as exc:
            shutil.rmtree(str(cache_dir), ignore_errors=True)
            raise RuntimeError(
                f"Erro ao renderizar páginas de '{lo_path.name}': {exc}"
            ) from exc

    marker.touch()
    return pdf_pages


# ── Thread assíncrona ──────────────────────────────────────────────────────────

class LoConvertThread(QThread):
    """
    Converte qualquer arquivo suportado pelo LibreOffice em JPEG em background.
    Funciona para apresentações (.pptx/.ppt/.odp) e documentos (.docx/.doc/.odt/.rtf).

    Sinais:
        progress(atual, total)         — atualização de progresso
        pages_ready(paths, stem)       — conversão concluída com sucesso
        conversion_failed(error_msg)   — erro ou LibreOffice ausente
    """

    progress          = Signal(int, int)   # (página_atual, total)
    pages_ready       = Signal(list, str)  # (list[str] paths, stem)
    conversion_failed = Signal(str)        # mensagem de erro

    def __init__(self, lo_path: str, dpi: int = _DEFAULT_DPI, parent=None):
        super().__init__(parent)
        self._lo_path = lo_path
        self._dpi     = dpi

    def run(self) -> None:
        try:
            paths = convert_lo_sync(
                self._lo_path,
                dpi=self._dpi,
                progress_cb=lambda cur, tot: self.progress.emit(cur, tot),
            )
            stem = Path(self._lo_path).stem
            self.pages_ready.emit(paths, stem)
        except RuntimeError as exc:
            self.conversion_failed.emit(str(exc))
        except Exception as exc:
            self.conversion_failed.emit(f"Erro inesperado: {exc}")


# ── Limpeza de cache órfão ─────────────────────────────────────────────────────

def _flush_lo_dir(pages_root: Path, referenced_paths: set[str]) -> None:
    """Remove subdiretórios órfãos de um diretório de cache LO."""
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
        try:
            shutil.rmtree(str(sub), ignore_errors=True)
        except Exception:
            log.debug("Failed to remove orphan LibreOffice cache directory %s", sub, exc_info=True)


def flush_pptx_pages_dir(referenced_paths: set[str]) -> None:
    """Remove subdiretórios órfãos de data/pptx_pages."""
    _flush_lo_dir(Path(_paths.PPTX_PAGES_DIR), referenced_paths)


def flush_docx_pages_dir(referenced_paths: set[str]) -> None:
    """Remove subdiretórios órfãos de data/docx_pages."""
    _flush_lo_dir(Path(_paths.DOCX_PAGES_DIR), referenced_paths)


# ── Aliases de retrocompatibilidade ───────────────────────────────────────────
# Mantém compatibilidade com código que ainda importe pelo nome antigo.
# Podem ser removidos quando todas as referências forem atualizadas.

convert_pptx_sync   = convert_lo_sync
PptxConvertThread   = LoConvertThread
