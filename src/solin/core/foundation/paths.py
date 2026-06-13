"""
paths.py — Solin
================
Fonte única de verdade para todos os caminhos de dados em runtime.

Usa QStandardPaths para garantir que dados, cache e temporários sejam
armazenados nos locais corretos em cada plataforma:

  • macOS   : ~/Library/Application Support/Solin/  e  ~/Library/Caches/Solin/
  • Windows : %APPDATA%\\<org>\\<app>\\              e  %LOCALAPPDATA%\\<org>\\<app>\\cache
  • Linux   : ~/.local/share/Solin/                 e  ~/.cache/Solin/

Isso é especialmente crítico quando compilado com Nuitka (ex: Solin.app no macOS),
onde o bundle .app é somente-leitura e jamais deve receber arquivos de dados.

IMPORTANTE: init() DEVE ser chamado em main.py logo após QApplication ser criada
e app.setOrganizationName() / app.setApplicationName() serem definidos.
Em dev, esses nomes usam SolinDev para isolar dados/cache da producao.
Chamadas subsequentes são no-op.

Uso:
    # Em main.py — logo após criar QApplication:
    from solin.core.foundation import paths
    paths.init()
    paths.ensure_dirs()

    # Em qualquer outro módulo:
    from solin.core.foundation import paths
    cache_file = os.path.join(paths.CACHE_DIR, "myfile.json")
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── Dados persistentes do usuário ─────────────────────────────────────────────
# Sobrevivem a limpezas de cache. Nunca devem ser apagados pelo SO ou pelo user
# sem a consciência de que playlists e imagens serão perdidas.
DATA_DIR:        str = ""   # raiz de dados do usuário
PLAYLISTS_FILE:  str = ""   # data/playlists.json
PENDING_DEL_FILE: str = ""  # data/pending_cleanup.json
IMAGES_DIR:      str = ""   # data/images/
EMBEDDED_DIR:    str = ""   # data/embedded/   (mídia recebida via Wi-Fi)
LOG_DIR:         str = ""   # data/logs/

# ── Cache regenerável ─────────────────────────────────────────────────────────
# Pode ser limpo pelo usuário ou pelo SO sem perda de dados permanentes.
CACHE_DIR:       str = ""   # raiz do cache
MEDIA_CACHE_DIR: str = ""   # cache/media/   (downloads automáticos)
THUMB_CACHE_DIR: str = ""   # cache/thumbs/
MEETING_THUMB_CACHE_DIR: str = ""  # cache/meeting_thumbs/
PDF_PAGES_DIR:   str = ""   # cache/pdf_pages/
PPTX_PAGES_DIR:  str = ""   # cache/pptx_pages/
DOCX_PAGES_DIR:  str = ""   # cache/docx_pages/

# ── Estado interno ────────────────────────────────────────────────────────────
_initialized: bool = False


def init() -> None:
    """
    Resolve e registra todos os caminhos usando QStandardPaths.

    Pré-condição: QCoreApplication já criado, com OrganizationName e
    ApplicationName configurados (feito em main.py antes desta chamada).
    Chamadas subsequentes são no-op seguras.
    """
    global _initialized
    global DATA_DIR, PLAYLISTS_FILE, PENDING_DEL_FILE, IMAGES_DIR, EMBEDDED_DIR
    global LOG_DIR
    global CACHE_DIR, MEDIA_CACHE_DIR, THUMB_CACHE_DIR, MEETING_THUMB_CACHE_DIR
    global PDF_PAGES_DIR, PPTX_PAGES_DIR, DOCX_PAGES_DIR

    if _initialized:
        return

    # Import tardio para evitar importação de Qt antes de QApplication existir.
    from PySide6.QtCore import QStandardPaths
    Loc = QStandardPaths.StandardLocation

    _data  = Path(QStandardPaths.writableLocation(Loc.AppDataLocation))
    _cache = Path(QStandardPaths.writableLocation(Loc.CacheLocation))

    # ── Dados persistentes do usuário ─────────────────────────────────────────
    DATA_DIR         = str(_data)
    PLAYLISTS_FILE   = str(_data / "playlists.json")
    PENDING_DEL_FILE = str(_data / "pending_cleanup.json")
    IMAGES_DIR       = str(_data / "images")
    EMBEDDED_DIR     = str(_data / "embedded")
    LOG_DIR          = str(_data / "logs")

    # ── Cache regenerável ─────────────────────────────────────────────────────
    CACHE_DIR       = str(_cache)
    MEDIA_CACHE_DIR = str(_cache / "media")
    THUMB_CACHE_DIR = str(_cache / "thumbs")
    MEETING_THUMB_CACHE_DIR = str(_cache / "meeting_thumbs")
    PDF_PAGES_DIR   = str(_cache / "pdf_pages")
    PPTX_PAGES_DIR  = str(_cache / "pptx_pages")
    DOCX_PAGES_DIR  = str(_cache / "docx_pages")

    _initialized = True

    log.debug(
        "[paths] Initialized - data=%s  cache=%s",
        _data, _cache,
    )


def ensure_dirs() -> None:
    """
    Cria todos os diretórios de dados e cache, se ainda não existirem.
    Deve ser chamado uma única vez na inicialização, após init().
    """
    if not _initialized:
        raise RuntimeError(
            "paths.ensure_dirs() chamado antes de paths.init(). "
            "Chame paths.init() logo após criar QApplication."
        )

    for directory in (
        DATA_DIR,
        LOG_DIR,
        # IMAGES_DIR / EMBEDDED_DIR são gerenciados pelo ProfileManager
        CACHE_DIR,
        MEDIA_CACHE_DIR,
        THUMB_CACHE_DIR,
        MEETING_THUMB_CACHE_DIR,
        PDF_PAGES_DIR,
        PPTX_PAGES_DIR,
        DOCX_PAGES_DIR,
    ):
        Path(directory).mkdir(parents=True, exist_ok=True)

    log.debug("[paths] Directories verified/created.")
