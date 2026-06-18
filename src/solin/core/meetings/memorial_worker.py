"""Qt worker for loading Memorial publication media off the UI thread."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from solin.core.jw.memorial_publication import (
    MemorialDownloadError,
    download_memorial_bytes,
    resolve_memorial_jwpub,
)

from . import models as meeting_models
from .jwpub_cache import JwpubCache, JwpubChecksumStore
from .memorial_calendar import memorial_date_for_year, monday_of
from .memorial_content import (
    extract_memorial_jwpub,
    memorial_publication_symbol,
    read_memorial_publication_content,
)

log = logging.getLogger(__name__)


class MemorialWorker(QObject):
    """
    Runs in a dedicated QThread.

    All blocking I/O (HTTP, archive extraction, SQLite reads, video resolution)
    happens here. Results are delivered to MemorialService through Qt signals.
    """

    memorial_done = Signal(object)  # meeting_models.MemorialData
    progress = Signal(int)  # 0-100
    error = Signal(str)  # message

    def __init__(
        self,
        jwpub_cache_dir: str | Path,
        checksum_store: JwpubChecksumStore,
        parent=None,
    ):
        super().__init__(parent)
        self._cache = JwpubCache(jwpub_cache_dir)
        self._checksum_store = checksum_store
        self._lang = "T"

    @Slot(str)
    def set_lang(self, lang: str):
        self._lang = lang

    @Slot(int)
    def load_memorial(self, year: int):
        """Fetch and resolve Memorial media for the given year."""
        lang = self._lang
        pub = memorial_publication_symbol(year)
        issue = "0"  # mi<YY> has no numeric issue; use "0" as cache key.

        md = meeting_models.MemorialData(year=year)

        memorial_date = memorial_date_for_year(year)
        if not memorial_date:
            md.status = "error"
            self.error.emit(f"Could not calculate the Memorial date for {year}")
            self.memorial_done.emit(md)
            return

        md.memorial_date = memorial_date
        md.memorial_week = monday_of(memorial_date)

        today = date.today()
        days_until = (memorial_date - today).days
        is_past = days_until < -1

        if days_until > 7:
            md.status = "not_yet"
            self.memorial_done.emit(md)
            return

        # Do not bail out early for is_past. A cached copy should still be
        # shown, and without one the API response determines not_found/deleted.

        self.progress.emit(5)

        jwpub_info = resolve_memorial_jwpub(pub, lang)
        dl_url = jwpub_info.download_url
        checksum = jwpub_info.checksum
        not_found = jwpub_info.not_found
        md.thumb_url = jwpub_info.thumbnail_url

        is_cached = self._cache.is_cached(pub, lang, issue)
        needs_download = (
            not is_cached
            or self._checksum_store.has_changed(pub, lang, issue, checksum)
        )

        if needs_download:
            if not dl_url:
                if is_cached:
                    log.warning(
                        "memorial %s/%s: API unreachable, falling back to stale cache",
                        pub,
                        lang,
                    )
                    self.progress.emit(60)
                else:
                    if is_past and not_found:
                        md.status = "deleted"
                    elif not_found:
                        md.status = "not_found"
                    else:
                        if is_past:
                            md.status = "deleted"
                        else:
                            md.status = "error"
                            self.error.emit(f"URL not found for {pub} lang={lang}")
                    self.memorial_done.emit(md)
                    return
            else:
                dest = self._cache.jwpub_path(pub, lang, issue)
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    raw = download_memorial_bytes(dl_url, timeout=120)
                    self.progress.emit(60)
                    dest.write_bytes(raw)
                    self._cache.invalidate_extract(pub, lang, issue)
                    self._checksum_store.save(pub, lang, issue, checksum)
                except (MemorialDownloadError, OSError) as exc:
                    if is_cached:
                        log.warning(
                            "memorial %s/%s: download failed, using stale cache: %s",
                            pub,
                            lang,
                            exc,
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

        pub_dir = extract_memorial_jwpub(pub, lang, issue, self._cache)
        if not pub_dir:
            md.status = "error"
            self.error.emit("JWPUB extraction failed")
            self.memorial_done.emit(md)
            return

        md.pub_dir = pub_dir
        self.progress.emit(80)

        content = read_memorial_publication_content(pub_dir)
        md.cover_bytes = content.cover_bytes
        self.progress.emit(85)

        md.videos = content.media_items
        md.status = "ready" if (content.media_items or md.cover_bytes) else "empty"
        self.progress.emit(100)
        self.memorial_done.emit(md)


__all__ = ["MemorialWorker"]
