"""yeartext.py -- Solin"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot

from solin.core.foundation.thread_workers import ThreadedWorkerPool
from solin.core.jw.yeartext_content import (
    YeartextFetchError,
    fetch_yeartext,
    parse_yeartext_html,
)
from solin.core.storage.json_files import read_json_file, write_json_atomic

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _FetchCompletion:
    api_code: str
    year: int
    generation: int
    quote: str = ""
    reference: str = ""
    error: str = ""


class YeartextService(QObject):
    """
    Yeartext service with automatic fetching and caching by language/year.

    Signals
    -------
    fetched(api_code, year, quote, reference)
    fetch_failed(api_code, year, message)
    fetch_started(api_code, year)
    """

    fetched = Signal(str, int, str, str)
    fetch_failed = Signal(str, int, str)
    fetch_started = Signal(str, int)
    _fetch_completed = Signal(object)

    def __init__(
        self,
        *,
        cache_file: str | Path,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._cache: dict = {}
        self._fetches: dict[str, int] = {}
        self._next_generation = 0
        self._workers = ThreadedWorkerPool()
        self._closed = False
        self._cache_path = Path(cache_file)
        self._fetch_completed.connect(self._consume_fetch_completion)
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

    def _update_cache(self, api_code: str, year: int, quote: str, reference: str) -> None:
        self._cache[api_code] = {
            "year": year,
            "quote": quote,
            "reference": reference,
            "cached_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_cache()

    def get_cached(self, api_code: str, year: int) -> Optional[tuple[str, str]]:
        """
        Return (quote, reference) if the cache is valid for api_code + year.
        Return None if missing, outdated, or empty.
        Automatically migrate legacy entries (full_text).
        """
        entry = self._cache.get(api_code)
        if not entry or entry.get("year") != year:
            return None

        # Current format: separate quote and reference.
        if entry.get("quote"):
            return entry["quote"], entry.get("reference", "")

        # Legacy format (full_text); migrate silently.
        full_text = entry.get("full_text", "")
        if full_text:
            quote, ref = parse_yeartext_html(full_text)
            if quote:
                self._update_cache(api_code, year, quote, ref)
                return quote, ref

        return None

    def is_fetching(self, api_code: str) -> bool:
        return api_code in self._fetches

    def fetch_async(self, api_code: str, year: int) -> None:
        """Start an idempotent background fetch."""
        if self._closed or self.is_fetching(api_code):
            return
        self._next_generation += 1
        generation = self._next_generation
        self._fetches[api_code] = generation
        self.fetch_started.emit(api_code, year)

        def fetch() -> None:
            try:
                result = fetch_yeartext(api_code, year)
                completion = _FetchCompletion(
                    api_code=result.api_code,
                    year=result.year,
                    generation=generation,
                    quote=result.quote,
                    reference=result.reference,
                )
            except YeartextFetchError as exc:
                completion = _FetchCompletion(api_code, year, generation, error=str(exc))
            if self._closed:
                return
            try:
                self._fetch_completed.emit(completion)
            except RuntimeError:
                # The application may finish while the bounded HTTP request is
                # still returning. No Qt object may be touched after teardown.
                return

        if self._workers.submit(f"yeartext-{api_code}", fetch) is None:
            self._fetches.pop(api_code, None)

    def ensure_current(self, api_code: str, year: int) -> Optional[tuple[str, str]]:
        cached = self.get_cached(api_code, year)
        if cached:
            return cached
        self.fetch_async(api_code, year)
        return None

    def override_cache(self, api_code: str, year: int, quote: str, reference: str) -> None:
        """Overwrite the cache with manually edited text."""
        # The worker may finish after a manual save. Invalidate its generation
        # before it can update the cache or emit an obsolete success/error.
        self._fetches.pop(api_code, None)
        self._update_cache(api_code, year, quote, reference)

    @Slot(object)
    def _consume_fetch_completion(self, value: object) -> None:
        if not isinstance(value, _FetchCompletion):
            return
        if self._closed or self._fetches.get(value.api_code) != value.generation:
            return
        self._fetches.pop(value.api_code)
        if value.error:
            self.fetch_failed.emit(value.api_code, value.year, value.error)
            return
        self._update_cache(
            value.api_code,
            value.year,
            value.quote,
            value.reference,
        )
        self.fetched.emit(
            value.api_code,
            value.year,
            value.quote,
            value.reference,
        )

    def shutdown(self, timeout: float = 0.25) -> tuple[str, ...]:
        """Stop accepting results and bound shutdown independently of HTTP I/O."""

        if self._closed:
            return ()
        self._closed = True
        self._fetches.clear()
        return self._workers.shutdown(timeout)
