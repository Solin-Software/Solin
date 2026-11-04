"""Qt worker for loading Memorial publication media off the UI thread."""

from __future__ import annotations

import copy
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from solin.core.media.publication_assets import PublicationAssetStore
from datetime import date
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from solin.core.jw.memorial_publication import (
    MemorialJwpubInfo,
    resolve_memorial_jwpub,
)

from solin.core.jw.publication_archive import JwpubArchiveDownloadError, download_jwpub_archive

from . import models as meeting_models
from .content_policy import (
    MeetingContentError,
    MeetingContentOperation,
    MeetingContentPolicy,
    publication_dependency,
)
from .jwpub_cache import JwpubCache, JwpubChecksumStore, local_jwpub_checksum
from .memorial_calendar import memorial_date_for_year, monday_of
from .memorial_content import (
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

    content_checked = Signal(str, int, str)
    finished = Signal()
    saved_ready = Signal(object)
    asset_changed = Signal(str)
    memorial_done = Signal(object)  # meeting_models.MemorialData
    progress = Signal(int)  # 0-100
    error = Signal(str)  # message

    def __init__(
        self,
        jwpub_cache_dir: str | Path,
        checksum_store: JwpubChecksumStore,
        parent=None,
        *,
        asset_store: PublicationAssetStore | None = None,
        content_policy: MeetingContentPolicy | None = None,
        protected_publication_sources: Callable[[], Iterable[str]] | None = None,
    ):
        super().__init__(parent)
        self._cache = JwpubCache(jwpub_cache_dir)
        self._checksum_store = checksum_store
        self._lang = "T"
        self._asset_store = asset_store
        self._policy = content_policy or MeetingContentPolicy()
        self._protected_sources = protected_publication_sources or (lambda: ())
        self._cancelled = threading.Event()
        self._asset_owner = "memorial:" + uuid4().hex
        self._asset_paths: set[str] = set()
        self._observations: dict[tuple[str, str], tuple[float, MemorialJwpubInfo]] = {}

    @Slot(str)
    def set_lang(self, lang: str):
        self._lang = lang

    def request_cancel(self) -> None:
        self._cancelled.set()

    def release_assets(self) -> None:
        if self._asset_store is not None:
            self._asset_store.release(self._asset_owner)

    @Slot(int, object)
    def load_memorial(self, year: int, options: object = None):
        if self._cancelled.is_set():
            return
        options = options if isinstance(options, dict) else {}
        self._asset_paths = set()
        try:
            operation = self._cache.operation(protected_sources=tuple(self._protected_sources()))
            with operation:
                self._load_memorial(year, options)
        except Exception as exc:  # noqa: BLE001 - worker boundary reports failures without replacing saved content
            log.exception("Memorial content operation failed")
            self.release_assets()
            self.error.emit(str(exc))
        finally:
            if self._asset_store is not None:
                try:
                    self._cache.prune(
                        protected_sources=tuple(self._protected_sources()),
                        preferred_publications={
                            (memorial_publication_symbol(year), self._lang, "0")
                        },
                    )
                except Exception:  # noqa: BLE001 - maintenance fails closed without replacing prepared data
                    log.warning("Could not prune Memorial publication cache", exc_info=True)
            self.finished.emit()

    def _publish_image(self, path: Path, title: str, digest: str) -> str:
        if self._asset_store is None:
            raise RuntimeError("A publication asset store is required to prepare Memorial images")
        candidate = str(self._asset_store.root / (digest + path.suffix.lower()))
        with self._asset_store.lease([candidate]):
            destination = self._asset_store.materialize(path, title=title)
            self._asset_paths.add(destination)
            self._asset_store.retain(self._asset_owner, self._asset_paths)
        self.asset_changed.emit(destination)
        return destination

    def _record_download_checksum(self, pub: str, lang: str, issue: str, checksum: str) -> None:
        checksum = checksum or local_jwpub_checksum(self._cache.jwpub_path(pub, lang, issue))
        if not checksum:
            self._checksum_store.delete(pub, lang, issue)
            raise RuntimeError("Could not determine the downloaded Memorial publication checksum")
        self._checksum_store.save(pub, lang, issue, checksum)

    def _repair_saved(self, saved, pub, lang, issue, download_url, checksum, *, explicit=False):
        missing = [
            item
            for item in saved.videos
            if item.media_type == "image" and not Path(item.file_path).is_file()
        ]
        if not missing or (not explicit and not self._policy.auto_recover_images):
            return not missing
        for download in (False, True):
            if download:
                if not download_url:
                    break
                download_jwpub_archive(
                    download_url,
                    self._cache.jwpub_path(pub, lang, issue),
                    expected_checksum=checksum,
                    cancelled=self._cancelled.is_set,
                )
                self._cache.mark_extract_stale(pub, lang, issue)
                self._record_download_checksum(pub, lang, issue, checksum)
            root = self._cache.extract(pub, lang, issue)
            if root is None:
                continue
            remaining = []
            for item in missing:
                source = item.publication_source
                if not source or not item.asset_hash:
                    remaining.append(item)
                    continue
                path = (root / source["member"]).resolve()
                if not path.is_relative_to(root.resolve()) or not path.is_file():
                    remaining.append(item)
                    continue
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest == item.asset_hash:
                    destination = self._publish_image(path, item.label or item.caption, digest)
                    if Path(destination) != Path(item.file_path):
                        remaining.append(item)
                else:
                    remaining.append(item)
            missing = remaining
            if not missing:
                return True
        return False

    def _load_memorial(self, year: int, options: dict):
        """Fetch and resolve Memorial media for the given year."""
        lang = self._lang
        pub = memorial_publication_symbol(year)
        issue = "0"  # mi<YY> has no numeric issue; use "0" as cache key.

        md = meeting_models.MemorialData(year=year, language_code=lang)

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

        if days_until > 7 and options.get("saved") is None:
            md.status = "not_yet"
            self.memorial_done.emit(md)
            return

        # Do not bail out early for is_past. A cached copy should still be
        # shown, and without one the API response determines not_found/deleted.

        self.progress.emit(5)

        saved = options.get("saved")
        repair_only = options.get("operation") == MeetingContentOperation.REPAIR
        if (
            saved is not None
            and repair_only
            and self._repair_saved(saved, pub, lang, issue, "", "", explicit=True)
        ):
            self.saved_ready.emit(copy.deepcopy(saved))
            return

        observation_key = (pub, lang)
        observation = self._observations.get(observation_key)
        if (
            not options.get("force")
            and observation
            and time.monotonic() - observation[0] < 6 * 60 * 60
        ):
            jwpub_info = observation[1]
        else:
            jwpub_info = resolve_memorial_jwpub(pub, lang)
            if jwpub_info.checksum:
                self._observations[observation_key] = (time.monotonic(), jwpub_info)
        dl_url = jwpub_info.download_url
        checksum = jwpub_info.checksum
        not_found = jwpub_info.not_found
        md.thumb_url = jwpub_info.thumbnail_url
        if checksum:
            self.content_checked.emit(lang, year, checksum)

        if saved is not None and repair_only:
            if not self._repair_saved(saved, pub, lang, issue, dl_url, checksum, explicit=True):
                self.error.emit(MeetingContentError.IMAGE_REVISION_UNAVAILABLE)
            self.saved_ready.emit(copy.deepcopy(saved))
            return

        saved = options.get("saved")
        if saved is not None and not options.get("force"):
            changed = bool(checksum and saved.source_checksum and checksum != saved.source_checksum)
            if not self._policy.auto_update or not changed:
                if not self._repair_saved(saved, pub, lang, issue, dl_url, checksum):
                    if self._policy.auto_recover_images:
                        self.error.emit(MeetingContentError.IMAGE_REVISION_UNAVAILABLE)
                self.saved_ready.emit(copy.deepcopy(saved))
                return
        is_cached = self._cache.can_materialize(pub, lang, issue)
        needs_download = not is_cached or self._checksum_store.has_changed(
            pub, lang, issue, checksum
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
                    download_jwpub_archive(
                        dl_url,
                        dest,
                        timeout=120,
                        expected_checksum=checksum,
                        cancelled=self._cancelled.is_set,
                    )
                    self.progress.emit(60)
                    self._cache.mark_extract_stale(pub, lang, issue)
                    self._record_download_checksum(pub, lang, issue, checksum)
                except (JwpubArchiveDownloadError, OSError) as exc:
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

        pub_dir = self._cache.extract(pub, lang, issue)
        if not pub_dir:
            md.status = "error"
            self.error.emit("JWPUB extraction failed")
            self.memorial_done.emit(md)
            return

        self.progress.emit(80)

        content = read_memorial_publication_content(pub_dir)
        md.cover_bytes = content.cover_bytes
        self.progress.emit(85)

        md.source_checksum = self._checksum_store.get(pub, lang, issue)
        md.publication_dependencies = [publication_dependency(pub, lang, issue, md.source_checksum)]
        for item in content.media_items:
            if item.media_type != "image" or not item.file_path:
                continue
            path = Path(item.file_path)
            item.publication_source = {
                **md.publication_dependencies[0],
                "member": path.resolve().relative_to(pub_dir.resolve()).as_posix(),
            }
            with path.open("rb") as stream:
                item.asset_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            item.file_path = self._publish_image(path, item.label or item.caption, item.asset_hash)
        md.videos = content.media_items
        md.status = "ready" if (content.media_items or md.cover_bytes) else "empty"
        self.progress.emit(100)
        self.memorial_done.emit(md)


__all__ = ["MemorialWorker"]
