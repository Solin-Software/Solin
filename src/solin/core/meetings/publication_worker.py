"""Qt worker for loading meeting JWPUB publications off the UI thread."""

from __future__ import annotations

import hashlib
import logging
import time
import sqlite3
import threading
from copy import deepcopy
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from uuid import uuid4

if TYPE_CHECKING:
    from solin.core.media.publication_assets import PublicationAssetStore

from PySide6.QtCore import QObject, Signal, Slot

from solin.core.jw.publication_archive import (
    JwpubArchiveDownloadError,
    download_jwpub_archive,
    resolve_jwpub_archive,
    resolve_meeting_media,
)
from solin.core.jw.publication_links import JwpubMediaInfo

from . import models as meeting_models
from .content_policy import (
    MeetingContentError,
    MeetingContentPolicy,
    MeetingContentOperation,
    PublicationContentRequest,
    publication_dependency,
)
from .jwpub_cache import JwpubCache, JwpubChecksumStore, local_jwpub_checksum, needs_jwpub_download
from .meeting_weeks import current_monday, mwb_issue_for_week, watchtower_issue_candidates
from .publication_content import (
    parse_publication_ref_items,
    read_mwb_week_content,
    read_watchtower_study_content,
    sync_cbs_from_publication_refs,
    watchtower_issue_contains_week,
)

log = logging.getLogger(__name__)


# Worker: all blocking logic runs here, never on the main thread.


