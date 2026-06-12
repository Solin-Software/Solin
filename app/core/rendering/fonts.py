"""font_manager.py — Solin

Gerencia o download, conversão e registro de fontes externas no Qt.

Por que converter WOFF2 → TTF?
  Qt (QFontDatabase) só suporta TTF/OTF nativamente.
  WOFF2 é formato web (compressão Brotli) — usado por browsers/Electron,
  mas rejeitado silenciosamente pelo DirectWrite do Windows.
  A biblioteca `fontTools` (+ `brotli`) faz a conversão sem perda.

Fluxo (espelho do helpers/fonts.ts do meeting-media-manager):
  1. Verifica se o .ttf já existe em cache/fonts/
  2. Faz HEAD request para comparar tamanho do .woff2 remoto vs local
  3. Se necessário: baixa o .woff2 → converte → salva como .ttf
  4. Registra no QFontDatabase via addApplicationFont()
  5. Emite font_ready(font_name) quando disponível

Dependências extras:
  pip install fonttools brotli

Uso:
    from app.core.rendering.fonts import font_manager
    font_manager.font_ready.connect(lambda name: widget.update())
    font_manager.ensure('Wt-ClearText-Bold')
    family = font_manager.family('Wt-ClearText-Bold')  # fallback automático
"""
from __future__ import annotations

import io
import logging
import os
import urllib.error as _url_err
import urllib.request as _url_req
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QFontDatabase

from app.core.foundation import paths as _paths
from app.core.network.http import urlopen as _urlopen

log = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────
# _FONTS_CACHE_DIR é resolvido sob demanda para garantir que paths.init()
# já foi chamado antes do primeiro acesso (crítico para Nuitka/.app bundles).
def _fonts_cache_dir() -> str:
    return os.path.join(_paths.CACHE_DIR, "fonts")

_FONT_URLS: dict[str, str] = {
    "Wt-ClearText-Bold": (
        "https://b.jw-cdn.org/fonts/wt-clear-text/1.029/Wt-ClearText-Bold.woff2"
    ),
}

# Fallback por fonte: usado se download ou conversão falharem
_FONT_FALLBACKS: dict[str, str] = {
    "Wt-ClearText-Bold": "Georgia",
}

_DOWNLOAD_TIMEOUT = 30
_HEAD_TIMEOUT     = 5

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ── Conversão WOFF2 → TTF ──────────────────────────────────────────────────────
def _woff2_to_ttf(woff2_data: bytes) -> bytes:
    """
    Converte bytes WOFF2 para TTF puro usando fontTools.
    Requer: pip install fonttools brotli
    """
    try:
        from fontTools.ttLib import TTFont  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "fontTools não instalado. Execute: pip install fonttools brotli"
        ) from exc

    font = TTFont(io.BytesIO(woff2_data))
    font.flavor = None   # remove wrapper WOFF2 → TTF/OTF puro
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


