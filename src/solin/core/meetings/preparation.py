"""Single coordinator for durable meeting preparation and optional media caching."""

from __future__ import annotations

import itertools
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal, Slot

from solin.core.foundation.qt_threads import stop_owned_qthread
from solin.core.jw.language_context import JWMediaLanguageContext
from solin.core.media.cache import MediaCacheManager

from .models import WeekData
from .resolution_worker import MeetingMediaResolutionWorker
from .tree_builder import MeetingTreeBuilder
from .tree_merger import media_identity_signature
from .tree_store import (
    MeetingTreeOverview,
    MeetingTreeSnapshot,
    MeetingTreeStore,
    make_meeting_tree_key,
)
from .tree_types import Node

if TYPE_CHECKING:
    from .publications import JwpubService

log = logging.getLogger(__name__)


def _iter_media_with_download_eligibility(
    nodes: list[Node],
    *,
    inside_subsection: bool = False,
) -> Iterator[tuple[Node, bool]]:
    """Walk media with its automatic-download decision and ancestor context."""
    for node in nodes:
        node_type = node.get("type")
        nested_in_subsection = inside_subsection or node_type == "subsection"
        if node_type == "media":
            yield node, not (
                nested_in_subsection and bool(node.get("meeting_generated"))
            )
        children = node.get("children") or []
        if isinstance(children, list):
            yield from _iter_media_with_download_eligibility(
                children,
                inside_subsection=nested_in_subsection,
            )


def _automatic_download_urls(nodes: list[Node]) -> set[str]:
    return {
        url
        for node, eligible in _iter_media_with_download_eligibility(nodes)
        if eligible
        if (url := _remote_media_url(node))
    }


def _remote_media_url(node: Node) -> str:
    resolved = str(node.get("resolved_url") or "")
    if resolved.startswith(("http://", "https://")):
        return resolved
    ref = node.get("media_ref") or {}
    file_path = str(ref.get("file_path") or "") if isinstance(ref, dict) else ""
    return file_path if file_path.startswith(("http://", "https://")) else ""


class MeetingPreparationPriority(IntEnum):
    BACKGROUND = 0
    INTERACTIVE = 1


class MeetingPreparationPhase(str, Enum):
    IDLE = "idle"
    LOADING = "loading"
    AVAILABLE = "available"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class MeetingPreparationKey:
    monday: date
    language_code: str
    is_sign_language: bool = False

    @classmethod
    def from_context(
        cls,
        monday: date,
        context: JWMediaLanguageContext,
    ) -> "MeetingPreparationKey":
        return cls(
            monday=monday,
            language_code=(context.api_code or "T").strip() or "T",
            is_sign_language=bool(context.is_sign_language),
        )


@dataclass(frozen=True, slots=True)
class MeetingPreparationRequest:
    key: MeetingPreparationKey
    force_refresh: bool = False
    download_media: bool = False
    priority: MeetingPreparationPriority = MeetingPreparationPriority.INTERACTIVE


@dataclass(frozen=True, slots=True)
class MeetingPreparationState:
    phase: MeetingPreparationPhase
    snapshot: MeetingTreeSnapshot | None = None
    error: str = ""
    refreshing: bool = False
    source_status: str = ""


@dataclass(slots=True)
class _PreparationJob:
    key: MeetingPreparationKey
    generation: int
    download_media: bool
    priority: MeetingPreparationPriority
    active: bool = True
    force_pending: bool = False
    completed_publications: set[str] = field(default_factory=set)
    errors: dict[str, str] = field(default_factory=dict)
    terminal_statuses: dict[str, str] = field(default_factory=dict)
    pending_by_tree: dict[str, set[str]] = field(default_factory=dict)
    patches_by_tree: dict[
        str,
        dict[str, tuple[tuple, dict[str, Any]]],
    ] = field(default_factory=dict)
    prefetched_urls: set[str] = field(default_factory=set)
    resolved_signatures: set[tuple] = field(default_factory=set)

    @property
    def batch_id(self) -> str:
        sign = "sign" if self.key.is_sign_language else "spoken"
        return (
            f"meeting:{self.key.monday.isoformat()}:"
            f"{self.key.language_code}:{sign}"
        )


