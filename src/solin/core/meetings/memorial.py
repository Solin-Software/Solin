"""
memorial.py ─ Solin
==============================
Serviço de mídia da Celebração do Memorial JW.

Responsabilidades:
  1. Calcular a data exata do Memorial do ano atual (algoritmo JW moderno)
  2. Determinar a semana em que o Memorial cai
  3. Baixar e extrair o JWPUB mi<YY> com curl_cffi (browser-friendly)
  4. Consultar SQLite: capa (CategoryType=26) + vídeos intro (CategoryType=-1)
  5. Resolver URLs dos vídeos via API GETPUBMEDIALINKS
  6. Armazenar em cache (estrutura idêntica ao JwpubCache)
  7. Emitir sinais para a UI (main thread via QueuedConnection)

Threading:
  _MemorialWorker  — QObject num QThread dedicado (toda I/O bloqueante aqui)
  MemorialService  — QObject na main thread (API pública para a UI)

Política de fetch:
  O worker só busca mídias se a data atual está a ≤ 7 dias do Memorial.
  Fora desse período, emite memorial_not_yet/memorial_past (UI pode ignorar).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

log = logging.getLogger(__name__)

from solin.core.network.http import get_bytes
from solin.core.jw.publication_links import (
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    PUB_MEDIA_URL,
    build_pub_media_url,
    fetch_pub_media_json,
    select_pub_media_file,
)

from .jwpub_cache import JwpubCache, JwpubChecksumStore
from .memorial_calendar import memorial_date_for_year, monday_of
from .memorial_content import (
    extract_memorial_jwpub,
    memorial_publication_symbol,
    read_memorial_publication_content,
)
from . import models as meeting_models


class MemorialDownloadError(RuntimeError):
    """Transport-independent failure while downloading Memorial resources."""


def _chrome_headers() -> dict:
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language":  "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control":    "no-cache",
        "Pragma":           "no-cache",
        "Sec-Ch-Ua": (
            '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        ),
        "Sec-Ch-Ua-Mobile":    "?0",
        "Sec-Ch-Ua-Platform":  '"Windows"',
        "Sec-Fetch-Dest":      "document",
        "Sec-Fetch-Mode":      "navigate",
        "Sec-Fetch-Site":      "none",
        "Sec-Fetch-User":      "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def _http_get_with_browser_impersonation(url: str, timeout: int) -> bytes | None:
    try:
        from curl_cffi import requests  # type: ignore[reportMissingImports]
    except ImportError:
        return None

    response = requests.get(
        url,
        headers=_chrome_headers(),
        timeout=timeout,
        impersonate="chrome124",
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def _http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes:
    """
    GET com TLS browser impersonation via curl_cffi.
    Fallback para o adaptador HTTP central quando curl_cffi não estiver disponível.
    """
    import time
    last: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            content = _http_get_with_browser_impersonation(url, timeout)
            if content is not None:
                return content
            return get_bytes(
                url,
                timeout=timeout,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
        except Exception as exc:  # noqa: BLE001 - curl_cffi/HTTP transport boundary
            last = exc
            if attempt < retries:
                time.sleep(2.0 ** attempt)
    if last is None:
        raise MemorialDownloadError(f"No download attempt was made for {url}")
    raise MemorialDownloadError(f"Could not download {url}: {last}") from last


def _http_get_json(url: str) -> Optional[dict]:
    try:
        data = _http_get(url, timeout=DEFAULT_TIMEOUT)
        return json.loads(data.decode())
    except (
        MemorialDownloadError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        log.warning("GET JSON %s → %s", url, exc)
        return None


# ── JWPUB helpers (worker-thread only) ────────────────────────────────────────

def _get_mi_jwpub_url(pub: str, lang: str) -> tuple[Optional[str], str, str, bool]:
    """
    Busca URL de download + thumbnail quadrado + checksum para o pub mi<YY>.
    Retorna (download_url, thumb_url, checksum, not_found).

    not_found=True  → API respondeu com JSON mas sem arquivos para este pub/lang
                       (publicação genuinamente ausente — equivalente a HTTP 404).
    not_found=False → URL encontrada, ou falha de rede (não conseguimos confirmar).

    Estratégia (production-grade):
      1. API direta via urllib simples — o endpoint é público e não requer
         browser fingerprinting; igual ao que publications.py usa em _get_json.
      2. Fallback com curl_cffi (browser impersonation) caso a chamada simples
         falhe por qualquer razão (bloqueio de rede, TLS restritivo, etc.).
    """
    params = {
        "pub":         pub,
        "issue":       "0",   # mi<YY> não tem número de edição; "0" é o valor correto
        "langwritten": lang,
        "fileformat":  "JWPUB",
        "output":      "json",
        "alllangs":    "0",
        "txtCMSLang":  "E",
    }

    _any_api_response = False   # tracks whether any attempt got a valid JSON reply

    # ── Passo 1: API direta (urllib — sem UA spoofing) ────────────────────────
    data = fetch_pub_media_json(params)
    if data:
        _any_api_response = True
        media_file = select_pub_media_file(
            data,
            lang,
            ("JWPUB",),
            fallback_languages=("E",),
        )
        if media_file is not None:
            log.debug("_get_mi_jwpub_url: URL obtained from direct API for %s/%s", pub, lang)
            return media_file.url, media_file.thumbnail_url, media_file.checksum, False

    # ── Passo 2: Fallback — browser impersonation via curl_cffi ──────────────
    log.warning(
        "_get_mi_jwpub_url: direct API failed for %s/%s - trying browser impersonation",
        pub, lang,
    )
    url = build_pub_media_url(params, PUB_MEDIA_URL)
    data = _http_get_json(url)
    if data:
        _any_api_response = True
        media_file = select_pub_media_file(
            data,
            lang,
            ("JWPUB",),
            fallback_languages=("E",),
        )
        if media_file is not None:
            log.debug("_get_mi_jwpub_url: URL obtained via fallback (curl_cffi) for %s/%s", pub, lang)
            return media_file.url, media_file.thumbnail_url, media_file.checksum, False

    # Both strategies failed to return a URL.
    # If at least one strategy got a valid JSON response the API is reachable,
    # meaning the publication simply doesn't exist → not_found=True.
    not_found = _any_api_response
    log.error("_get_mi_jwpub_url: no strategy returned a URL for %s/%s", pub, lang)
    return None, "", "", not_found


# ── Worker ────────────────────────────────────────────────────────────────────

class _MemorialWorker(QObject):
    """
    Roda num QThread dedicado.
    Toda I/O bloqueante (HTTP, zip, SQLite, resolução de vídeo) acontece aqui.
    Comunica com MemorialService via sinais (QueuedConnection automática).
    """
    memorial_done  = Signal(object)   # meeting_models.MemorialData
    progress       = Signal(int)      # 0-100
    error          = Signal(str)      # mensagem

    def __init__(
        self,
        jwpub_cache_dir: str | Path,
        checksum_store: JwpubChecksumStore,
        parent=None,
    ):
        super().__init__(parent)
        self._cache           = JwpubCache(jwpub_cache_dir)
        self._checksum_store  = checksum_store
        self._lang            = "T"

    @Slot(str)
    def set_lang(self, lang: str):
        self._lang = lang

    @Slot(int)
    def load_memorial(self, year: int):
        """Busca e resolve as mídias do Memorial para o ano dado."""
        lang  = self._lang
        pub   = memorial_publication_symbol(year)
        issue = "0"   # mi<YY> não tem issue numérico — usa "0" como chave de cache

        md = meeting_models.MemorialData(year=year)

        # ── Calcular data ──────────────────────────────────────────────────────
        memorial_date = memorial_date_for_year(year)
        if not memorial_date:
            md.status = "error"
            self.error.emit(f"Could not calculate the Memorial date for {year}")
            self.memorial_done.emit(md)
            return

        md.memorial_date = memorial_date
        md.memorial_week = monday_of(memorial_date)

        # ── Verificar janela de fetch ──────────────────────────────────────────
        today      = date.today()
        days_until = (memorial_date - today).days
        is_past    = days_until < -1   # more than 1 day after the memorial

        if days_until > 7:
            md.status = "not_yet"
            self.memorial_done.emit(md)
            return

        # NOTE: we intentionally do NOT bail out early for is_past here.
        # If we have a cached copy it should still be shown; and if not,
        # we need to reach the API to decide whether to show "not_found"
        # or "deleted" (media removed by JW).

        self.progress.emit(5)

        # ── Obter URL + checksum do Memorial (sempre chama API para detectar updates) ──
        dl_url, thumb_url, checksum, not_found = _get_mi_jwpub_url(pub, lang)
        md.thumb_url = thumb_url

        is_cached = self._cache.is_cached(pub, lang, issue)

        needs_download = (
            not is_cached
            or self._checksum_store.has_changed(pub, lang, issue, checksum)
        )

        if needs_download:
            if not dl_url:
                if is_cached:
                    # API unreachable but we have a local copy → use stale cache.
                    log.warning(
                        "memorial %s/%s: API unreachable, falling back to stale cache",
                        pub, lang,
                    )
                    self.progress.emit(60)
                else:
                    # No URL AND no local copy — determine the right user message.
                    if is_past and not_found:
                        # API confirmed pub absent + date already passed
                        # → JW has removed the media from their servers.
                        md.status = "deleted"
                    elif not_found:
                        # API confirmed pub absent but date is still upcoming/current
                        # → publication not yet available; user can retry later.
                        md.status = "not_found"
                    else:
                        # Pure network error — we can't tell whether the pub exists.
                        if is_past:
                            md.status = "deleted"  # assume deleted if past and we can't confirm
                        else:
                            md.status = "error"
                            self.error.emit(f"URL not found for {pub} lang={lang}")
                    self.memorial_done.emit(md)
                    return
            else:
                dest = self._cache.jwpub_path(pub, lang, issue)
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    raw = _http_get(dl_url, timeout=120)
                    self.progress.emit(60)
                    dest.write_bytes(raw)
                    # New .jwpub on disk — wipe the stale extract dir so that
                    # extract_memorial_jwpub() below unpacks the fresh content.
                    self._cache.invalidate_extract(pub, lang, issue)
                    self._checksum_store.save(pub, lang, issue, checksum)
                except (MemorialDownloadError, OSError) as exc:
                    if is_cached:
                        # Download failed but stale cache exists → use it.
                        log.warning(
                            "memorial %s/%s: download failed, using stale cache: %s",
                            pub, lang, exc,
                        )
                        self.progress.emit(60)
                    else:
                        md.status = "error"
                        self.error.emit(f"Download failed: {exc}")
                        self.memorial_done.emit(md)
                        return
        else:
            self.progress.emit(60)

        self.progress.emit(70)

        # ── Extração ───────────────────────────────────────────────────────────
        pub_dir = extract_memorial_jwpub(pub, lang, issue, self._cache)
        if not pub_dir:
            md.status = "error"
            self.error.emit("JWPUB extraction failed")
            self.memorial_done.emit(md)
            return

        md.pub_dir = pub_dir
        self.progress.emit(80)

        # ── Consulta SQLite e construção dos itens ─────────────────────────────
        content = read_memorial_publication_content(pub_dir)
        md.cover_bytes = content.cover_bytes
        self.progress.emit(85)

        md.videos = content.media_items
        md.status = "ready" if (content.media_items or md.cover_bytes) else "empty"
        self.progress.emit(100)
        self.memorial_done.emit(md)


# ── MemorialService — vive na main thread ────────────────────────────────────

class MemorialService(QObject):
    """
    API pública para a UI.  Vive na main thread.
    Expõe sinais para o widget consumir resultados do worker.

    Uso típico:
        svc = MemorialService(self)
        svc.set_lang("T")
        svc.memorial_ready.connect(self._on_memorial)
        svc.load()                           # dispara worker se estiver na janela
        monday = svc.memorial_week()         # para saber em qual semana mostrar
    """

    memorial_ready   = Signal(object)     # meeting_models.MemorialData (status="ready")
    memorial_status  = Signal(str)        # status string (p/ UI genérica)
    memorial_progress = Signal(int)       # 0-100

    _sig_load     = Signal(int)
    _sig_set_lang = Signal(str)

    def __init__(
        self,
        jwpub_cache_dir: str | Path,
        checksum_store: JwpubChecksumStore,
        parent=None,
    ):
        super().__init__(parent)
        self._lang   = "T"
        self._year   = date.today().year
        self._data:  Optional[meeting_models.MemorialData] = None

        self._thread = QThread(self)
        self._worker = _MemorialWorker(jwpub_cache_dir, checksum_store)
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)

        self._worker.memorial_done.connect(self._on_done)
        self._worker.progress.connect(self.memorial_progress)
        self._worker.error.connect(self._on_error)

        self._sig_load.connect(self._worker.load_memorial)
        self._sig_set_lang.connect(self._worker.set_lang)

        self._thread.start()

    def shutdown(self, wait_ms: int = 3000, delete_when_stopped: bool = False) -> None:
        """Encerra explicitamente a worker thread do Memorial."""
        thread: QThread | None = getattr(self, "_thread", None)
        if thread is None:
            return
        try:
            running = thread.isRunning()
        except RuntimeError:
            self._thread = None
            return

        if running:
            thread.quit()
            thread.wait(wait_ms)
            try:
                still_running = thread.isRunning()
            except RuntimeError:
                self._thread = None
                return
            if still_running and delete_when_stopped:
                self.setParent(None)
                thread.finished.connect(self.deleteLater)

    def __del__(self):
        try:
            self.shutdown(wait_ms=0)
        except Exception:  # noqa: BLE001 - destructors must never raise during shutdown
            log.debug("Failed to shutdown MemorialService during finalization", exc_info=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_lang(self, lang: str):
        """Define o idioma de mídia JW (não o idioma da interface)."""
        if lang == self._lang:
            return
        self._lang = lang
        self._data  = None
        self._sig_set_lang.emit(lang)

    def get_lang(self) -> str:
        return self._lang

    def load(self, force: bool = False):
        """
        Dispara o worker para carregar/verificar as mídias do Memorial.
        Se force=False e já temos dados prontos, emite o sinal imediatamente.
        """
        if not force and self._data and self._data.status == "ready":
            self.memorial_ready.emit(self._data)
            return
        self._sig_load.emit(self._year)

    def get_data(self) -> Optional[meeting_models.MemorialData]:
        return self._data

    def memorial_date(self) -> Optional[date]:
        """Data calculada do Memorial (pode ser None se ainda não calculada)."""
        if self._data:
            return self._data.memorial_date
        # Cálculo síncrono leve para uso imediato pela UI (sem bloquear)
        return memorial_date_for_year(self._year)

    def memorial_week(self) -> Optional[date]:
        """Segunda-feira da semana do Memorial."""
        d = self.memorial_date()
        return monday_of(d) if d else None

    def is_memorial_week(self, monday: date) -> bool:
        """Verdadeiro se a semana dada é a semana do Memorial."""
        mw = self.memorial_week()
        return mw is not None and mw == monday

    # ── Slots ─────────────────────────────────────────────────────────────────

    @Slot(object)
    def _on_done(self, data: meeting_models.MemorialData):
        self._data = data
        self.memorial_status.emit(data.status)
        if data.status == "ready":
            self.memorial_ready.emit(data)

    @Slot(str)
    def _on_error(self, msg: str):
        log.error("MemorialService: %s", msg)