# ── Worker de download + conversão ────────────────────────────────────────────
class _DownloadWorker(QThread):
    """
    Baixa o WOFF2 em background, converte para TTF e salva no cache.
    Emite succeeded com o caminho do .ttf final.
    """

    succeeded = Signal(str, str)   # font_name, local_ttf_path
    failed    = Signal(str, str)   # font_name, error_message

    def __init__(self, font_name: str, url: str, woff2_path: str,
                 ttf_path: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._font_name  = font_name
        self._url        = url
        self._woff2_path = woff2_path
        self._ttf_path   = ttf_path

    def run(self) -> None:
        font_name  = self._font_name
        url        = self._url
        woff2_path = self._woff2_path
        ttf_path   = self._ttf_path

        # 1. Download do WOFF2
        log.debug("[font_manager] Baixando %s", url)
        req = _url_req.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with _urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
                woff2_data = resp.read()
        except (_url_err.URLError, OSError) as exc:
            log.warning("[font_manager] Falha ao baixar '%s': %s", font_name, exc)
            self.failed.emit(font_name, str(exc))
            return

        # 2. Salva .woff2 em cache (referência de tamanho para HEAD futuro)
        try:
            os.makedirs(os.path.dirname(woff2_path), exist_ok=True)
            with open(woff2_path, "wb") as f:
                f.write(woff2_data)
        except OSError as exc:
            log.warning("[font_manager] Não foi possível salvar WOFF2: %s", exc)

        # 3. Converte WOFF2 → TTF
        log.debug("[font_manager] Convertendo WOFF2 → TTF para '%s'", font_name)
        try:
            ttf_data = _woff2_to_ttf(woff2_data)
        except Exception as exc:  # noqa: BLE001 - fontTools conversion boundary
            log.exception("[font_manager] Conversão falhou para '%s'", font_name)
            self.failed.emit(font_name, f"Conversão falhou: {exc}")
            return

        # 4. Salva .ttf em cache
        try:
            with open(ttf_path, "wb") as f:
                f.write(ttf_data)
        except OSError as exc:
            log.warning("[font_manager] Falha ao salvar TTF '%s': %s", font_name, exc)
            self.failed.emit(font_name, str(exc))
            return

        log.info("[font_manager] '%s' pronta: %s (%d bytes TTF)",
                 font_name, ttf_path, len(ttf_data))
        self.succeeded.emit(font_name, ttf_path)


# ── FontManager principal ──────────────────────────────────────────────────────
class FontManager(QObject):
    """
    Singleton que gerencia fontes externas para uso no Qt.

    Signals
    -------
    font_ready(font_name)   — fonte registrada com sucesso no QFontDatabase
    font_failed(font_name)  — falha total; family() retorna o fallback
    """

    font_ready  = Signal(str)
    font_failed = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._registered: dict[str, Optional[str]] = {}
        self._workers: dict[str, _DownloadWorker] = {}

    # ── API pública ───────────────────────────────────────────────────────────

    def ensure(self, font_name: str) -> None:
        """
        Garante que a fonte estará disponível (idempotente).
          - TTF em cache + WOFF2 com tamanho correto → registra imediatamente.
          - Caso contrário → baixa + converte em background.
        """
        if font_name in self._registered:
            return

        url = _FONT_URLS.get(font_name)
        if not url:
            log.warning("[font_manager] Fonte desconhecida: '%s'", font_name)
            return

        woff2_path = self._woff2_cache_path(font_name)
        ttf_path   = self._ttf_cache_path(font_name)

        if os.path.isfile(ttf_path) and self._woff2_size_matches(woff2_path, url):
            self._register(font_name, ttf_path)
        else:
            self._start_download(font_name, url, woff2_path, ttf_path)

    def family(self, font_name: str) -> str:
        """
        Retorna a família a usar no QFont.
        Wt-ClearText-Bold se registrada com sucesso, senão Georgia.
        """
        name = self._registered.get(font_name)
        if name:
            return name
        return _FONT_FALLBACKS.get(font_name, "serif")

    def is_ready(self, font_name: str) -> bool:
        return bool(self._registered.get(font_name))

    # ── Caminhos de cache ─────────────────────────────────────────────────────

    def _woff2_cache_path(self, font_name: str) -> str:
        return os.path.join(_fonts_cache_dir(), f"{font_name}.woff2")

    def _ttf_cache_path(self, font_name: str) -> str:
        return os.path.join(_fonts_cache_dir(), f"{font_name}.ttf")

    # ── Verificação de cache via HEAD ─────────────────────────────────────────

    def _woff2_size_matches(self, woff2_path: str, url: str) -> bool:
        """
        True se o .woff2 local tem o mesmo Content-Length que o remoto.
        Se não há rede, aceita o cache existente.
        """
        if not os.path.isfile(woff2_path) or os.path.getsize(woff2_path) == 0:
            return False

        local_size = os.path.getsize(woff2_path)
        req = _url_req.Request(url, method="HEAD",
                               headers={"User-Agent": _USER_AGENT})
        try:
            with _urlopen(req, timeout=_HEAD_TIMEOUT) as resp:
                cl = resp.headers.get("Content-Length")
                if cl is None:
                    log.debug("[font_manager] HEAD sem Content-Length; aceitando cache.")
                    return True
                match = local_size == int(cl)
                if not match:
                    log.debug(
                        "[font_manager] Tamanho diverge (local=%d remote=%s); "
                        "re-download.", local_size, cl,
                    )
                return match
        except (_url_err.URLError, OSError, ValueError):
            log.debug("[font_manager] HEAD falhou; usando cache existente.")
            return True

    # ── Registro no Qt ────────────────────────────────────────────────────────

    def _register(self, font_name: str, ttf_path: str) -> None:
        font_id = QFontDatabase.addApplicationFont(ttf_path)
        if font_id < 0:
            log.warning(
                "[font_manager] QFontDatabase rejeitou '%s' (%s). "
                "Usando fallback '%s'.",
                font_name, ttf_path, _FONT_FALLBACKS.get(font_name, "serif"),
            )
            self._registered[font_name] = None
            self.font_failed.emit(font_name)
            return

        families = QFontDatabase.applicationFontFamilies(font_id)
        registered_name = families[0] if families else font_name
        self._registered[font_name] = registered_name
        log.info("[font_manager] '%s' registrada como '%s'.", font_name, registered_name)
        self.font_ready.emit(font_name)

    # ── Download em background ────────────────────────────────────────────────

    def _start_download(self, font_name: str, url: str,
                        woff2_path: str, ttf_path: str) -> None:
        if font_name in self._workers:
            return

        log.info("[font_manager] Iniciando download+conversão de '%s'…", font_name)
        worker = _DownloadWorker(font_name, url, woff2_path, ttf_path, parent=None)
        worker.succeeded.connect(self._on_download_success)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(lambda: self._cleanup_worker(font_name))
        self._workers[font_name] = worker
        worker.start()

    def _on_download_success(self, font_name: str, ttf_path: str) -> None:
        self._register(font_name, ttf_path)

    def _on_download_failed(self, font_name: str, message: str) -> None:
        log.warning("[font_manager] Falha para '%s': %s", font_name, message)
        ttf_path = self._ttf_cache_path(font_name)
        if os.path.isfile(ttf_path) and os.path.getsize(ttf_path) > 0:
            log.info("[font_manager] Usando TTF anterior em cache para '%s'.", font_name)
            self._register(font_name, ttf_path)
        else:
            self._registered[font_name] = None
            self.font_failed.emit(font_name)

    def _cleanup_worker(self, font_name: str) -> None:
        worker = self._workers.pop(font_name, None)
        if worker:
            worker.deleteLater()


# ── Instância singleton ────────────────────────────────────────────────────────
font_manager = FontManager()
