"""Single coordinator for durable meeting preparation and optional media caching."""

from __future__ import annotations

import itertools
import logging
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from enum import Enum, IntEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
import shiboken6

from solin.core.foundation.qt_threads import stop_owned_qthread
from solin.core.foundation.thread_workers import CancellationFlag, ThreadedWorkerPool
from solin.core.jw.language_context import JWMediaLanguageContext
from solin.core.media.cache import MediaCacheManager

from .content_policy import (
    MeetingContentError,
    MeetingContentPolicy,
    MeetingContentOperation,
    MeetingRequestOrigin,
    PublicationContentRequest,
    publication_dependency,
)
from .models import WeekData, media_type_for_mime_type
from .meeting_weeks import meeting_week_bounds
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

_CANONICAL_RECOVERY_BATCH_MS = 250


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
            yield node, not (nested_in_subsection and bool(node.get("meeting_generated")))
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
    publications: frozenset[str] = frozenset({"mwb", "wt"})
    operation: MeetingContentOperation = MeetingContentOperation.PREPARE
    origin: MeetingRequestOrigin = MeetingRequestOrigin.MANUAL


@dataclass(frozen=True, slots=True)
class MeetingPreparationState:
    phase: MeetingPreparationPhase
    snapshot: MeetingTreeSnapshot | None = None
    error: str = ""
    refreshing: bool = False
    source_status: str = ""
    update_available: bool = False
    repair_required: bool = False
    repair_requires_update: bool = False


@dataclass(slots=True)
class _MediaDownloadBatch:
    key: MeetingPreparationKey
    pub_type: str
    origin: MeetingRequestOrigin
    batch_id: str = field(default_factory=lambda: f"meeting:{uuid4().hex}")
    prefetched_urls: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _PreparationJob:
    key: MeetingPreparationKey
    generation: int
    priority: MeetingPreparationPriority
    publications: frozenset[str] = frozenset({"mwb", "wt"})
    operation: MeetingContentOperation = MeetingContentOperation.PREPARE
    origin: MeetingRequestOrigin = MeetingRequestOrigin.MANUAL
    active: bool = True
    completed_publications: set[str] = field(default_factory=set)
    errors: dict[str, str] = field(default_factory=dict)
    terminal_statuses: dict[str, str] = field(default_factory=dict)
    repair_update_publications: set[str] = field(default_factory=set)
    pending_by_tree: dict[str, set[str]] = field(default_factory=dict)
    patches_by_tree: dict[
        str,
        dict[str, tuple[tuple, dict[str, Any]]],
    ] = field(default_factory=dict)
    downloads: dict[str, _MediaDownloadBatch] = field(default_factory=dict)

    @property
    def download_media(self) -> bool:
        return bool(self.downloads)


@dataclass(slots=True)
class _QueuedPreparation:
    request: MeetingPreparationRequest
    # Keep the causal owner, not a copy of its grants, so later authorization
    # upgrades and revocation remain effective while the command waits.
    download_sources: dict[str, _PreparationJob] = field(default_factory=dict)

    def bind_downloads(
        self, source: _PreparationJob, publications: frozenset[str] | set[str]
    ) -> None:
        if source.key != self.request.key or not (
            self.request.operation == MeetingContentOperation.UPDATE
            or (self.request.operation == MeetingContentOperation.PREPARE and self.request.force_refresh)
        ):
            return
        for pub_type in publications & self.request.publications:
            previous = self.download_sources.get(pub_type)
            previous_batch = previous.downloads.get(pub_type) if previous is not None else None
            batch = source.downloads.get(pub_type)
            if previous is None or (
                batch is not None and (
                    previous_batch is None
                    or batch.origin == MeetingRequestOrigin.MANUAL
                    or previous_batch.origin != MeetingRequestOrigin.MANUAL
                )
            ):
                self.download_sources[pub_type] = source


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


class _SourceMaintenance:
    def __init__(self) -> None:
        self.workers = ThreadedWorkerPool()
        self.pending: dict[MeetingPreparationKey, CancellationFlag] = {}
        self.dirty: set[MeetingPreparationKey] = set()

    def cancel(self, key: MeetingPreparationKey) -> None:
        self.dirty.discard(key)
        if (cancellation := self.pending.pop(key, None)) is not None:
            cancellation.set()

    def close(self) -> None:
        for key in list(self.pending):
            self.cancel(key)
        self.workers.shutdown(timeout=0)


@dataclass(frozen=True, slots=True)
class _SourceMaintenanceResult:
    pub_type: str
    tree_key: str
    changed: bool = False
    error: str = ""


