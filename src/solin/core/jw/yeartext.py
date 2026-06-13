"""yeartext.py -- Solin"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error as _url_err
import urllib.parse as _url_parse
import urllib.request as _url_req
from datetime import datetime
from typing import Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from solin.core.foundation import paths as _paths
from solin.core.network.http import urlopen as _urlopen

log = logging.getLogger(__name__)

_WOL_API_URL    = "https://wol.jw.org/wol/finder"
_API_TIMEOUT    = 12
_CACHE_FILENAME = "yeartext_cache.json"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


def parse_yeartext_html(html: str) -> Tuple[str, str]:
    """
    Parseia o HTML multi-paragrafo retornado pela API wol.jw.org.

    Estrutura da resposta:
      - p1..pN-1 : partes da citacao (texto da Escritura)
      - pN       : referencia biblica -- mantida EXATAMENTE como recebida
                   (ex: "-- Mateus 5:3." em pt, "(Matteo 5:3)" em it, etc.)
      - pN+1     : paragrafo vazio com class="sb" -- ignorado

    A referencia e preservada sem modificacoes para que cada idioma
    exiba o separador que a API ja envia (dash, parenteses, etc.).

    Retorna (quote, reference). Nunca lanca excecao.
    """
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL | re.IGNORECASE)
    texts = [_strip_tags(p) for p in paras]
    texts = [t for t in texts if t]   # remove paragrafos vazios

    if not texts:
        return _strip_tags(html), ""
    if len(texts) == 1:
        return texts[0], ""

    # Ultimo paragrafo nao-vazio = referencia (exatamente como recebida)
    ref   = texts[-1]
    # Une as partes da citacao com \n, preservando as quebras de linha
    # que a propria API define via paragrafos <p> separados
    quote = "\n".join(texts[:-1])
    return quote, ref


class _FetchWorker(QThread):
    succeeded = Signal(str, int, str, str)  # api_code, year, quote, reference
    failed    = Signal(str, int, str)        # api_code, year, message

    def __init__(self, api_code: str, year: int,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._api_code = api_code
        self._year     = year

    def run(self) -> None:
        api_code = self._api_code
        year     = self._year

        params = {
            "docid":    f"110{year}800",
            "format":   "json",
            "snip":     "yes",
            "wtlocale": api_code,
        }
        url = f"{_WOL_API_URL}?{_url_parse.urlencode(params)}"
        log.debug("[yeartext] GET %s", url)

        req = _url_req.Request(url, headers={
            "User-Agent":       _USER_AGENT,
            "Accept":           "application/json, text/javascript, */*; q=0.01",
            "Accept-Language":  "pt-BR,pt;q=0.9,en;q=0.8",
            "Referer":          "https://wol.jw.org/",
            "X-Requested-With": "XMLHttpRequest",
        })

        try:
            with _urlopen(req, timeout=_API_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
        except (_url_err.URLError, OSError) as exc:
            log.warning("[yeartext] Request failed (%s/%d): %s", api_code, year, exc)
            self.failed.emit(api_code, year, str(exc))
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.failed.emit(api_code, year, f"JSON invalido: {exc}")
            return

        log.debug("[yeartext] JSON response (%s/%d): %s", api_code, year, data)

        if not data.get("exists", False):
            self.failed.emit(api_code, year,
                             f"Texto {year} nao encontrado para '{api_code}'")
            return

        content = data.get("content", "")
        if not content:
            self.failed.emit(api_code, year, "Conteudo vazio na resposta da API")
            return

        quote, reference = parse_yeartext_html(content)
        if not quote:
            self.failed.emit(api_code, year,
                             "Nao foi possivel extrair o texto da resposta")
            return

        log.info("[yeartext] OK (%s/%d): %s... / ref: %s",
                 api_code, year, quote[:60], reference)
        self.succeeded.emit(api_code, year, quote, reference)


class YeartextService(QObject):
    """
    Servico de Texto Anual com fetch automatico e cache por idioma/ano.

    Signals
    -------
    fetched(api_code, year, quote, reference)
    fetch_failed(api_code, year, message)
    fetch_started(api_code, year)
    """

    fetched       = Signal(str, int, str, str)
    fetch_failed  = Signal(str, int, str)
    fetch_started = Signal(str, int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cache:   dict = {}
        self._workers: dict[str, _FetchWorker] = {}
        self._cache_path = os.path.join(_paths.CACHE_DIR, _CACHE_FILENAME)
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            if os.path.isfile(self._cache_path):
                with open(self._cache_path, encoding="utf-8") as f:
                    self._cache = json.load(f)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            log.warning("[yeartext] Failed to load cache: %s", exc)
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            os.makedirs(_paths.CACHE_DIR, exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("[yeartext] Failed to save cache: %s", exc)

    def _update_cache(self, api_code: str, year: int,
                      quote: str, reference: str) -> None:
        self._cache[api_code] = {
            "year":      year,
            "quote":     quote,
            "reference": reference,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_cache()

    def get_cached(self, api_code: str, year: int) -> Optional[Tuple[str, str]]:
        """
        Retorna (quote, reference) se o cache for valido para api_code + ano.
        Retorna None se ausente, desatualizado ou vazio.
        Migra automaticamente entradas no formato legado (full_text).
        """
        entry = self._cache.get(api_code)
        if not entry or entry.get("year") != year:
            return None

        # Formato atual: quote + reference separados
        if entry.get("quote"):
            return entry["quote"], entry.get("reference", "")

        # Formato legado (full_text) -- migra silenciosamente
        full_text = entry.get("full_text", "")
        if full_text:
            quote, ref = parse_yeartext_html(full_text)
            if quote:
                self._update_cache(api_code, year, quote, ref)
                return quote, ref

        return None

    def is_fetching(self, api_code: str) -> bool:
        w = self._workers.get(api_code)
        return w is not None and w.isRunning()

    def fetch_async(self, api_code: str, year: int) -> None:
        """Dispara fetch em background. Idempotente."""
        if self.is_fetching(api_code):
            return
        worker = _FetchWorker(api_code, year, parent=None)
        worker.succeeded.connect(self._on_worker_success)
        worker.failed.connect(self._on_worker_failure)
        worker.finished.connect(lambda: self._cleanup_worker(api_code))
        self._workers[api_code] = worker
        self.fetch_started.emit(api_code, year)
        worker.start()

    def ensure_current(self, api_code: str,
                       year: int) -> Optional[Tuple[str, str]]:
        cached = self.get_cached(api_code, year)
        if cached:
            return cached
        self.fetch_async(api_code, year)
        return None

    def override_cache(self, api_code: str, year: int,
                       quote: str, reference: str) -> None:
        """Sobrescreve o cache com texto editado manualmente."""
        self._update_cache(api_code, year, quote, reference)

    def _on_worker_success(self, api_code: str, year: int,
                           quote: str, reference: str) -> None:
        self._update_cache(api_code, year, quote, reference)
        self.fetched.emit(api_code, year, quote, reference)

    def _on_worker_failure(self, api_code: str, year: int,
                           message: str) -> None:
        self.fetch_failed.emit(api_code, year, message)

    def _cleanup_worker(self, api_code: str) -> None:
        worker = self._workers.pop(api_code, None)
        if worker:
            worker.deleteLater()
