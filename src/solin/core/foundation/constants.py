"""
constants.py — Solin
========================
Fonte única de verdade para constantes estáticas compartilhadas do projeto.
Importe daqui em vez de redefinir em cada módulo.

ATENÇÃO — caminhos de dados/cache:
    Caminhos de runtime pertencem a RuntimePaths/ProfilePaths e devem ser
    injetados pelo bootstrap. Constantes neste módulo devem permanecer puras.

Uso:
    from solin.core.foundation.constants import APP_VERSION, QSETTINGS_ORG_NAME, ...
"""

from __future__ import annotations

import sys

from solin.version import VERSION

IS_DEV: bool = "__compiled__" not in globals()

# ── Identidade Qt / QSettings ─────────────────────────────────────────────────
# Em desenvolvimento, usa outro namespace para nao misturar registros, dados
# em QStandardPaths, cache e perfis com a instalacao de producao.
DISPLAY_APP_NAME: str = "Solin"
QT_ORGANIZATION_NAME: str = "SolinDev" if IS_DEV else "Solin"
QT_APPLICATION_NAME: str = "SolinDev" if IS_DEV else "Solin"

# QSettings usa (organization, application). Mantemos os nomes de "application"
# internos estaveis, e isolamos dev/prod pelo organization.
QSETTINGS_ORG_NAME: str = QT_ORGANIZATION_NAME
QSETTINGS_PROFILE_ORG_PREFIX: str = f"{QSETTINGS_ORG_NAME}_"
QSETTINGS_PREFS_APP: str = "ProjectionPrefs"
QSETTINGS_APP_APP: str = "App"
QSETTINGS_GLOBAL_APP: str = "GlobalApp"
QSETTINGS_MAIN_WINDOW_GEOMETRY_APP: str = "MainWindowGeometry"
QSETTINGS_TIMER_APP: str = "Timer"
QSETTINGS_MONITORS_APP: str = "Monitors"
QSETTINGS_NOTIFICATIONS_APP: str = "Notifications"
QSETTINGS_PROFILE_SCOPED_APPS: tuple[str, ...] = (
    QSETTINGS_PREFS_APP,
    QSETTINGS_APP_APP,
    QSETTINGS_MAIN_WINDOW_GEOMETRY_APP,
    QSETTINGS_TIMER_APP,
    QSETTINGS_MONITORS_APP,
    QSETTINGS_NOTIFICATIONS_APP,
)

# ── Versão do aplicativo ────────────────────────────────────────────────────────
# Formato: YY.RELEASE.PATCH.REVISION.
APP_VERSION: str = VERSION

# Identificador estável enviado às APIs do Solin. Não use sys.platform cru no
# backend: "win32"/"darwin" são nomes Python, não nomes de produto.
if sys.platform == "win32":
    APP_PLATFORM: str = "windows"
elif sys.platform == "darwin":
    APP_PLATFORM: str = "macos"
elif sys.platform.startswith("linux"):
    APP_PLATFORM: str = "linux"
else:
    APP_PLATFORM: str = sys.platform

# ── Arquivos temporários de stream (cache OFF) ─────────────────────────────────
# Prefixo obrigatório em todo tempfile e lockfile gerado pelo Solin.
# A limpeza de órfãos na inicialização filtra EXCLUSIVAMENTE por este prefixo,
# evitando interferência em lockfiles de outros aplicativos.
TEMP_STREAM_PREFIX: str = "Solin_stream_"

# ── IPC ────────────────────────────────────────────────────────────────────────
IPC_SERVER_NAME: str = "SolinDev_IPC_v1" if IS_DEV else "Solin_IPC_v1"
IPC_TIMEOUT_MS: int = 800

# ── Notificações remotas ────────────────────────────────────────────────────────
# URL do endpoint JSON de notificações. Substitua pelo seu servidor em produção.
if IS_DEV:
    NOTIFICATION_API_URL: str = "http://localhost:5000/v1/notifications"
else:
    NOTIFICATION_API_URL: str = "https://solinav.vercel.app/v1/notifications"
# Delay (ms) após a janela principal ser exibida antes de checar notificações.
# Garante que a UI já está visível e responsiva ao usuário antes do fetch.
NOTIFICATION_CHECK_DELAY_MS: int = 1500

