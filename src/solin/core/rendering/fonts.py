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
    from solin.core.rendering.fonts import FontManager
    font_manager = FontManager(runtime_paths.cache_dir)
    font_manager.font_ready.connect(lambda name: widget.update())
    font_manager.ensure('Wt-ClearText-Bold')
    family = font_manager.family('Wt-ClearText-Bold')  # fallback automático
"""

from __future__ import annotations

import io
import hashlib
import logging
import os
import threading
import urllib.error as _url_err
import urllib.request as _url_req
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QFontDatabase

from solin.core.network.http import urlopen as _urlopen

log = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────
_FONT_URLS: dict[str, str] = {
    "Wt-ClearText-Bold": ("https://b.jw-cdn.org/fonts/wt-clear-text/1.029/Wt-ClearText-Bold.woff2"),
}

# Fallback por fonte: usado se download ou conversão falharem
_FONT_FALLBACKS: dict[str, str] = {
    "Wt-ClearText-Bold": "Georgia",
}

_DOWNLOAD_TIMEOUT = 30

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
        raise RuntimeError("fontTools is not installed. Run: pip install fonttools brotli") from exc

    font = TTFont(io.BytesIO(woff2_data))
    font.flavor = None  # remove wrapper WOFF2 → TTF/OTF puro
    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


# ── Worker de download + conversão ────────────────────────────────────────────
class _DownloadWorker(QThread):
    """
    Baixa o WOFF2 em background, converte para TTF e salva no cache.
    Emite succeeded com o caminho do .ttf final.
    """

    succeeded = Signal(str, str)  # font_name, local_ttf_path
    failed = Signal(str, str)  # font_name, error_message

    def __init__(
        self,
        font_name: str,
        url: str,
        woff2_path: str,
        ttf_path: str,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._font_name = font_name
        self._url = url
        self._woff2_path = woff2_path
        self._ttf_path = ttf_path
        self._response_lock = threading.Lock()
        self._response = None

    def cancel(self) -> None:
        self.requestInterruption()
        with self._response_lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

    def run(self) -> None:
        font_name = self._font_name
        url = self._url
        woff2_path = self._woff2_path
        ttf_path = self._ttf_path

        # 1. Download do WOFF2
        log.debug("[font_manager] Downloading %s", url)
        req = _url_req.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with _urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
                with self._response_lock:
                    self._response = resp
                chunks: list[bytes] = []
                while not self.isInterruptionRequested():
                    chunk = resp.read(131_072)
                    if not chunk:
                        break
                    chunks.append(chunk)
                woff2_data = b"".join(chunks)
        except (_url_err.URLError, OSError) as exc:
            if self.isInterruptionRequested():
                return
            log.warning("[font_manager] Failed to download '%s': %s", font_name, exc)
            self.failed.emit(font_name, str(exc))
            return
        finally:
            with self._response_lock:
                self._response = None

        if self.isInterruptionRequested():
            return

        # 2. Salva .woff2 em cache (referência de tamanho para HEAD futuro)
        try:
            os.makedirs(os.path.dirname(woff2_path), exist_ok=True)
            with open(woff2_path, "wb") as f:
                f.write(woff2_data)
        except OSError as exc:
            log.warning("[font_manager] Could not save WOFF2: %s", exc)

        # 3. Converte WOFF2 → TTF
        log.debug("[font_manager] Converting WOFF2 to TTF for '%s'", font_name)
        try:
            ttf_data = _woff2_to_ttf(woff2_data)
        except Exception as exc:  # noqa: BLE001 - fontTools conversion boundary
            log.exception("[font_manager] Conversion failed for '%s'", font_name)
            self.failed.emit(font_name, f"Conversion failed: {exc}")
            return

        if self.isInterruptionRequested():
            return

        # 4. Salva .ttf em cache
        try:
            with open(ttf_path, "wb") as f:
                f.write(ttf_data)
        except OSError as exc:
            log.warning("[font_manager] Failed to save TTF '%s': %s", font_name, exc)
            self.failed.emit(font_name, str(exc))
            return

        log.info("[font_manager] '%s' ready: %s (%d bytes TTF)", font_name, ttf_path, len(ttf_data))
        self.succeeded.emit(font_name, ttf_path)


# ── FontManager principal ──────────────────────────────────────────────────────
class FontManager(QObject):
    """
    Gerencia fontes externas para uso no Qt com cache explicitamente injetado.

    Signals
    -------
    font_ready(font_name)   — fonte registrada com sucesso no QFontDatabase
    font_failed(font_name)  — falha total; family() retorna o fallback
    """

    font_ready = Signal(str)
    font_failed = Signal(str)

    def __init__(
        self,
        cache_dir: str | os.PathLike[str],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._fonts_cache_dir = Path(cache_dir) / "fonts"
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
            log.warning("[font_manager] Unknown font: '%s'", font_name)
            return

        woff2_path = self._woff2_cache_path(font_name)
        ttf_path = self._ttf_cache_path(font_name)

        if os.path.isfile(ttf_path) and os.path.getsize(ttf_path) > 0:
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

    def _cache_key(self, font_name: str) -> str:
        url = _FONT_URLS.get(font_name, "")
        version = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        return f"{font_name}-{version}"

    def _woff2_cache_path(self, font_name: str) -> str:
        return str(self._fonts_cache_dir / f"{self._cache_key(font_name)}.woff2")

    def _ttf_cache_path(self, font_name: str) -> str:
        return str(self._fonts_cache_dir / f"{self._cache_key(font_name)}.ttf")

    # ── Registro no Qt ────────────────────────────────────────────────────────

    def _register(self, font_name: str, ttf_path: str) -> None:
        font_id = QFontDatabase.addApplicationFont(ttf_path)
        if font_id < 0:
            log.warning(
                "[font_manager] QFontDatabase rejected '%s' (%s). Using fallback '%s'.",
                font_name,
                ttf_path,
                _FONT_FALLBACKS.get(font_name, "serif"),
            )
            self._registered[font_name] = None
            self.font_failed.emit(font_name)
            return

        families = QFontDatabase.applicationFontFamilies(font_id)
        registered_name = families[0] if families else font_name
        self._registered[font_name] = registered_name
        log.info("[font_manager] '%s' registered as '%s'.", font_name, registered_name)
        self.font_ready.emit(font_name)

    # ── Download em background ────────────────────────────────────────────────

    def _start_download(self, font_name: str, url: str, woff2_path: str, ttf_path: str) -> None:
        if font_name in self._workers:
            return

        log.info("[font_manager] Starting download+conversion for '%s'...", font_name)
        worker = _DownloadWorker(font_name, url, woff2_path, ttf_path, parent=self)
        worker.succeeded.connect(self._on_download_success)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(lambda: self._cleanup_worker(font_name))
        self._workers[font_name] = worker
        worker.start()

    def _on_download_success(self, font_name: str, ttf_path: str) -> None:
        self._register(font_name, ttf_path)

    def _on_download_failed(self, font_name: str, message: str) -> None:
        log.warning("[font_manager] Failed for '%s': %s", font_name, message)
        ttf_path = self._ttf_cache_path(font_name)
        if os.path.isfile(ttf_path) and os.path.getsize(ttf_path) > 0:
            log.info("[font_manager] Using previous cached TTF for '%s'.", font_name)
            self._register(font_name, ttf_path)
        else:
            self._registered[font_name] = None
            self.font_failed.emit(font_name)

    def _cleanup_worker(self, font_name: str) -> None:
        worker = self._workers.pop(font_name, None)
        if worker:
            worker.deleteLater()

    def shutdown(self) -> None:
        """Cancel and join every active font worker before application teardown."""
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        for worker in workers:
            worker.wait()