class JwpubWorker(QObject):
    """
    Run in a dedicated QThread. All blocking operations (HTTP, ZIP, SQLite)
    happen here. Communicate with JwpubService exclusively through signals;
    QThread provides automatic QueuedConnection without manual moveToThread.

    Signals emitted to the main thread:
      mwb_done(key, WeekData)
      wt_done(key, WeekData)
      cbs_done(key, WeekData)
      progress(key, pub, pct)
      error(key, pub, msg)
      media_resolved(request_id, metadata)
    """

    asset_changed = Signal(str)
    content_checked = Signal(object, str, object)
    mwb_done = Signal(str, object)
    wt_done = Signal(str, object)
    cbs_done = Signal(str, object)
    load_finished = Signal(str, str, bool, int)
    progress = Signal(str, str, int, str, bool, int)
    error = Signal(str, str, str, str, bool, int)
    media_resolved = Signal(str, object)  # request_id, resolved metadata

    def __init__(
        self,
        jwpub_cache_dir: str | PathLike[str],
        checksum_store: JwpubChecksumStore,
        parent=None,
        *,
        asset_store: PublicationAssetStore | None = None,
        content_policy: MeetingContentPolicy | None = None,
        protected_publication_sources: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._cache = JwpubCache(jwpub_cache_dir)
        self._asset_store = asset_store
        self._protected_sources = protected_publication_sources or (lambda: ())
        self._asset_namespace = uuid4().hex
        self._asset_paths: set[str] = set()
        self._legacy_restored: set[str] = set()
        self._cancelled_requests: set[tuple[str, str, bool, int]] = set()
        self._request_lock = threading.Lock()
        self._current_request: tuple[str, str, bool, int] | None = None
        self._content_policy = content_policy or MeetingContentPolicy()
        self._remote_observations: dict[tuple[str, str, str], tuple[float, Any]] = {}
        self._checksum_store = checksum_store
        self._downloaded_publications: set[tuple[str, str, str]] = set()
        self._lang = "T"
        self._is_sign_language = False
        self._request_generation = 0
        self._cancelled = threading.Event()

    def request_cancel(self) -> None:
        """Request cancellation safely from the main thread."""
        self._cancelled.set()

    def _emit_if_active(self, signal, *args) -> None:
        if not self._is_cancelled():
            signal.emit(*args)

    def cancel_request(self, key: tuple[str, str, bool, int]) -> None:
        with self._request_lock:
            self._cancelled_requests.add(key)

    def _is_cancelled(self) -> bool:
        with self._request_lock:
            return self._cancelled.is_set() or self._current_request in self._cancelled_requests

    def release_assets(self, key: tuple[str, str, bool, int]) -> None:
        with self._request_lock:
            self._cancelled_requests.discard(key)
        if self._asset_store is not None:
            self._asset_store.release(self._asset_namespace + repr(key))

    def _publish_image(self, source: Path, *, title: str, expected_hash: str) -> str:
        if self._asset_store is None:
            raise RuntimeError("A publication asset store is required to prepare meeting images")
        candidate = str(self._asset_store.root / (expected_hash + source.suffix.lower()))
        with self._asset_store.lease([candidate]):
            destination = self._asset_store.materialize(source, title=title)
            self._asset_paths.add(destination)
            self._asset_store.retain(
                self._asset_namespace + repr(self._current_request), self._asset_paths
            )
        return destination

    def _new_week_data(self, monday: date, **values) -> meeting_models.WeekData:
        return meeting_models.WeekData(
            monday=monday,
            language_code=self._lang,
            is_sign_language=self._is_sign_language,
            request_generation=self._request_generation,
            **values,
        )

    def _emit_error(self, key: str, publication: str, message: str) -> None:
        self._emit_if_active(
            self.error,
            key,
            publication,
            message,
            self._lang,
            self._is_sign_language,
            self._request_generation,
        )

    def _emit_progress(self, key: str, publication: str, percent: int) -> None:
        self._emit_if_active(
            self.progress,
            key,
            publication,
            percent,
            self._lang,
            self._is_sign_language,
            self._request_generation,
        )

    @Slot(str)
    def set_lang(self, lang: str):
        self._lang = lang

    @Slot(bool)
    def set_sign_language(self, is_sign: bool):
        """Indicate whether the media language is a sign language (affects song resolution)."""
        self._is_sign_language = is_sign

    # ── Load week ─────────────────────────────────────────────────────────────

    @Slot(object, bool, str, bool, int, str, object)
    def load_week(
        self,
        monday: date,
        force: bool = False,
        language: str = "T",
        is_sign_language: bool = False,
        generation: int = 0,
        known_wt_issue: str = "",
        content_request: PublicationContentRequest | None = None,
    ) -> None:
        """Execute one explicit content operation for the selected publications."""
        if self._cancelled.is_set():
            return
        request = content_request or PublicationContentRequest()
        self._lang = language or "T"
        self._is_sign_language = bool(is_sign_language)
        self._request_generation = max(0, int(generation))
        self._current_request = (
            monday.isoformat(),
            self._lang,
            self._is_sign_language,
            self._request_generation,
        )
        self._asset_paths = set()
        self._legacy_restored = set()
        self._legacy_archive_proofs: dict[Path, str] = {}
        self._loaded_publications: set[tuple[str, str, str]] = set()
        self._downloaded_publications.clear()
        if force:
            self._remote_observations.clear()
        try:
            operation = self._cache.operation(protected_sources=tuple(self._protected_sources()))
            with operation:
                self._execute_content_request(monday, str(known_wt_issue), request)
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary always terminates failed requests
            log.exception("Meeting content operation failed")
            for pub_type in request.publications:
                self._emit_error(monday.isoformat(), pub_type, str(exc))
        finally:
            self._prune_publications(monday, request)
            self.load_finished.emit(
                monday.isoformat(), self._lang, self._is_sign_language, self._request_generation
            )

    def _prune_publications(self, monday: date, request: PublicationContentRequest) -> None:
        if self._asset_store is None:
            return
        preferred = set()
        anchor = current_monday()
        for week in (anchor, anchor + timedelta(weeks=1)):
            preferred.add(("mwb", self._lang, mwb_issue_for_week(week)))
            preferred.update(
                ("w", self._lang, issue) for issue in watchtower_issue_candidates(week)
            )
        if monday in {anchor, anchor + timedelta(weeks=1)}:
            preferred.update(self._loaded_publications)
            preferred.update(
                (dep["pub"], dep["language"], dep["issue"])
                for dependencies in request.dependencies.values()
                for dep in dependencies
            )
        try:
            self._cache.prune(
                protected_sources=tuple(set(self._protected_sources()) | self._legacy_restored),
                preferred_publications=preferred,
            )
        except Exception:  # noqa: BLE001 - optional maintenance must fail closed without failing committed content
            log.warning("Could not prune publication cache", exc_info=True)

    def _resolve_archive(self, pub: str, language: str, issue: str):
        key = (pub, language, issue)
        now = time.monotonic()
        cached = self._remote_observations.get(key)
        if cached is not None and now - cached[0] < 6 * 60 * 60:
            return cached[1]
        info = resolve_jwpub_archive(pub, language, issue)
        if info.checksum:
            self._remote_observations[key] = (now, info)
        return info

    def _execute_content_request(
        self, monday: date, known_wt_issue: str, request: PublicationContentRequest
    ) -> None:
        if request.operation == MeetingContentOperation.REPAIR:
            self._repair_images(monday, request)
            return
        cached: dict[str, meeting_models.WeekData] = {}
        if request.operation != MeetingContentOperation.CHECK:
            for pub_type in sorted(request.publications):
                if self._is_cancelled():
                    return
                try:
                    data = self._serve_cached_publication(monday, known_wt_issue, pub_type, request)
                    if data is not None:
                        cached[pub_type] = data
                except Exception:  # noqa: BLE001 - a damaged cache must not prevent revalidation
                    log.warning("Could not serve cached %s", pub_type, exc_info=True)
        for pub_type in sorted(request.publications):
            if self._is_cancelled():
                return
            try:
                self._execute_publication_request(
                    monday, known_wt_issue, pub_type, request, cached.get(pub_type)
                )
            except Exception as exc:  # noqa: BLE001 - independent publications must fail separately
                log.exception("Meeting %s operation failed for %s", request.operation, pub_type)
                if pub_type not in cached:
                    self._emit_error(monday.isoformat(), pub_type, str(exc))
                elif pub_type == "mwb":
                    self._revalidate_midweek_references(
                        monday, cached[pub_type], request.dependencies.get(pub_type, [])
                    )

    def _source_matches_applied(
        self,
        pub: str,
        issue: str,
        dependencies: Iterable[dict[str, str]],
        remote_checksum: str = "",
    ) -> bool:
        applied = [
            dependency
            for dependency in dependencies
            if (dependency["pub"], dependency["language"]) == (pub, self._lang)
            and dependency.get("checksum")
        ]
        cached = self._checksum_store.get(pub, self._lang, issue)
        return (
            not applied
            or (pub, self._lang, issue) in self._downloaded_publications
            or any(
                dependency["issue"] == issue and dependency["checksum"] == cached
                for dependency in applied
            )
            or bool(remote_checksum and cached == remote_checksum)
        )

    def _serve_cached_publication(
        self, monday: date, known_wt_issue: str, pub_type: str, request: PublicationContentRequest
    ) -> meeting_models.WeekData | None:
        dependencies = request.dependencies.get(pub_type, [])
        if pub_type == "mwb":
            issue = mwb_issue_for_week(monday)
            if self._cache.can_materialize(
                "mwb", self._lang, issue
            ) and self._source_matches_applied("mwb", issue, dependencies):
                return self._parse_mwb(
                    self._new_week_data(
                        monday,
                        mwb_status="loading",
                        mwb_issue=issue,
                        mwb_source_checksum=self._checksum_store.get("mwb", self._lang, issue),
                    ),
                    monday,
                    issue,
                    self._lang,
                    dependencies,
                    cache_only=True,
                )
        elif pub_type == "wt":
            candidates = [known_wt_issue] if known_wt_issue else watchtower_issue_candidates(monday)
            for issue in dict.fromkeys(candidates):
                if self._is_cancelled():
                    return None
                if not self._cache.can_materialize(
                    "w", self._lang, issue
                ) or not self._source_matches_applied("w", issue, dependencies):
                    continue
                data = self._new_week_data(
                    monday,
                    wt_status="loading",
                    wt_source_checksum=self._checksum_store.get("w", self._lang, issue),
                )
                if self._try_wt_cached(data, monday, issue, self._lang, emit_errors=False):
                    return data
        return None

    def _revalidate_midweek_references(
        self,
        monday: date,
        cached: meeting_models.WeekData,
        dependencies: Iterable[dict[str, str]],
    ) -> None:
        data = deepcopy(cached)
        if data.mwb_publication_refs:
            self._load_mwb_publication_refs(monday, data, data.mwb_publication_refs, dependencies)
        else:
            data.cbs_status = "idle"
            self._emit_if_active(self.mwb_done, monday.isoformat(), deepcopy(data))

    def _execute_publication_request(
        self,
        monday: date,
        known_wt_issue: str,
        pub_type: str,
        request: PublicationContentRequest,
        cached: meeting_models.WeekData | None = None,
    ) -> None:
        if request.operation == MeetingContentOperation.CHECK:
            observed = {}
            for dep in request.dependencies.get(pub_type, []):
                if self._is_cancelled():
                    return
                info = self._resolve_archive(dep["pub"], dep["language"], dep["issue"])
                if info.checksum:
                    observed[":".join((dep["pub"], dep["language"], dep["issue"]))] = info.checksum
            self._emit_if_active(
                self.content_checked,
                (monday, self._lang, self._is_sign_language, self._request_generation),
                pub_type,
                observed,
            )
        elif pub_type == "mwb":
            issue = mwb_issue_for_week(monday)
            info = self._resolve_archive("mwb", self._lang, issue)
            if self._is_cancelled():
                return
            obtained = self._obtain_publication(
                "mwb",
                self._lang,
                issue,
                monday.isoformat(),
                "mwb",
                emit_error=cached is None,
                archive_info=info,
                refresh_unverified=not self._source_matches_applied(
                    "mwb", issue, request.dependencies.get("mwb", []), info.checksum
                ),
            )
            if cached is not None and (
                not obtained
                or cached.mwb_source_checksum == self._checksum_store.get("mwb", self._lang, issue)
            ):
                self._revalidate_midweek_references(
                    monday, cached, request.dependencies.get("mwb", [])
                )
            elif obtained and self._source_matches_applied(
                "mwb", issue, request.dependencies.get("mwb", []), info.checksum
            ):
                self._parse_mwb(
                    self._new_week_data(
                        monday,
                        mwb_status="loading",
                        mwb_issue=issue,
                        mwb_source_checksum=self._checksum_store.get("mwb", self._lang, issue),
                    ),
                    monday,
                    issue,
                    self._lang,
                    request.dependencies.get("mwb", []),
                )
        elif pub_type == "wt":
            candidates = [known_wt_issue] if known_wt_issue else watchtower_issue_candidates(monday)
            if cached is not None:
                candidates = [cached.wt_issue]
            for issue in dict.fromkeys(candidates):
                if self._is_cancelled():
                    return
                info = self._resolve_archive("w", self._lang, issue)
                if self._is_cancelled():
                    return
                if self._obtain_publication(
                    "w",
                    self._lang,
                    issue,
                    monday.isoformat(),
                    "wt",
                    emit_error=False,
                    archive_info=info,
                    refresh_unverified=not self._source_matches_applied(
                        "w", issue, request.dependencies.get("wt", []), info.checksum
                    ),
                ):
                    if not self._source_matches_applied(
                        "w", issue, request.dependencies.get("wt", []), info.checksum
                    ):
                        continue
                    if cached is not None and cached.wt_source_checksum == self._checksum_store.get(
                        "w", self._lang, issue
                    ):
                        return
                    if self._try_wt_cached(
                        self._new_week_data(
                            monday,
                            wt_status="loading",
                            wt_source_checksum=self._checksum_store.get("w", self._lang, issue),
                        ),
                        monday,
                        issue,
                        self._lang,
                    ):
                        break
            else:
                if cached is None:
                    self._emit_error(monday.isoformat(), "wt", "NOT_FOUND")

    def _obtain_publication(
        self,
        pub,
        lang,
        issue,
        key,
        pub_ui,
        *,
        emit_error=True,
        archive_info: JwpubMediaInfo | None = None,
        refresh_unverified: bool = False,
    ) -> bool:
        self._loaded_publications.add((pub, lang, issue))
        info = archive_info if archive_info is not None else self._resolve_archive(pub, lang, issue)
        cached = self._cache.can_materialize(pub, lang, issue)
        if not info.download_url:
            if not cached and emit_error:
                self._emit_error(
                    key, pub_ui, "NOT_FOUND" if info.not_found else "Publication unavailable"
                )
            return cached
        if (refresh_unverified and not info.checksum) or needs_jwpub_download(
            self._cache, self._checksum_store, pub, lang, issue, info.checksum
        ):
            if not self._download(
                pub,
                lang,
                issue,
                info.download_url,
                key,
                pub_ui,
                emit_error=emit_error,
                expected_checksum=info.checksum,
            ):
                return False
            self._record_downloaded_revision(pub, lang, issue, info.checksum)
        return True

    def _record_downloaded_revision(self, pub: str, lang: str, issue: str, checksum: str) -> None:
        checksum = checksum or local_jwpub_checksum(self._cache.jwpub_path(pub, lang, issue))
        if not checksum:
            self._checksum_store.delete(pub, lang, issue)
            raise OSError("Downloaded publication revision could not be verified")
        self._checksum_store.save(pub, lang, issue, checksum)
        self._downloaded_publications.add((pub, lang, issue))

    def _materialize_images(self, items, pub_dir: Path, pub: str, lang: str, issue: str) -> None:
        checksum = self._checksum_store.get(pub, lang, issue)
        for item in items:
            if item.media_type != "image" or not item.file_path:
                continue
            source = Path(item.file_path)
            member = source.resolve().relative_to(pub_dir.resolve()).as_posix()
            item.publication_source = {
                **publication_dependency(pub, lang, issue, checksum),
                "member": member,
            }
            with source.open("rb") as stream:
                item.asset_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            item.file_path = self._publish_image(
                source, title=item.label or item.caption, expected_hash=item.asset_hash
            )
            self._emit_if_active(self.asset_changed, item.file_path)

    def _repair_images(self, monday: date, request: PublicationContentRequest) -> None:
        if self._asset_store is None:
            return
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for ref in request.repair_refs:
            source = ref.get("publication_source")
            if not isinstance(source, dict) or not ref.get("asset_hash"):
                if not self._repair_legacy_image(ref, request):
                    for pub_type in request.publications:
                        self._emit_error(
                            monday.isoformat(),
                            pub_type,
                            MeetingContentError.LEGACY_REVISION_UNVERIFIABLE,
                        )
                continue
            identity = (
                str(source.get("pub") or ""),
                str(source.get("language") or ""),
                str(source.get("issue") or ""),
            )
            if all(identity):
                grouped.setdefault(identity, []).append(ref)
        for (pub, lang, issue), refs in grouped.items():
            unresolved = self._repair_from_local(pub, lang, issue, refs)
            if not unresolved or self._is_cancelled():
                continue
            info = self._resolve_archive(pub, lang, issue)
            if info.download_url and self._download(
                pub,
                lang,
                issue,
                info.download_url,
                monday.isoformat(),
                next(iter(request.publications)),
                emit_error=False,
                expected_checksum=info.checksum,
            ):
                self._record_downloaded_revision(pub, lang, issue, info.checksum)
                unresolved = self._repair_from_local(pub, lang, issue, unresolved)
            if unresolved:
                for pub_type in request.publications:
                    self._emit_error(
                        monday.isoformat(), pub_type, MeetingContentError.IMAGE_REVISION_UNAVAILABLE
                    )

    def _repair_legacy_image(self, ref: dict[str, Any], request: PublicationContentRequest) -> bool:
        """Bridge persisted extraction references only when the saved revision is proven."""
        path = Path(str(ref.get("file_path") or "")).resolve()
        for dependencies in request.dependencies.values():
            for dependency in dependencies:
                pub, language, issue = (dependency[name] for name in ("pub", "language", "issue"))
                root = self._cache.extract_dir(pub, language, issue).resolve()
                expected = dependency.get("checksum")
                if not expected or not path.is_relative_to(root):
                    continue
                archive = self._cache.jwpub_path(pub, language, issue)
                actual = self._legacy_archive_proofs.get(archive)
                if actual is None:
                    actual = local_jwpub_checksum(archive)
                    self._legacy_archive_proofs[archive] = actual
                if actual.casefold() != expected.casefold():
                    continue
                if path.is_file() or (
                    self._cache.repair_extract_for_source(str(path)) and path.is_file()
                ):
                    self._legacy_restored.add(str(path))
                    self._emit_if_active(self.asset_changed, str(path))
                    return True
        return False

    def _repair_from_local(self, pub, lang, issue, refs):
        pub_dir = self._ensure_extract(pub, lang, issue)
        if pub_dir is None:
            return refs
        unresolved = []
        for ref in refs:
            source = ref["publication_source"]
            path = (pub_dir / source["member"]).resolve()
            if not path.is_relative_to(pub_dir.resolve()) or not path.is_file():
                unresolved.append(ref)
                continue
            with path.open("rb") as stream:
                actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_hash != ref["asset_hash"]:
                unresolved.append(ref)
                continue
            destination = self._publish_image(
                path, title=str(ref.get("label") or ""), expected_hash=ref["asset_hash"]
            )
            self._emit_if_active(self.asset_changed, destination)
            if Path(destination) != Path(str(ref.get("file_path") or "")):
                unresolved.append(ref)
        return unresolved

    def _parse_mwb(
        self,
        wd: meeting_models.WeekData,
        monday: date,
        issue: str,
        lang: str,
        applied_dependencies: Iterable[dict[str, str]] = (),
        *,
        cache_only: bool = False,
    ) -> meeting_models.WeekData | None:
        key = monday.isoformat()
        pub_dir = self._ensure_extract("mwb", lang, issue)
        db_path = self._cache.db_path("mwb", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            wd.mwb_status = "error"
            if not cache_only:
                self._emit_error(key, "mwb", "Extraction failed")
            return None
        try:
            content = read_mwb_week_content(pub_dir, db_path, monday)
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary reports failures to the UI
            log.exception("Could not parse MWB publication %s/%s", lang, issue)
            wd.mwb_status = "error"
            if not cache_only:
                self._emit_error(key, "mwb", str(exc))
            return None
        if content is None:
            wd.mwb_status = "empty"
            self._emit_if_active(self.mwb_done, key, deepcopy(wd))
            return wd

        self._materialize_images(content.media_items, pub_dir, "mwb", lang, issue)
        wd.mwb_dependencies = [publication_dependency("mwb", lang, issue, wd.mwb_source_checksum)]
        wd.mwb_date_label = content.date_label
        wd.mwb_week_title = content.date_label
        wd.mwb_all_media = content.media_items
        wd.mwb_publication_refs = content.publication_refs
        wd.mwb_cover_bytes = content.cover_bytes
        wd.mwb_status = "ready"
        wd.cbs_ref = content.cbs_ref
        sync_cbs_from_publication_refs(wd)
        if cache_only or content.publication_refs:
            wd.cbs_status = "loading"
        # Queued Qt signals carry Python objects by reference. Publish an independent
        # base snapshot before enrichment mutates reference items and dependencies.
        self._emit_if_active(self.mwb_done, key, deepcopy(wd))
        if content.publication_refs:
            self._load_mwb_publication_refs(
                monday, wd, content.publication_refs, applied_dependencies, cache_only=True
            )
            if not cache_only and not self._is_cancelled():
                self._load_mwb_publication_refs(
                    monday, wd, content.publication_refs, applied_dependencies
                )
        return wd

    # ── WT ────────────────────────────────────────────────────────────────────

    def _try_wt_cached(
        self,
        wd: meeting_models.WeekData,
        monday: date,
        issue: str,
        lang: str,
        *,
        emit_errors: bool = True,
    ) -> bool:
        pub_dir = self._ensure_extract("w", lang, issue)
        db_path = self._cache.db_path("w", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            return False
        try:
            if not watchtower_issue_contains_week(db_path, monday):
                return False
        except (OSError, sqlite3.Error):
            return False
        self._parse_wt(wd, monday, issue, lang, emit_errors=emit_errors)
        return wd.wt_status in {"ready", "empty"}

    def _parse_wt(
        self,
        wd: meeting_models.WeekData,
        monday: date,
        issue: str,
        lang: str,
        *,
        emit_errors: bool = True,
    ):
        key = monday.isoformat()
        pub_dir = self._ensure_extract("w", lang, issue)
        db_path = self._cache.db_path("w", lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            wd.wt_status = "error"
            if emit_errors:
                self._emit_error(key, "wt", "Extraction failed")
            return
        try:
            content = read_watchtower_study_content(pub_dir, db_path, monday)
        except Exception as exc:  # noqa: BLE001 - Qt worker boundary reports failures to the UI
            log.exception("Could not parse WT publication %s/%s", lang, issue)
            wd.wt_status = "error"
            if emit_errors:
                self._emit_error(key, "wt", str(exc))
            return
        if content is None:
            wd.wt_status = "empty"
            self._emit_if_active(self.wt_done, key, deepcopy(wd))
            return
        self._materialize_images(content.media_items, pub_dir, "w", lang, issue)
        wd.wt_dependencies = [publication_dependency("w", lang, issue, wd.wt_source_checksum)]
        wd.wt_issue = issue
        wd.wt_study_title = content.title
        wd.wt_all_media = content.media_items
        wd.wt_cover_bytes = content.cover_bytes
        wd.wt_status = "ready"
        self._emit_if_active(self.wt_done, key, deepcopy(wd))

    def _load_mwb_publication_refs(
        self,
        monday: date,
        wd: meeting_models.WeekData,
        refs: list[meeting_models.MeetingPublicationRef],
        applied_dependencies: Iterable[dict[str, str]] = (),
        *,
        cache_only: bool = False,
    ):
        key = monday.isoformat()
        lang = self._lang
        dependencies = list(applied_dependencies)
        wd.mwb_dependencies = [
            publication_dependency("mwb", lang, wd.mwb_issue, wd.mwb_source_checksum)
        ]
        loaded = []
        incomplete = False
        for ref in deepcopy(refs):
            if self._is_cancelled():
                return
            ref.items = []
            try:
                result, failed = self._load_publication_ref(
                    key, ref, dependencies, cache_only=cache_only
                )
            except Exception:  # noqa: BLE001 - one reference must not suppress healthy siblings
                log.warning("Could not load MWB reference %s/%s", ref.pub, ref.issue, exc_info=True)
                result, failed = None, True
            incomplete = incomplete or failed
            if result is None:
                if cache_only:
                    loaded.append(ref)
                continue
            loaded.append(result)
            dependency = publication_dependency(
                result.pub,
                lang,
                result.issue or "0",
                self._checksum_store.get(result.pub, lang, result.issue or "0"),
            )
            if dependency not in wd.mwb_dependencies:
                wd.mwb_dependencies.append(dependency)
        wd.mwb_publication_refs = loaded
        sync_cbs_from_publication_refs(wd)
        wd.cbs_status = (
            "loading"
            if cache_only
            else ("error" if incomplete else ("ready" if loaded else "empty"))
        )
        self._emit_if_active(self.cbs_done, key, deepcopy(wd))

    def _load_publication_ref(
        self,
        key: str,
        ref: meeting_models.MeetingPublicationRef,
        dependencies: list[dict[str, str]],
        *,
        cache_only: bool,
    ) -> tuple[meeting_models.MeetingPublicationRef | None, bool]:
        candidates = [ref.issue or "0"]
        if candidates[0] != "0":
            candidates.append("0")
        unavailable = True
        for issue in candidates:
            if self._is_cancelled():
                return None, True
            remote_checksum = ""
            if cache_only:
                if not self._cache.can_materialize(ref.pub, self._lang, issue):
                    continue
            else:
                info = self._resolve_archive(ref.pub, self._lang, issue)
                if self._is_cancelled():
                    return None, True
                if not self._obtain_publication(
                    ref.pub,
                    self._lang,
                    issue,
                    key,
                    "mwb",
                    emit_error=False,
                    archive_info=info,
                    refresh_unverified=not self._source_matches_applied(
                        ref.pub, issue, dependencies, info.checksum
                    ),
                ):
                    unavailable = unavailable and info.not_found
                    continue
                remote_checksum = info.checksum
            if not self._source_matches_applied(ref.pub, issue, dependencies, remote_checksum):
                unavailable = False
                continue
            ref.issue = issue
            ref.items = self._parse_ref_items(ref.pub, self._lang, issue, ref)
            return ref, False
        previously_applied = any(
            (dependency["pub"], dependency["language"], dependency["issue"])
            == (ref.pub, self._lang, issue)
            for dependency in dependencies
            for issue in candidates
        )
        return None, cache_only or previously_applied or not unavailable

    def _parse_ref_items(
        self, pub: str, lang: str, issue: str, ref: meeting_models.MeetingPublicationRef
    ) -> list[meeting_models.MeetingMedia]:
        pub_dir = self._ensure_extract(pub, lang, issue)
        db_path = self._cache.db_path(pub, lang, issue) if pub_dir else None
        if not pub_dir or not db_path:
            raise ValueError("Referenced publication could not be extracted")
        items = parse_publication_ref_items(
            pub_dir,
            db_path,
            ref.meps_doc_id,
            "cbs" if ref.is_cbs else ref.section,
            ref.caption,
            strict=True,
        )
        self._materialize_images(items, pub_dir, pub, lang, issue)
        return items

    # ── Media URL resolution (async, called from main thread via signal) ──────

    @Slot(str, str, int, int, int, str, str, bool)
    def resolve_media_async(
        self,
        request_id: str,
        key_symbol: str,
        track: int,
        issue_tag: int,
        meps_doc_id: int,
        media_type: str,
        lang: str,
        is_sign_language: bool,
    ):
        """Resolve meeting media metadata in the worker thread."""
        result = resolve_meeting_media(
            key_symbol,
            track,
            issue_tag,
            meps_doc_id,
            lang,
            is_sign_language=is_sign_language,
            media_type=media_type,
        )
        self._emit_if_active(
            self.media_resolved,
            request_id,
            result,
        )

    # ── Internal download helper ───────────────────────────────────────────────

    def _download(
        self,
        pub: str,
        lang: str,
        issue: str,
        url: str,
        key: str,
        pub_ui: str,
        emit_error: bool = True,
        *,
        expected_checksum: str = "",
    ) -> bool:
        """
        Download the file, emitting progress. Return True on success.

        emit_error=False suppresses the error signal on download failure.
        Used for stale-while-revalidate: if a local copy was already served and
        background revalidation fails (e.g. network loss during download),
        keep the displayed content instead of replacing it with an error.
        """
        dest = self._cache.jwpub_path(pub, lang, issue)
        try:
            download_jwpub_archive(
                url,
                dest,
                progress=lambda pct: self._emit_progress(key, pub_ui, pct),
                cancelled=self._is_cancelled,
                expected_checksum=expected_checksum,
            )
            # Keep the currently presented files available until a complete
            # replacement has been extracted in staging.
            self._cache.mark_extract_stale(pub, lang, issue)
            return True
        except JwpubArchiveDownloadError as exc:
            if emit_error:
                self._emit_error(key, pub_ui, str(exc))
            else:
                log.warning(
                    "%s %s/%s: background re-download failed, keeping served cache: %s",
                    pub_ui,
                    pub,
                    issue,
                    exc,
                )
            return False

    def _ensure_extract(self, pub: str, lang: str, issue: str) -> Optional[Path]:
        if self._cache.is_cached(pub, lang, issue):
            return self._cache.extract_dir(pub, lang, issue)
        return self._cache.extract(pub, lang, issue)


__all__ = ["JwpubWorker"]
