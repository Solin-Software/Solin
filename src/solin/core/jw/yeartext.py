"""yeartext.py -- Solin"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from solin.core.jw.yeartext_content import (
    YeartextFetchError,
    fetch_yeartext,
    parse_yeartext_html,
)
from solin.core.storage.json_files import read_json_file, write_json_atomic

log = logging.getLogger(__name__)


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

        try:
            result = fetch_yeartext(api_code, year)
        except YeartextFetchError as exc:
            self.failed.emit(api_code, year, str(exc))
            return

        self.succeeded.emit(
            result.api_code,
            result.year,
            result.quote,
            result.reference,
        )


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

    def __init__(
        self,
        *,
        cache_file: str | Path,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._cache:   dict = {}
        self._workers: dict[str, _FetchWorker] = {}
        self._cache_path = Path(cache_file)
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            if self._cache_path.is_file():
                self._cache = read_json_file(self._cache_path)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            log.warning("[yeartext] Failed to load cache: %s", exc)
            self._cache = {}

    def _save_cache(self) -> None:
        try:
            write_json_atomic(self._cache_path, self._cache)
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

    def get_cached(self, api_code: str, year: int) -> Optional[tuple[str, str]]:
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
                       year: int) -> Optional[tuple[str, str]]:
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