class MeetingPreparationService(QObject):
    """Prepare one durable meeting aggregate for every caller and policy."""

    tree_changed = Signal(object, str, object)
    state_changed = Signal(object, str, object)
    progress = Signal(object, str, int)
    error = Signal(object, str, str)
    _resolve_requested = Signal(str, str, int, int, int, str, str, bool)
    _source_maintenance_ready = Signal(object, object, object)

    def __init__(
        self,
        publication_service: "JwpubService",
        store: MeetingTreeStore,
        cache_manager: MediaCacheManager,
        parent: QObject | None = None,
        *,
        content_policy: MeetingContentPolicy | None = None,
    ) -> None:
        super().__init__(parent)
        self._publication_service = publication_service
        self._policy = content_policy or getattr(
            publication_service, "content_policy", MeetingContentPolicy()
        )
        self._pending_commands: dict[MeetingPreparationKey, list[_QueuedPreparation]] = {}
        self._store = store
        self._cache_manager = cache_manager
        self._builder = MeetingTreeBuilder()
        self._today = date.today
        self._repair_update_required: set[tuple[MeetingPreparationKey, str]] = set()
        self._jobs: dict[MeetingPreparationKey, _PreparationJob] = {}
        # Downloads outlive metadata jobs; retain their owners until the queue settles.
        self._download_batches: dict[str, _MediaDownloadBatch] = {}
        cache_manager.prefetch_batch_changed.connect(self._on_download_batch_changed)
        self._generation = 0
        self._resolution_order = itertools.count()
        self._pending_resolutions: dict[tuple, _PendingResolution] = {}
        self._active_resolution: _PendingResolution | None = None
        self._pending_canonical_recoveries: dict[
            MeetingPreparationKey,
            dict[str, set[str]],
        ] = {}
        self._canonical_recovery_timer = QTimer(self)
        self._canonical_recovery_timer.setSingleShot(True)
        self._canonical_recovery_timer.setInterval(_CANONICAL_RECOVERY_BATCH_MS)
        self._canonical_recovery_timer.timeout.connect(self._flush_canonical_media_recoveries)
        self._stopped = False
        self._source_maintenance = _SourceMaintenance()
        self._source_maintenance_ready.connect(self._on_source_maintenance_ready)
        self.destroyed.connect(self._source_maintenance.close)
        weakref.finalize(self, self._source_maintenance.close)

        if hasattr(publication_service, "content_checked"):
            publication_service.content_checked.connect(self._on_content_checked)
        publication_service.mwb_ready.connect(self._on_mwb_ready)
        publication_service.wt_ready.connect(self._on_wt_ready)
        publication_service.cbs_ready.connect(self._on_cbs_ready)
        publication_service.context_progress.connect(self._on_progress)
        publication_service.context_error.connect(self._on_error)
        publication_service.context_load_finished.connect(self._on_load_finished)

        self._resolver_thread = QThread(self)
        self._resolver = MeetingMediaResolutionWorker()
        self._resolver.moveToThread(self._resolver_thread)
        self._resolver_thread.finished.connect(self._resolver.deleteLater)
        self._resolve_requested.connect(self._resolver.resolve)
        self._resolver.resolved.connect(self._on_media_resolved)
        self._resolver_thread.start()

    @property
    def policy(self) -> MeetingContentPolicy:
        return self._policy

    def prepare_meeting(
        self,
        key: MeetingPreparationKey,
        pub_type: str,
        *,
        force: bool = False,
        download_media: bool = False,
        automatic: bool = False,
    ) -> MeetingPreparationState:
        if pub_type not in {"mwb", "wt"}:
            raise ValueError(f"Unsupported meeting publication: {pub_type}")
        self.ensure_week(
            MeetingPreparationRequest(
                key=key,
                publications=frozenset({pub_type}),
                force_refresh=force,
                download_media=download_media,
                operation=MeetingContentOperation.UPDATE
                if force
                else MeetingContentOperation.PREPARE,
                origin=MeetingRequestOrigin.AUTOMATIC if automatic else MeetingRequestOrigin.MANUAL,
            )
        )
        return self.state(key, pub_type)

    def check_updates(self, key: MeetingPreparationKey) -> MeetingPreparationState:
        selected = frozenset(self.snapshots(key)) & {"mwb", "wt"}
        if selected:
            self.ensure_week(
                MeetingPreparationRequest(
                    key=key,
                    publications=selected,
                    operation=MeetingContentOperation.CHECK,
                    origin=MeetingRequestOrigin.MAINTENANCE,
                    priority=MeetingPreparationPriority.BACKGROUND,
                )
            )
        return self.state(key, "mwb")

    def maintain_prepared(self, key: MeetingPreparationKey) -> MeetingPreparationState:
        state = self.check_updates(key)
        self._schedule_source_maintenance(key)
        return state

    def _schedule_source_maintenance(self, key: MeetingPreparationKey) -> None:
        lifetime = self._source_maintenance
        if self._stopped:
            return
        if key in lifetime.pending:
            lifetime.dirty.add(key)
            return
        snapshots = self.snapshots(key)
        if not snapshots:
            return
        cancellation = CancellationFlag()
        lifetime.pending[key] = cancellation
        # Keep CHECK and its caller's download-grant attachment in this GUI turn.
        QTimer.singleShot(0, self, lambda: self._start_source_maintenance(key, cancellation, snapshots))

    def _start_source_maintenance(
        self, key: MeetingPreparationKey, cancellation: CancellationFlag,
        snapshots: dict[str, MeetingTreeSnapshot],
    ) -> None:
        lifetime = self._source_maintenance
        if self._stopped or lifetime.pending.get(key) is not cancellation:
            return
        store = self._store
        owner_ref = weakref.ref(self)

        def work() -> None:
            results = []
            for pub_type, previous in snapshots.items():
                if cancellation.is_set():
                    return
                try:
                    snapshot = store.repair_canonical_image_sources(previous.tree_key)
                    results.append(_SourceMaintenanceResult(
                        pub_type, previous.tree_key,
                        changed=bool(snapshot and snapshot.revision != previous.revision),
                    ))
                except (OSError, UnicodeError, ValueError) as exc:
                    results.append(_SourceMaintenanceResult(pub_type, previous.tree_key, error=str(exc)))

            def deliver() -> None:
                owner = owner_ref()
                if owner is not None and shiboken6.isValid(owner):
                    try:
                        owner._source_maintenance_ready.emit(key, cancellation, results)
                    except RuntimeError:
                        log.debug("Meeting source-maintenance owner was destroyed before delivery")

            cancellation.run_if_active(deliver)

        if lifetime.workers.submit("solin-meeting-sources", work) is None:
            lifetime.cancel(key)

    @Slot(object, object, object)
    def _on_source_maintenance_ready(
        self, key: MeetingPreparationKey, cancellation: CancellationFlag,
        results: list[_SourceMaintenanceResult],
    ) -> None:
        if self._source_maintenance.pending.get(key) is not cancellation:
            return
        self._source_maintenance.pending.pop(key)
        rerun = key in self._source_maintenance.dirty
        self._source_maintenance.dirty.discard(key)
        cutoff, latest = meeting_week_bounds(self._today())
        if self._stopped or cancellation.is_set() or not cutoff <= key.monday <= latest:
            return
        for result in results:
            if result.error:
                self.error.emit(key, result.pub_type, result.error)
                continue
            # Read the authoritative in-memory revision: operator writes may have
            # completed after normalization, and active sync owns its bindings.
            snapshot = self._store.cached_snapshot(result.tree_key)
            if snapshot is None or snapshot.linked_sync.get("enabled"):
                continue
            if result.changed:
                self.tree_changed.emit(key, result.pub_type, snapshot)
            if self._policy.auto_recover_images and self._missing_image_refs(snapshot):
                self._request_repair(key, result.pub_type, MeetingRequestOrigin.MAINTENANCE)
        if rerun:
            self._schedule_source_maintenance(key)

    def repair_meeting(self, key: MeetingPreparationKey, pub_type: str) -> MeetingPreparationState:
        return self._request_repair(key, pub_type, MeetingRequestOrigin.MANUAL)

    def _request_repair(self, key, pub_type, origin) -> MeetingPreparationState:
        if self.snapshot(key, pub_type) is not None:
            self.ensure_week(
                MeetingPreparationRequest(
                    key=key,
                    publications=frozenset({pub_type}),
                    operation=MeetingContentOperation.REPAIR,
                    origin=origin,
                )
            )
        return self.state(key, pub_type)

    def _repair_saved_image_sources(
        self, key: MeetingPreparationKey, pub_type: str
    ) -> MeetingTreeSnapshot | None:
        previous = self.snapshot(key, pub_type)
        if previous is None:
            return None
        if not previous.image_source_repair_required:
            return previous
        try:
            snapshot = self._store.repair_canonical_image_sources(previous.tree_key)
        except (OSError, UnicodeError, ValueError) as exc:
            self.error.emit(key, pub_type, str(exc))
            return None
        if snapshot is not None and snapshot.revision != previous.revision:
            self.tree_changed.emit(key, pub_type, snapshot)
        return snapshot

    @staticmethod
    def _missing_image_refs(snapshot: MeetingTreeSnapshot) -> list[dict[str, Any]]:
        from .tree_types import iter_nodes

        if snapshot.linked_sync.get("enabled"):
            return []
        refs = []
        seen: set[tuple] = set()
        canonical = {
            str(node["meeting_source_key"]): node
            for node in iter_nodes(snapshot.canonical_nodes)
            if node.get("meeting_generated") and node.get("meeting_source_key")
            and node.get("type") == "media"
            and str((node.get("media_ref") or {}).get("mime_type") or "").startswith("image/")
        }
        surfaces = [
            snapshot.nodes,
            snapshot.canonical_nodes,
            list(snapshot.hidden_canonical_media.values()),
        ]
        for surface in surfaces:
            for node in iter_nodes(surface):
                ref = node.get("media_ref") or {}
                if not isinstance(ref, dict) or not str(ref.get("mime_type", "")).startswith(
                    "image/"
                ):
                    continue
                if not node.get("meeting_generated") and not ref.get("publication_source"):
                    continue
                source = ref.get("publication_source") or {}
                identity = (
                    str(node.get("meeting_source_key") or node.get("id") or ""),
                    str(source.get("pub") or ""), str(source.get("language") or ""),
                    str(source.get("issue") or ""), str(source.get("member") or ""),
                    str(ref.get("asset_hash") or ""),
                )
                source_key = str(node.get("meeting_source_key") or "")
                identity = (source_key,) if source_key else identity
                if identity in seen:
                    continue
                seen.add(identity)
                original = canonical.get(source_key)
                original_ref = original.get("media_ref") or {} if original is not None else {}
                original_path = str(original_ref.get("file_path") or "")
                if (
                    original_path and not original_path.startswith(("http://", "https://"))
                    and not Path(original_path).is_file()
                ):
                    refs.append(dict(original_ref))
                    continue
                path = str(node.get("resolved_url") or ref.get("file_path") or "")
                if path.startswith(("http://", "https://")) or (path and Path(path).is_file()):
                    continue
                if original is not None:
                    ref = original_ref
                    path = str(original.get("resolved_url") or ref.get("file_path") or "")
                    if path.startswith(("http://", "https://")) or (path and Path(path).is_file()):
                        continue
                refs.append(dict(ref))
        return refs

    @Slot(object, str, object)
    def _on_content_checked(
        self, raw_context: object, pub_type: str, raw_checksums: object
    ) -> None:
        if self._stopped or not isinstance(raw_context, tuple) or len(raw_context) != 4:
            return
        monday, language, sign, generation = raw_context
        key = MeetingPreparationKey(monday, language, sign)
        job = self._jobs.get(key)
        snapshot = self.snapshot(key, pub_type)
        if job is None or generation != job.generation or snapshot is None:
            return
        checksums = dict(raw_checksums) if isinstance(raw_checksums, dict) else {}
        try:
            self._store.observe_publications(snapshot.tree_key, checksums)
        except (OSError, UnicodeError, ValueError) as exc:
            job.errors[pub_type] = str(exc)
            self.error.emit(key, pub_type, str(exc))
            return
        if self._policy.auto_update and (
            self.state(key, pub_type).update_available or not snapshot.canonical_hash
        ):
            self._queue_automatic_update(key, {pub_type})
        self._emit_states(key)

    def _queue_automatic_update(self, key: MeetingPreparationKey, publications: set[str]) -> None:
        queue = self._pending_commands.setdefault(key, [])
        job = self._jobs.get(key)
        covered: set[str] = set()
        for command in queue:
            if command.request.operation != MeetingContentOperation.UPDATE:
                continue
            selected = publications & command.request.publications
            covered.update(selected)
            if job is not None:
                command.bind_downloads(job, selected)
        selected = publications - covered
        if selected:
            command = _QueuedPreparation(
                MeetingPreparationRequest(
                    key=key,
                    publications=frozenset(selected),
                    operation=MeetingContentOperation.UPDATE,
                    origin=MeetingRequestOrigin.MAINTENANCE,
                    priority=MeetingPreparationPriority.BACKGROUND,
                )
            )
            if job is not None:
                command.bind_downloads(job, selected)
            queue.append(command)

    def ensure_week(self, request: MeetingPreparationRequest) -> MeetingPreparationState:
        return self._ensure_week(request)

    def _ensure_week(
        self,
        request: MeetingPreparationRequest,
        *,
        downloads: dict[str, _MediaDownloadBatch] | None = None,
    ) -> MeetingPreparationState:
        if self._stopped:
            return self.state(request.key, "mwb")
        key = request.key
        selected = frozenset(request.publications) & {"mwb", "wt"}
        if request.operation == MeetingContentOperation.PREPARE and not request.force_refresh:
            prepared = selected & self.snapshots(key).keys()
            if prepared:
                self.maintain_prepared(key)
                maintenance = self._jobs.get(key)
                if request.download_media and maintenance is not None:
                    self._authorize_downloads(maintenance, prepared, request.origin)
                    if request.origin == MeetingRequestOrigin.MANUAL:
                        maintenance.origin = MeetingRequestOrigin.MANUAL
                    for pub_type in prepared:
                        snapshot = self.snapshot(key, pub_type)
                        if snapshot is not None:
                            self._schedule_snapshot_media(maintenance, pub_type, snapshot)
                selected -= prepared
                request = replace(request, publications=selected)
        if request.operation in {MeetingContentOperation.CHECK, MeetingContentOperation.REPAIR}:
            selected &= self.snapshots(key).keys()
        if request.operation == MeetingContentOperation.REPAIR:
            selected = frozenset(
                pub_type for pub_type in selected
                if (snapshot := self._repair_saved_image_sources(key, pub_type)) is not None
                and self._missing_image_refs(snapshot)
                and (self._policy.auto_recover_images or request.origin == MeetingRequestOrigin.MANUAL)
            )
        if not selected:
            return self.state(key, "mwb")
        existing = self._jobs.get(key)
        if existing is not None and existing.active:
            if (
                request.force_refresh
                or existing.operation != request.operation
                or not selected.issubset(existing.publications)
            ):
                queued = self._pending_commands.setdefault(key, [])
                command = next((item for item in queued if item.request == request), None)
                if command is None:
                    command = _QueuedPreparation(request)
                    queued.append(command)
                command.bind_downloads(existing, selected)
                return self.state(key, next(iter(selected)))
            if request.origin == MeetingRequestOrigin.MANUAL:
                existing.origin = MeetingRequestOrigin.MANUAL
            previous_priority = existing.priority
            existing.priority = max(existing.priority, request.priority)
            if existing.priority != previous_priority:
                self._promote_publication(existing)
            self._promote_resolutions(existing)
            if request.download_media:
                self._authorize_downloads(existing, selected, request.origin)
                self._prefetch_existing_snapshots(existing)
            return self.state(key, "mwb")

        self._generation += 1
        job = _PreparationJob(
            key=key,
            generation=self._generation,
            priority=request.priority,
            publications=selected,
            operation=request.operation,
            origin=request.origin,
        )
        if downloads is not None:
            job.downloads.update(downloads)
        elif existing is not None:
            # Only unresolved, already-authorized AV work survives an independent
            # metadata check. Finished downloads do not authorize future updates.
            job.downloads.update({
                pub_type: batch for pub_type, batch in existing.downloads.items()
                if any(
                    pending and tree_key.startswith(f"{pub_type}:")
                    for tree_key, pending in existing.pending_by_tree.items()
                )
            })
        if request.download_media:
            self._authorize_downloads(job, selected, request.origin)
        if any(batch.origin == MeetingRequestOrigin.MANUAL
               for pub_type, batch in job.downloads.items() if pub_type in selected):
            job.origin = MeetingRequestOrigin.MANUAL
        if existing is not None:
            self._discard_resolution_targets(key, existing.generation)
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
            known_wt_issue=wt_snapshot.issue if wt_snapshot is not None else "",
            content_request=PublicationContentRequest(
                publications=selected,
                operation=request.operation,
                dependencies={
                    pub_type: snapshot.publication_dependencies
                    or [
                        publication_dependency(
                            "mwb" if pub_type == "mwb" else "w",
                            key.language_code,
                            snapshot.issue,
                            snapshot.source_checksum,
                        )
                    ]
                    for pub_type, snapshot in snapshots.items()
                    if pub_type in selected
                },
                repair_refs=tuple(
                    ref
                    for pub_type, snapshot in snapshots.items()
                    if pub_type in selected
                    for ref in self._missing_image_refs(snapshot)
                ),
            ),
        )
        return self.state(key, "mwb")

    def recover_missing_canonical_media(
        self,
        key: MeetingPreparationKey,
        pub_type: str,
        source_path: str,
    ) -> MeetingPreparationState:
        """Batch an exact-content repair after official local media vanishes."""

        today = self._today()
        cutoff, _ = meeting_week_bounds(today)
        if (
            self._stopped
            or key.monday < cutoff
            or pub_type not in {"mwb", "wt"}
            or self.snapshot(key, pub_type) is None
        ):
            return self.state(key, "mwb")
        publications = self._pending_canonical_recoveries.setdefault(key, {})
        paths = publications.setdefault(pub_type, set())
        if source_path:
            paths.add(source_path)
        if not self._canonical_recovery_timer.isActive():
            self._canonical_recovery_timer.start()
        return self.state(key, "mwb")

    @Slot()
    def _flush_canonical_media_recoveries(self) -> None:
        self._canonical_recovery_timer.stop()
        if self._stopped:
            self._pending_canonical_recoveries.clear()
            return
        pending = self._pending_canonical_recoveries
        self._pending_canonical_recoveries = {}
        for key, publications in pending.items():
            revisions = {
                pub_type: snapshot.revision
                for pub_type in publications
                if (snapshot := self.snapshot(key, pub_type)) is not None
            }
            self.ensure_week(
                MeetingPreparationRequest(
                    key=key,
                    publications=frozenset(publications),
                    operation=MeetingContentOperation.REPAIR,
                    origin=MeetingRequestOrigin.MAINTENANCE,
                )
            )
            for pub_type in publications:
                snapshot = self.snapshot(key, pub_type)
                if (
                    snapshot is not None and not snapshot.linked_sync.get("enabled")
                    and snapshot.revision == revisions.get(pub_type)
                ):
                    self.tree_changed.emit(key, pub_type, snapshot)

    def prune_expired(self) -> int:
        """Retire expired jobs before removing their persisted meeting owners."""
        cutoff, _ = meeting_week_bounds(self._today())
        for key in list(self._source_maintenance.pending):
            if key.monday < cutoff:
                self._source_maintenance.cancel(key)
        self._cancel_download_batches(lambda batch: batch.key.monday < cutoff)
        for key, job in list(self._jobs.items()):
            if key.monday >= cutoff:
                continue
            self._pending_commands.pop(key, None)
            self._pending_canonical_recoveries.pop(key, None)
            self._publication_service.cancel_pending_week(
                key.monday,
                language_code=key.language_code,
                is_sign_language=key.is_sign_language,
                generation=job.generation,
            )
            self._discard_resolution_targets(key, job.generation)
            self._jobs.pop(key, None)
            self._publication_service.clear_week(key.monday)
        return self._store.prune_before(cutoff)

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
        source_status = job.terminal_statuses.get(pub_type, "") if job is not None else ""
        refreshing = bool(job and job.active and pub_type in job.publications)
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
            bool(
                snapshot
                and any(
                    dep.get("checksum")
                    and (
                        observed := snapshot.observed_publication_checksums.get(
                            ":".join(
                                (dep.get("pub", ""), dep.get("language", ""), dep.get("issue", ""))
                            )
                        )
                    )
                    and observed != dep["checksum"]
                    for dep in (
                        snapshot.publication_dependencies
                        or [
                            publication_dependency(
                                "mwb" if pub_type == "mwb" else "w",
                                key.language_code,
                                snapshot.issue,
                                snapshot.source_checksum,
                            )
                        ]
                    )
                )
            ),
            bool(snapshot and (
                snapshot.image_source_repair_required or self._missing_image_refs(snapshot)
            )),
            (key, pub_type) in self._repair_update_required
            and bool(snapshot and self._missing_image_refs(snapshot)),
        )

    def cancel_automatic_downloads(self) -> None:
        self._cancel_download_batches(lambda batch: batch.origin != MeetingRequestOrigin.MANUAL)
        for key, queued in list(self._pending_commands.items()):
            retained = []
            for command in queued:
                request = command.request
                if request.origin == MeetingRequestOrigin.AUTOMATIC:
                    continue
                if request.origin != MeetingRequestOrigin.MANUAL:
                    command.request = replace(request, download_media=False)
                retained.append(command)
            self._pending_commands[key] = retained
        for job in list(self._jobs.values()):
            if job.origin == MeetingRequestOrigin.AUTOMATIC:
                self._publication_service.cancel_pending_week(
                    job.key.monday,
                    language_code=job.key.language_code,
                    is_sign_language=job.key.is_sign_language,
                    generation=job.generation,
                )
                job.active = False
                job.completed_publications.update(job.publications)
                self._finish_or_revalidate(job)
                self._emit_states(job.key)

    def cancel_other_language_contexts(
        self,
        context: JWMediaLanguageContext,
    ) -> None:
        """Retire obsolete language work without touching the active context."""
        language = (context.api_code or "T").strip() or "T"
        is_sign = bool(context.is_sign_language)
        for key in list(self._source_maintenance.pending):
            if (key.language_code, key.is_sign_language) != (language, is_sign):
                self._source_maintenance.cancel(key)
        self._cancel_download_batches(
            lambda batch: (batch.key.language_code, batch.key.is_sign_language) != (language, is_sign)
        )
        pending_contexts = self._pending_commands.keys() | self._pending_canonical_recoveries.keys()
        for key in pending_contexts:
            if (key.language_code, key.is_sign_language) != (language, is_sign):
                self._pending_commands.pop(key, None)
                self._pending_canonical_recoveries.pop(key, None)
        for key, job in list(self._jobs.items()):
            if (key.language_code, key.is_sign_language) == (language, is_sign):
                continue
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
        self._source_maintenance.close()
        self._source_maintenance.workers.shutdown(timeout=max(0, wait_ms) / 1000)
        self._canonical_recovery_timer.stop()
        self._pending_canonical_recoveries.clear()
        self._pending_commands.clear()
        self.cancel_automatic_downloads()
        self._cancel_download_batches(lambda _batch: True)
        for signal, callback in (
            (self._publication_service.mwb_ready, self._on_mwb_ready),
            (self._publication_service.wt_ready, self._on_wt_ready),
            (self._publication_service.cbs_ready, self._on_cbs_ready),
            (self._publication_service.context_progress, self._on_progress),
            (self._publication_service.context_error, self._on_error),
            (self._publication_service.context_load_finished, self._on_load_finished),
            (self._resolver.resolved, self._on_media_resolved),
            (self._cache_manager.prefetch_batch_changed, self._on_download_batch_changed),
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
        if (
            job is None
            or not job.active
            or job.operation in {MeetingContentOperation.CHECK, MeetingContentOperation.REPAIR}
            or pub_type not in job.publications
            or wd.request_generation != job.generation
        ):
            return
        status = wd.mwb_status if pub_type == "mwb" else wd.wt_status
        job.terminal_statuses[pub_type] = status
        if status == "ready":
            self._reconcile_publication(job, pub_type, wd)
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
        canonical_complete = pub_type != "mwb" or wd.cbs_status in {"idle", "ready", "empty"}
        try:
            snapshot = self._store.reconcile(
                tree_key,
                canonical,
                self._builder.canonical_hash(canonical),
                overview,
                fallback=fallback,
                source_checksum=(
                    wd.mwb_source_checksum if pub_type == "mwb" else wd.wt_source_checksum
                ),
                canonical_complete=canonical_complete,
                publication_dependencies=(
                    wd.mwb_dependencies if pub_type == "mwb" else wd.wt_dependencies
                ),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            message = str(exc)
            job.errors[pub_type] = message
            self.error.emit(job.key, pub_type, message)
            return
        job.errors.pop(pub_type, None)
        self._repair_update_required.discard((job.key, pub_type))
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
        for node, automatic_download in _iter_media_with_download_eligibility(snapshot.nodes):
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
            if self._node_media_type(node) == "image":
                continue
            if url and (not has_jw_identity or self._has_required_metadata(node)):
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
        self._prefetch_urls(job, pub_type, ready_urls)

    @staticmethod
    def _remote_url(node: Node) -> str:
        return _remote_media_url(node)

    @staticmethod
    def _node_media_type(node: Node) -> str:
        media_type = str(node.get("media_type") or "").strip().casefold()
        if media_type:
            return media_type
        ref = node.get("media_ref") or {}
        mime_type = str(ref.get("mime_type") or "") if isinstance(ref, dict) else ""
        return media_type_for_mime_type(mime_type)

    @staticmethod
    def _has_required_metadata(node: Node) -> bool:
        if MeetingPreparationService._node_media_type(node) == "image":
            return True
        try:
            has_duration = int(node.get("base_duration_ticks") or 0) > 0
        except (TypeError, ValueError):
            return False
        has_thumbnail = bool(node.get("thumbnail_url") or node.get("thumbnail_local_path"))
        return has_duration and has_thumbnail

    @staticmethod
    def _resolution_signature(
        key: MeetingPreparationKey,
        ref: dict[str, Any],
    ) -> tuple:
        return (
            key.language_code,
            key.is_sign_language,
            media_type_for_mime_type(str(ref.get("mime_type") or "")),
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
        if self._stopped or self._active_resolution is not None or not self._pending_resolutions:
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
            str(signature[2]),
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
            effective_url = url or target.fallback_url
            if effective_url or title or thumbnail or duration_ticks:
                patch: dict[str, Any] = {}
                if url:
                    patch["resolved_url"] = url
                if title:
                    patch["title"] = title
                    patch["auto_title"] = False
                    patch["media_ref_label"] = title
                if thumbnail:
                    patch["thumbnail_url"] = thumbnail
                    patch["thumbnail_binding"] = "jw_artwork"
                if duration_ticks:
                    patch["base_duration_ticks"] = duration_ticks
                job.patches_by_tree.setdefault(target.tree_key, {})[target.node_id] = (
                    target.identity,
                    patch,
                )
            elif target.fallback_url:
                batch = job.downloads.get(target.pub_type)
                if batch is not None:
                    batch.prefetched_urls.discard(target.fallback_url)
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
                self._prefetch_urls(job, pub_type, _automatic_download_urls(snapshot.nodes))
            self._emit_states(key)
        self._dispatch_resolution()

    def _prefetch_existing_snapshots(self, job: _PreparationJob) -> None:
        for pub_type, snapshot in self.snapshots(job.key).items():
            if pub_type in job.publications or pub_type in job.downloads:
                self._schedule_snapshot_media(job, pub_type, snapshot)

    def _authorize_downloads(
        self, job: _PreparationJob, publications: frozenset[str] | set[str],
        origin: MeetingRequestOrigin,
    ) -> None:
        for pub_type in publications:
            batch = job.downloads.get(pub_type)
            if batch is None:
                job.downloads[pub_type] = _MediaDownloadBatch(job.key, pub_type, origin)
            elif origin == MeetingRequestOrigin.MANUAL:
                batch.origin = origin
        for command in self._pending_commands.get(job.key, []):
            command.bind_downloads(job, publications)

    @Slot(str, int, int, int, int)
    def _on_download_batch_changed(
        self, batch_id: str, queued: int, active: int, _done: int, _failed: int
    ) -> None:
        if not queued and not active:
            self._download_batches.pop(batch_id, None)

    def _cancel_download_batches(self, predicate: Callable[[_MediaDownloadBatch], bool]) -> None:
        for job in self._jobs.values():
            job.downloads = {
                pub_type: batch for pub_type, batch in job.downloads.items() if not predicate(batch)
            }
        for queued in self._pending_commands.values():
            for command in queued:
                for pub_type, source in command.download_sources.items():
                    batch = source.downloads.get(pub_type)
                    if batch is not None and predicate(batch):
                        source.downloads.pop(pub_type, None)
        for batch_id, batch in list(self._download_batches.items()):
            if predicate(batch):
                self._download_batches.pop(batch_id, None)
                self._cache_manager.cancel_batch(batch_id)

    def _prefetch_urls(self, job: _PreparationJob, pub_type: str, urls: set[str]) -> None:
        batch = job.downloads.get(pub_type)
        if batch is None:
            return
        candidates = sorted(url for url in urls if url and url not in batch.prefetched_urls)
        if not candidates:
            return
        self._download_batches[batch.batch_id] = batch
        self._cache_manager.prefetch_many(candidates, batch.batch_id)
        batch.prefetched_urls.update(candidates)

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
        if (
            job is None
            or job.generation != generation
            or not job.active
            or pub_type not in job.publications
        ):
            return
        if message in {
            MeetingContentError.IMAGE_REVISION_UNAVAILABLE,
            MeetingContentError.LEGACY_REVISION_UNVERIFIABLE,
        }:
            self._repair_update_required.add((key, pub_type))
            if job.operation == MeetingContentOperation.REPAIR:
                job.repair_update_publications.add(pub_type)
            message = "Image repair requires updating this meeting"
        job.errors[pub_type] = message
        job.terminal_statuses[pub_type] = "not_found" if message == "NOT_FOUND" else "error"
        job.completed_publications.add(pub_type)
        self.error.emit(key, pub_type, message)
        if job.operation != MeetingContentOperation.REPAIR:
            self._finish_or_revalidate(job)
        self._emit_states(key)

    @Slot(str, str, bool, int)
    def _on_load_finished(
        self,
        monday_text: str,
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
        if job is None or job.generation != generation or not job.active:
            return
        if job.operation == MeetingContentOperation.REPAIR:
            for pub_type in job.publications:
                self._repair_saved_image_sources(key, pub_type)
            for pub_type in job.publications - job.errors.keys():
                self._repair_update_required.discard((key, pub_type))
            if self._policy.auto_update and job.repair_update_publications:
                self._queue_automatic_update(key, job.repair_update_publications)
            job.repair_update_publications.clear()
        job.completed_publications.update(job.publications)
        self._finish_or_revalidate(job)
        self._emit_states(key)

    def _finish_or_revalidate(self, job: _PreparationJob) -> None:
        if not job.publications.issubset(job.completed_publications):
            return
        job.active = False
        queued = self._pending_commands.get(job.key, [])
        if queued:
            command = queued.pop(0)
            downloads = {
                pub_type: batch for pub_type, source in command.download_sources.items()
                if (batch := source.downloads.get(pub_type)) is not None
            }
            self._ensure_week(command.request, downloads=downloads)
            return

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