@dataclass(frozen=True, slots=True)
class _ResolutionTarget:
    key: MeetingPreparationKey
    generation: int
    tree_key: str
    pub_type: str
    node_id: str
    identity: tuple
    fallback_url: str = ""


@dataclass(slots=True)
class _PendingResolution:
    request_id: str
    signature: tuple
    media_ref: dict[str, Any]
    priority: MeetingPreparationPriority
    order: int
    targets: list[_ResolutionTarget] = field(default_factory=list)


class MeetingPreparationService(QObject):
    """Prepare one durable meeting aggregate for every caller and policy."""

    tree_changed = Signal(object, str, object)
    state_changed = Signal(object, str, object)
    progress = Signal(object, str, int)
    error = Signal(object, str, str)
    _resolve_requested = Signal(str, str, int, int, int, str, bool)

    def __init__(
        self,
        publication_service: "JwpubService",
        store: MeetingTreeStore,
        cache_manager: MediaCacheManager,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._publication_service = publication_service
        self._store = store
        self._cache_manager = cache_manager
        self._builder = MeetingTreeBuilder()
        self._jobs: dict[MeetingPreparationKey, _PreparationJob] = {}
        self._generation = 0
        self._resolution_order = itertools.count()
        self._pending_resolutions: dict[tuple, _PendingResolution] = {}
        self._active_resolution: _PendingResolution | None = None
        self._stopped = False

        publication_service.mwb_ready.connect(self._on_mwb_ready)
        publication_service.wt_ready.connect(self._on_wt_ready)
        publication_service.cbs_ready.connect(self._on_cbs_ready)
        publication_service.context_progress.connect(self._on_progress)
        publication_service.context_error.connect(self._on_error)

        self._resolver_thread = QThread(self)
        self._resolver = MeetingMediaResolutionWorker()
        self._resolver.moveToThread(self._resolver_thread)
        self._resolver_thread.finished.connect(self._resolver.deleteLater)
        self._resolve_requested.connect(self._resolver.resolve)
        self._resolver.resolved.connect(self._on_media_resolved)
        self._resolver_thread.start()

    def ensure_week(self, request: MeetingPreparationRequest) -> MeetingPreparationState:
        if self._stopped:
            return self.state(request.key, "mwb")
        key = request.key
        existing = self._jobs.get(key)
        if existing is not None and existing.active:
            previous_priority = existing.priority
            existing.priority = max(existing.priority, request.priority)
            if existing.priority != previous_priority:
                self._promote_publication(existing)
            self._promote_resolutions(existing)
            if request.download_media and not existing.download_media:
                existing.download_media = True
                self._prefetch_existing_snapshots(existing)
            if request.force_refresh:
                existing.force_pending = True
            return self.state(key, "mwb")

        if existing is not None and not request.force_refresh:
            existing.priority = max(existing.priority, request.priority)
            self._promote_resolutions(existing)
            if request.download_media and not existing.download_media:
                existing.download_media = True
                self._prefetch_existing_snapshots(existing)
            for pub_type, snapshot in self.snapshots(key).items():
                self._schedule_snapshot_media(existing, pub_type, snapshot)
            return self.state(key, "mwb")

        self._generation += 1
        job = _PreparationJob(
            key=key,
            generation=self._generation,
            download_media=bool(request.download_media),
            priority=request.priority,
        )
        self._jobs[key] = job
        if job.download_media:
            self._prefetch_existing_snapshots(job)
        self._emit_states(key)
        snapshots = self.snapshots(key)
        wt_snapshot = snapshots.get("wt")
        self._publication_service.load_week(
            key.monday,
            force=request.force_refresh,
            language_code=key.language_code,
            is_sign_language=key.is_sign_language,
            generation=job.generation,
            priority=int(job.priority),
            materialize_cached_publications=frozenset(
                pub_type
                for pub_type in ("mwb", "wt")
                if pub_type not in snapshots
            ),
            known_wt_issue=wt_snapshot.issue if wt_snapshot is not None else "",
            persisted_source_checksums={
                pub_type: snapshot.source_checksum
                for pub_type, snapshot in snapshots.items()
            },
        )
        return self.state(key, "mwb")

    def snapshots(self, key: MeetingPreparationKey) -> dict[str, MeetingTreeSnapshot]:
        return self._store.snapshots_for_week(
            key.monday,
            key.language_code,
            key.is_sign_language,
        )

    def snapshot(
        self,
        key: MeetingPreparationKey,
        pub_type: str,
    ) -> MeetingTreeSnapshot | None:
        return self.snapshots(key).get(pub_type)

    def week_data(self, key: MeetingPreparationKey) -> WeekData | None:
        return self._publication_service.get_week_data(
            key.monday,
            language_code=key.language_code,
            is_sign_language=key.is_sign_language,
        )

    def loaded_week_data(
        self,
        context: JWMediaLanguageContext,
    ) -> dict[str, WeekData]:
        loaded: dict[str, WeekData] = {}
        for key in self._jobs:
            if (
                key.language_code != context.api_code
                or key.is_sign_language != context.is_sign_language
            ):
                continue
            wd = self.week_data(key)
            if wd is not None:
                loaded[key.monday.isoformat()] = wd
        return loaded

    def state(
        self,
        key: MeetingPreparationKey,
        pub_type: str,
    ) -> MeetingPreparationState:
        snapshot = self.snapshot(key, pub_type)
        job = self._jobs.get(key)
        error = job.errors.get(pub_type, "") if job is not None else ""
        source_status = (
            job.terminal_statuses.get(pub_type, "") if job is not None else ""
        )
        refreshing = bool(job and job.active)
        if snapshot is not None:
            phase = MeetingPreparationPhase.AVAILABLE
        elif error:
            phase = MeetingPreparationPhase.ERROR
        elif refreshing:
            phase = MeetingPreparationPhase.LOADING
        else:
            phase = MeetingPreparationPhase.IDLE
        return MeetingPreparationState(
            phase,
            snapshot,
            error,
            refreshing,
            source_status,
        )

    def cancel_automatic_downloads(self) -> None:
        for job in self._jobs.values():
            if not job.download_media:
                continue
            job.download_media = False
            self._cache_manager.cancel_batch(job.batch_id)
            job.prefetched_urls.clear()

    def cancel_other_language_contexts(
        self,
        context: JWMediaLanguageContext,
    ) -> None:
        """Retire obsolete language work without touching the active context."""
        language = (context.api_code or "T").strip() or "T"
        is_sign = bool(context.is_sign_language)
        for key, job in list(self._jobs.items()):
            if (key.language_code, key.is_sign_language) == (language, is_sign):
                continue
            if job.download_media:
                self._cache_manager.cancel_batch(job.batch_id)
            cancel_pending = getattr(
                self._publication_service,
                "cancel_pending_week",
                None,
            )
            if callable(cancel_pending):
                cancel_pending(
                    key.monday,
                    language_code=key.language_code,
                    is_sign_language=key.is_sign_language,
                    generation=job.generation,
                )
            self._jobs.pop(key, None)
            self._discard_resolution_targets(key, job.generation)

    def shutdown(self, wait_ms: int = 3000) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.cancel_automatic_downloads()
        for signal, callback in (
            (self._publication_service.mwb_ready, self._on_mwb_ready),
            (self._publication_service.wt_ready, self._on_wt_ready),
            (self._publication_service.cbs_ready, self._on_cbs_ready),
            (self._publication_service.context_progress, self._on_progress),
            (self._publication_service.context_error, self._on_error),
            (self._resolver.resolved, self._on_media_resolved),
        ):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._jobs.clear()
        self._pending_resolutions.clear()
        self._active_resolution = None
        thread = getattr(self, "_resolver_thread", None)
        stop_owned_qthread(
            thread,
            wait_ms=wait_ms,
            logger=log,
            label="Meeting media resolver",
        )
        self._publication_service.shutdown(
            wait_ms=wait_ms,
            delete_when_stopped=True,
        )

    @Slot(str, object)
    def _on_mwb_ready(self, _week_text: str, wd: WeekData) -> None:
        self._accept_publication("mwb", wd)

    @Slot(str, object)
    def _on_wt_ready(self, _week_text: str, wd: WeekData) -> None:
        self._accept_publication("wt", wd)

    @Slot(str, object)
    def _on_cbs_ready(self, _week_text: str, wd: WeekData) -> None:
        self._accept_publication("mwb", wd)

    def _accept_publication(self, pub_type: str, wd: WeekData) -> None:
        if self._stopped:
            return
        key = MeetingPreparationKey(
            wd.monday,
            (wd.language_code or "T").strip() or "T",
            bool(wd.is_sign_language),
        )
        job = self._jobs.get(key)
        if job is None or wd.request_generation != job.generation:
            return
        status = wd.mwb_status if pub_type == "mwb" else wd.wt_status
        job.terminal_statuses[pub_type] = status
        if status == "ready":
            self._reconcile_publication(job, pub_type, wd)
        if pub_type == "wt" or wd.cbs_status in {"ready", "empty", "error", "idle"}:
            job.completed_publications.add(pub_type)
        self._finish_or_revalidate(job)
        self._emit_states(key)

    def _reconcile_publication(
        self,
        job: _PreparationJob,
        pub_type: str,
        wd: WeekData,
    ) -> None:
        if pub_type == "mwb":
            canonical = self._builder.build_midweek(wd)
            issue = wd.mwb_issue or ""
            overview = MeetingTreeOverview(
                title=wd.mwb_date_label or wd.mwb_week_title,
                cover_bytes=wd.mwb_cover_bytes,
            )
        else:
            canonical = self._builder.build_weekend(wd)
            issue = wd.wt_issue or ""
            overview = MeetingTreeOverview(
                title=wd.wt_study_title,
                cover_bytes=wd.wt_cover_bytes,
            )
        tree_key = make_meeting_tree_key(
            pub_type,
            job.key.monday,
            job.key.language_code,
            issue,
            is_sign_language=job.key.is_sign_language,
        )
        fallback = self._store.find_snapshot(
            pub_type,
            job.key.monday,
            job.key.language_code,
            job.key.is_sign_language,
        )
        try:
            snapshot = self._store.reconcile(
                tree_key,
                canonical,
                self._builder.canonical_hash(canonical),
                overview,
                fallback=fallback,
                source_checksum=(
                    wd.mwb_source_checksum
                    if pub_type == "mwb"
                    else wd.wt_source_checksum
                ),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            message = str(exc)
            job.errors[pub_type] = message
            self.error.emit(job.key, pub_type, message)
            return
        job.errors.pop(pub_type, None)
        self.tree_changed.emit(job.key, pub_type, snapshot)
        self._schedule_snapshot_media(job, pub_type, snapshot)

    def _schedule_snapshot_media(
        self,
        job: _PreparationJob,
        pub_type: str,
        snapshot: MeetingTreeSnapshot,
    ) -> None:
        ready_urls: set[str] = set()
        pending = job.pending_by_tree.setdefault(snapshot.tree_key, set())
        for node, automatic_download in _iter_media_with_download_eligibility(
            snapshot.nodes
        ):
            url = self._remote_url(node)
            ref = node.get("media_ref") or {}
            has_jw_identity = isinstance(ref, dict) and bool(
                ref.get("key_symbol") or ref.get("meps_doc_id")
            )
            signature = (
                self._resolution_signature(job.key, ref)
                if isinstance(ref, dict) and has_jw_identity
                else ()
            )
            if url and automatic_download:
                ready_urls.add(url)
            if url and (
                not has_jw_identity
                or signature in job.resolved_signatures
                or (
                    self._cache_manager.is_cached(url)
                    and self._has_duration_metadata(node)
                )
            ):
                continue
            if not isinstance(ref, dict) or not has_jw_identity:
                continue
            node_id = str(node.get("id") or "")
            if not node_id or node_id in pending:
                continue
            pending.add(node_id)
            target = _ResolutionTarget(
                key=job.key,
                generation=job.generation,
                tree_key=snapshot.tree_key,
                pub_type=pub_type,
                node_id=node_id,
                identity=media_identity_signature(node),
                fallback_url=url,
            )
            self._enqueue_resolution(signature, ref, target, job.priority)
        self._prefetch_urls(job, ready_urls)

    @staticmethod
    def _remote_url(node: Node) -> str:
        return _remote_media_url(node)

    @staticmethod
    def _has_duration_metadata(node: Node) -> bool:
        media_type = str(node.get("media_type") or "").lower()
        ref = node.get("media_ref") or {}
        mime_type = (
            str(ref.get("mime_type") or "").lower()
            if isinstance(ref, dict)
            else ""
        )
        if media_type == "image" or mime_type.startswith("image/"):
            return True
        try:
            return int(node.get("base_duration_ticks") or 0) > 0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _resolution_signature(
        key: MeetingPreparationKey,
        ref: dict[str, Any],
    ) -> tuple:
        return (
            key.language_code,
            key.is_sign_language,
            str(ref.get("key_symbol") or ""),
            int(ref.get("track") or 0),
            int(ref.get("issue_tag") or 0),
            int(ref.get("meps_doc_id") or 0),
        )

    def _enqueue_resolution(
        self,
        signature: tuple,
        media_ref: dict[str, Any],
        target: _ResolutionTarget,
        priority: MeetingPreparationPriority,
    ) -> None:
        if self._active_resolution and self._active_resolution.signature == signature:
            self._active_resolution.targets.append(target)
            return
        pending = self._pending_resolutions.get(signature)
        if pending is None:
            pending = _PendingResolution(
                request_id=uuid4().hex,
                signature=signature,
                media_ref=dict(media_ref),
                priority=priority,
                order=next(self._resolution_order),
            )
            self._pending_resolutions[signature] = pending
        else:
            pending.priority = max(pending.priority, priority)
        pending.targets.append(target)
        self._dispatch_resolution()

    def _dispatch_resolution(self) -> None:
        if (
            self._stopped
            or self._active_resolution is not None
            or not self._pending_resolutions
        ):
            return
        pending = max(
            self._pending_resolutions.values(),
            key=lambda entry: (int(entry.priority), -entry.order),
        )
        self._pending_resolutions.pop(pending.signature, None)
        self._active_resolution = pending
        ref = pending.media_ref
        signature = pending.signature
        self._resolve_requested.emit(
            pending.request_id,
            str(ref.get("key_symbol") or ""),
            int(ref.get("track") or 0),
            int(ref.get("issue_tag") or 0),
            int(ref.get("meps_doc_id") or 0),
            str(signature[0]),
            bool(signature[1]),
        )

    def _promote_resolutions(self, job: _PreparationJob) -> None:
        for pending in self._pending_resolutions.values():
            if any(
                target.key == job.key and target.generation == job.generation
                for target in pending.targets
            ):
                pending.priority = max(pending.priority, job.priority)

    def _promote_publication(self, job: _PreparationJob) -> None:
        promote = getattr(self._publication_service, "promote_week", None)
        if not callable(promote):
            return
        promote(
            job.key.monday,
            language_code=job.key.language_code,
            is_sign_language=job.key.is_sign_language,
            generation=job.generation,
            priority=int(job.priority),
        )

    def _discard_resolution_targets(
        self,
        key: MeetingPreparationKey,
        generation: int,
    ) -> None:
        for signature, pending in list(self._pending_resolutions.items()):
            pending.targets = [
                target
                for target in pending.targets
                if not (target.key == key and target.generation == generation)
            ]
            if not pending.targets:
                self._pending_resolutions.pop(signature, None)
        if self._active_resolution is not None:
            self._active_resolution.targets = [
                target
                for target in self._active_resolution.targets
                if not (target.key == key and target.generation == generation)
            ]

    @Slot(str, object)
    def _on_media_resolved(self, request_id: str, raw_result: object) -> None:
        if self._stopped:
            return
        pending = self._active_resolution
        if pending is None or pending.request_id != request_id:
            return
        self._active_resolution = None
        result = dict(raw_result) if isinstance(raw_result, dict) else {}
        url = str(result.get("url") or "")
        title = str(result.get("title") or "")
        thumbnail = str(result.get("thumbnail") or "")
        try:
            duration_ticks = max(0, int(result.get("duration_ticks") or 0))
        except (TypeError, ValueError):
            duration_ticks = 0
        touched: set[tuple[MeetingPreparationKey, str, str]] = set()
        for target in pending.targets:
            job = self._jobs.get(target.key)
            if job is None or job.generation != target.generation:
                continue
            tree_pending = job.pending_by_tree.setdefault(target.tree_key, set())
            tree_pending.discard(target.node_id)
            if url:
                job.resolved_signatures.add(pending.signature)
                patch: dict[str, Any] = {"resolved_url": url}
                if title:
                    patch["title"] = title
                    patch["auto_title"] = False
                    patch["media_ref_label"] = title
                if thumbnail:
                    patch["thumbnail_url"] = thumbnail
                if duration_ticks:
                    patch["base_duration_ticks"] = duration_ticks
                job.patches_by_tree.setdefault(target.tree_key, {})[target.node_id] = (
                    target.identity,
                    patch,
                )
            elif target.fallback_url:
                job.prefetched_urls.discard(target.fallback_url)
            touched.add((target.key, target.pub_type, target.tree_key))

        for key, pub_type, tree_key in touched:
            job = self._jobs.get(key)
            if job is None or job.pending_by_tree.get(tree_key):
                continue
            patches = job.patches_by_tree.pop(tree_key, {})
            try:
                snapshot = self._store.patch_media_batch(tree_key, patches)
            except (OSError, UnicodeError, ValueError) as exc:
                message = str(exc)
                job.errors[pub_type] = message
                self.error.emit(key, pub_type, message)
                continue
            if snapshot is not None:
                self.tree_changed.emit(key, pub_type, snapshot)
                self._prefetch_urls(job, _automatic_download_urls(snapshot.nodes))
            self._emit_states(key)
        self._dispatch_resolution()

    def _prefetch_existing_snapshots(self, job: _PreparationJob) -> None:
        for pub_type, snapshot in self.snapshots(job.key).items():
            self._schedule_snapshot_media(job, pub_type, snapshot)

    def _prefetch_urls(self, job: _PreparationJob, urls: set[str]) -> None:
        if not job.download_media:
            return
        candidates = sorted(
            url
            for url in urls
            if url and url not in job.prefetched_urls
        )
        if not candidates:
            return
        self._cache_manager.prefetch_many(candidates, job.batch_id)
        job.prefetched_urls.update(candidates)

    @Slot(str, str, int, str, bool, int)
    def _on_progress(
        self,
        monday_text: str,
        pub_type: str,
        percent: int,
        language: str,
        is_sign_language: bool,
        generation: int,
    ) -> None:
        if self._stopped:
            return
        try:
            monday = date.fromisoformat(monday_text)
        except ValueError:
            return
        key = MeetingPreparationKey(monday, language, is_sign_language)
        job = self._jobs.get(key)
        if job is not None and job.generation == generation:
            self.progress.emit(key, pub_type, percent)

    @Slot(str, str, str, str, bool, int)
    def _on_error(
        self,
        monday_text: str,
        pub_type: str,
        message: str,
        language: str,
        is_sign_language: bool,
        generation: int,
    ) -> None:
        if self._stopped:
            return
        try:
            monday = date.fromisoformat(monday_text)
        except ValueError:
            return
        key = MeetingPreparationKey(monday, language, is_sign_language)
        job = self._jobs.get(key)
        if job is None or job.generation != generation:
            return
        job.errors[pub_type] = message
        job.terminal_statuses[pub_type] = (
            "not_found" if message == "NOT_FOUND" else "error"
        )
        job.completed_publications.add(pub_type)
        self.error.emit(key, pub_type, message)
        self._finish_or_revalidate(job)
        self._emit_states(key)

    def _finish_or_revalidate(self, job: _PreparationJob) -> None:
        if not {"mwb", "wt"}.issubset(job.completed_publications):
            return
        job.active = False
        if not job.force_pending:
            return
        job.force_pending = False
        self.ensure_week(
            MeetingPreparationRequest(
                key=job.key,
                force_refresh=True,
                download_media=job.download_media,
                priority=job.priority,
            )
        )

    def _emit_states(self, key: MeetingPreparationKey) -> None:
        for pub_type in ("mwb", "wt"):
            self.state_changed.emit(key, pub_type, self.state(key, pub_type))


__all__ = [
    "MeetingPreparationKey",
    "MeetingPreparationPhase",
    "MeetingPreparationPriority",
    "MeetingPreparationRequest",
    "MeetingPreparationService",
    "MeetingPreparationState",
]