# ── Ordem de reprodução ────────────────────────────────────────────────────────
ORDER_OFF: str = "off"
ORDER_NEXT: str = "next"
ORDER_RANDOM: str = "random"

# ── Recursos de projeção ────────────────────────────────────────────────────────
# Se True, permite zoom/pan interativo ao projetar uma aba ao vivo do browser.
# Se False (padrão), o recurso é desabilitado para projeções ao vivo.
ALLOW_ZOOM_PAN_ON_LIVE_TAB: bool = False

# ── OBS Studio — memorização de cena ────────────────────────────────────────────
# Se True (padrão), ao trocar para a cena de mídia o Solin memoriza a cena anterior
# e volta a ela quando a mídia terminar. Se False, sempre retorna à cena idle
# configurada em Settings, nunca memorizando a anterior.
MEMORIZE_PRE_MEDIA_SCENE: bool = True

JWL_PLAYLIST_EXTS: frozenset[str] = frozenset({".jwlplaylist"})
SOLIN_PLAYLIST_EXTS: frozenset[str] = frozenset({".solinplaylist"})
# Legacy dispatch remains JWL-only. Native packages are accepted exclusively by
# their dedicated top-level import/open flow and must not enter flat JWL ingest.
PLAYLIST_EXTS: frozenset[str] = JWL_PLAYLIST_EXTS
PDF_EXTS: frozenset[str] = frozenset({".pdf"})
JWPUB_EXTS: frozenset[str] = frozenset({".jwpub"})
PPTX_EXTS: frozenset[str] = frozenset({".pptx", ".ppt", ".odp"})
DOCX_EXTS: frozenset[str] = frozenset({".docx", ".doc", ".odt", ".rtf"})

# ── Qualidade de imagem ────────────────────────────────────────────────────────
THUMB_JPEG_QUALITY: int = 85  # 0-100; usado em todos os saves de thumbnail

# ── Cache de API ───────────────────────────────────────────────────────────────
CACHE_TTL_DAYS: int = 10

# ── Qualidade de vídeo JW (GETPUBMEDIALINKS) ───────────────────────────────────
# Resolução preferida para todos os vídeos buscados da API JW.org.
# Usada em jw/media_api.py (cânticos sjjm/sjj e clipes osg).
VIDEO_PREFERRED_QUALITY: str = "720p"

# Direção do fallback quando a resolução preferida não estiver disponível:
#   "below" → tenta resoluções menores primeiro, depois maiores (padrão conservador)
#   "above" → tenta resoluções maiores primeiro, depois menores
VIDEO_QUALITY_FALLBACK_DIR: str = "below"

# Ordem canônica de qualidades conhecidas, da mais alta para a mais baixa.
# Usada por pick_quality() em jw/media_api.py.
VIDEO_QUALITY_ORDER: tuple[str, ...] = (
    "1080p",
    "720p",
    "480p",
    "360p",
    "240p",
    "180p",
)

# ── Endpoint de verificação de atualização ──────────────────────────────────────
# GET → JSON:
# {
#   "setup": {"version": "X.Y.Z.W", "url": "https://solinav.com/v1/download/win/setup?v=...&id=..."},
#   "patch": {"version": "X.Y.Z.W", "url": "https://solinav.com/v1/download/win/patch?v=...&id=...",
#             "min_version": "X.Y.Z.W"}
# }
#
# O worker envia: ?v=<APP_VERSION>&id=<install_id>&platform=windows
#   v        → versão atual do app (para upsert AppInstance no servidor)
#   id       → UUID anônimo de install_id.py (para métricas, não identifica pessoas)
#   platform → plataforma (para expansão futura a macOS/Linux)
#
# O servidor embute from_version + install_id nas URLs de download retornadas,
# de forma que o clique no download registra métricas completas automaticamente.
if IS_DEV:
    UPDATE_CHECK_URL: str = "http://localhost:5000/v1/version"
else:
    UPDATE_CHECK_URL: str = "https://solinav.vercel.app/v1/version"

# Delay (ms) após a janela principal para checar atualizações (após notificações).
UPDATE_CHECK_DELAY_MS: int = 5000
