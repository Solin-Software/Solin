"""Application services for media playback and cache coordination."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import time
from typing import Literal, Protocol

MAX_CONCURRENT_PREFETCHES = 3
PREFETCH_RETRY_LIMIT = 1
MAX_BATCH_PREPROGRESS_FAILURES = 3
PREFETCH_PROGRESS_MIN_INTERVAL_SECONDS = 0.25
PREFETCH_PROGRESS_MIN_BYTES = 512 * 1024

PrefetchActionKind = Literal[
    "queued",
    "dequeued",
    "cache_changed",
    "progress",
    "error",
    "batch_changed",
    "batch_error",
    "cancel_active",
    "start",
]


class MediaCacheLookup(Protocol):
    def is_remote(self, url: str) -> bool: ...

    def is_cached(self, url: str) -> bool: ...


@dataclass(frozen=True)
class QueuedPrefetch:
    url: str
    batch_id: str = ""
    retries: int = 0


@dataclass
class ActivePrefetch:
    batch_id: str = ""
    retries: int = 0
    had_progress: bool = False
    last_progress_emit_at: float = 0.0
    last_progress_pct: int = -1
    last_progress_bytes: int = 0


@dataclass
class BatchState:
    total: int = 0
    queued: set[str] = field(default_factory=set)
    active: set[str] = field(default_factory=set)
    done: int = 0
    failed: int = 0
    consecutive_preprogress_failures: int = 0
    canceled: bool = False


@dataclass(frozen=True)
class PrefetchAction:
    kind: PrefetchActionKind
    url: str = ""
    batch_id: str = ""
    message: str = ""
    entry: QueuedPrefetch | None = None
    downloaded: int = 0
    total: int = 0


@dataclass
class PrefetchPlan:
    actions: list[PrefetchAction] = field(default_factory=list)
    added: int = 0

    def add(
        self,
        kind: PrefetchActionKind,
        *,
        url: str = "",
        batch_id: str = "",
        message: str = "",
        entry: QueuedPrefetch | None = None,
        downloaded: int = 0,
        total: int = 0,
    ) -> None:
        self.actions.append(
            PrefetchAction(
                kind,
                url=url,
                batch_id=batch_id,
                message=message,
                entry=entry,
                downloaded=downloaded,
                total=total,
            )
        )

    def extend(self, other: "PrefetchPlan") -> None:
        self.actions.extend(other.actions)
        self.added += other.added


class MediaPrefetchQueue:
    """Pure queue and batch coordinator for remote media prefetching."""

    def __init__(
        self,
        cache_lookup: MediaCacheLookup,
        *,
        max_concurrent: int = MAX_CONCURRENT_PREFETCHES,
        retry_limit: int = PREFETCH_RETRY_LIMIT,
        max_preprogress_failures: int = MAX_BATCH_PREPROGRESS_FAILURES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache_lookup = cache_lookup
        self.max_concurrent = max_concurrent
        self._retry_limit = retry_limit
        self._max_preprogress_failures = max_preprogress_failures
        self._clock = clock
        self._queue: deque[QueuedPrefetch] = deque()
        self._queued: dict[str, QueuedPrefetch] = {}
        self._active: dict[str, ActivePrefetch] = {}
        self._batches: dict[str, BatchState] = {}
        self._batch_signal_suppressed = 0
        self._dirty_batches: set[str] = set()

    def is_prefetching(self, url: str) -> bool:
        return url in self._active

    def is_queued(self, url: str) -> bool:
        return url in self._queued

    def batch_counts(self, batch_id: str) -> tuple[int, int, int, int]:
        batch = self._batches.get(batch_id)
        if not batch:
            return 0, 0, 0, 0
        return len(batch.queued), len(batch.active), batch.done, batch.failed

    def batch_is_active(self, batch_id: str) -> bool:
        batch = self._batches.get(batch_id)
        return bool(batch and (batch.queued or batch.active))

    def prefetch(self, url: str, *, priority: bool = False) -> PrefetchPlan:
        plan = PrefetchPlan()
        self._enqueue(plan, url, priority=priority)
        plan.extend(self.pump())
        return plan

    def prefetch_many(self, urls: list[str], batch_id: str) -> PrefetchPlan:
        plan = PrefetchPlan()
        if not batch_id:
            return plan

        seen: set[str] = set()
        batch = self._batches.setdefault(batch_id, BatchState())
        batch.canceled = False

        self._batch_signal_suppressed += 1
        try:
            for url in urls:
                if url in seen:
                    continue
                seen.add(url)
                if self._enqueue(plan, url, batch_id=batch_id):
                    plan.added += 1
                    batch.total += 1
        finally:
            self._batch_signal_suppressed = max(0, self._batch_signal_suppressed - 1)
        self._dirty_batches.add(batch_id)
        self._flush_deferred_batch_signals(plan)
        plan.extend(self.pump())
        return plan

    def cancel_prefetch(self, url: str) -> PrefetchPlan:
        plan = PrefetchPlan()
        queued = self._queued.pop(url, None)
        if queued is not None:
            self._queue = deque(entry for entry in self._queue if entry.url != url)
            self._remove_from_batch(plan, queued.batch_id, url, was_queued=True)
            plan.add("dequeued", url=url)
            return plan

        active = self._active.pop(url, None)
        if active is not None:
            self._remove_from_batch(plan, active.batch_id, url, was_active=True)
            plan.add("cancel_active", url=url)
            plan.add("dequeued", url=url)
            plan.extend(self.pump())
        return plan

    def cancel_all(self) -> PrefetchPlan:
        plan = PrefetchPlan()
        for url in list(self._queued.keys()):
            plan.extend(self.cancel_prefetch(url))
        for url in list(self._active.keys()):
            plan.extend(self.cancel_prefetch(url))
        return plan

    def cancel_batch(self, batch_id: str) -> PrefetchPlan:
        plan = PrefetchPlan()
        batch = self._batches.get(batch_id)
        if not batch:
            return plan
        batch.canceled = True
        for url in list(batch.queued):
            plan.extend(self.cancel_prefetch(url))
        for url in list(batch.active):
            plan.extend(self.cancel_prefetch(url))
        self._emit_batch_changed(plan, batch_id)
        return plan

    def notify_cached(self, url: str) -> PrefetchPlan:
        plan = PrefetchPlan()
        active = self._active.pop(url, None)
        if active is not None:
            self._mark_batch_done(plan, active.batch_id, url)
        queued = self._queued.pop(url, None)
        if queued is not None:
            self._queue = deque(entry for entry in self._queue if entry.url != url)
            self._remove_from_batch(plan, queued.batch_id, url, was_queued=True)
            plan.add("dequeued", url=url)
        if url:
            plan.add("cache_changed", url=url)
        plan.extend(self.pump())
        return plan

    def pump(self) -> PrefetchPlan:
        plan = PrefetchPlan()
        while self._queue and len(self._active) < self.max_concurrent:
            entry = self._queue.popleft()
            if self._queued.pop(entry.url, None) is None:
                continue
            if self._cache_lookup.is_cached(entry.url):
                self._mark_batch_done(plan, entry.batch_id, entry.url)
                plan.add("cache_changed", url=entry.url)
                plan.add("dequeued", url=entry.url)
                continue
            if entry.url in self._active:
                continue
            self._start_entry(plan, entry)
        return plan

    def start_progress(self, url: str, downloaded: int, total: int) -> PrefetchPlan:
        plan = PrefetchPlan()
        active = self._active.get(url)
        if active is not None and downloaded > 0:
            active.had_progress = True
            batch = self._batches.get(active.batch_id)
            if batch is not None:
                batch.consecutive_preprogress_failures = 0
        if active is None or self._should_emit_progress(active, downloaded, total):
            plan.add("progress", url=url, downloaded=downloaded, total=total)
        return plan

    def complete(self, url: str) -> PrefetchPlan:
        plan = PrefetchPlan()
        active = self._active.pop(url, None)
        if active is not None:
            self._mark_batch_done(plan, active.batch_id, url)
        plan.add("cache_changed", url=url)
        plan.extend(self.pump())
        return plan

    def fail(self, url: str, msg: str) -> PrefetchPlan:
        plan = PrefetchPlan()
        active = self._active.pop(url, None)
        if active is None:
            return plan

        self._remove_from_batch(plan, active.batch_id, url, was_active=True)

        if self._is_fatal_cache_error(msg):
            self._mark_batch_failed(
                plan,
                active.batch_id,
                url,
                preprogress=not active.had_progress,
            )
            plan.add("error", url=url, message=msg)
            if active.batch_id:
                self._abort_batch(plan, active.batch_id, msg)
            plan.extend(self.pump())
            return plan

        if active.retries < self._retry_limit:
            self._enqueue(
                plan,
                url,
                batch_id=active.batch_id,
                priority=True,
                retries=active.retries + 1,
            )
            plan.extend(self.pump())
            return plan

        self._mark_batch_failed(
            plan,
            active.batch_id,
            url,
            preprogress=not active.had_progress,
        )
        plan.add("error", url=url, message=msg)
        plan.extend(self.pump())
        return plan

    def _enqueue(
        self,
        plan: PrefetchPlan,
        url: str,
        *,
        batch_id: str = "",
        priority: bool = False,
        retries: int = 0,
    ) -> bool:
        if not self._cache_lookup.is_remote(url):
            return False
        if self._cache_lookup.is_cached(url):
            plan.add("cache_changed", url=url)
            return False
        if url in self._active:
            return False
        if url in self._queued:
            if priority:
                self._promote_queued(plan, url)
            return False

        entry = QueuedPrefetch(url=url, batch_id=batch_id, retries=retries)
        if priority:
            self._queue.appendleft(entry)
        else:
            self._queue.append(entry)
        self._queued[url] = entry

        if batch_id:
            batch = self._batches.setdefault(batch_id, BatchState())
            batch.queued.add(url)
            self._emit_batch_changed(plan, batch_id)

        plan.add("queued", url=url)
        return True

    def _promote_queued(self, plan: PrefetchPlan, url: str) -> None:
        entry = self._queued.get(url)
        if entry is None:
            return
        self._queue = deque(item for item in self._queue if item.url != url)
        self._queue.appendleft(entry)
        plan.add("queued", url=url)

    def _start_entry(self, plan: PrefetchPlan, entry: QueuedPrefetch) -> None:
        self._active[entry.url] = ActivePrefetch(
            batch_id=entry.batch_id,
            retries=entry.retries,
        )
        plan.add("dequeued", url=entry.url)

        if entry.batch_id:
            batch = self._batches.setdefault(entry.batch_id, BatchState())
            batch.queued.discard(entry.url)
            batch.active.add(entry.url)
            self._emit_batch_changed(plan, entry.batch_id)

        plan.add("start", url=entry.url, entry=entry)

    def _mark_batch_done(self, plan: PrefetchPlan, batch_id: str, url: str) -> None:
        if not batch_id:
            return
        batch = self._batches.setdefault(batch_id, BatchState())
        batch.queued.discard(url)
        batch.active.discard(url)
        batch.done += 1
        batch.consecutive_preprogress_failures = 0
        self._emit_batch_changed(plan, batch_id)

    def _mark_batch_failed(
        self,
        plan: PrefetchPlan,
        batch_id: str,
        url: str,
        *,
        preprogress: bool,
    ) -> None:
        if not batch_id:
            return
        batch = self._batches.setdefault(batch_id, BatchState())
        batch.queued.discard(url)
        batch.active.discard(url)
        batch.failed += 1
        if preprogress:
            batch.consecutive_preprogress_failures += 1
        else:
            batch.consecutive_preprogress_failures = 0
        self._emit_batch_changed(plan, batch_id)
        if batch.consecutive_preprogress_failures >= self._max_preprogress_failures:
            self._abort_batch(
                plan,
                batch_id,
                "Several downloads failed before receiving data. Check your connection.",
            )

    def _remove_from_batch(
        self,
        plan: PrefetchPlan,
        batch_id: str,
        url: str,
        *,
        was_queued: bool = False,
        was_active: bool = False,
    ) -> None:
        if not batch_id:
            return
        batch = self._batches.get(batch_id)
        if not batch:
            return
        if was_queued:
            batch.queued.discard(url)
        if was_active:
            batch.active.discard(url)
        self._emit_batch_changed(plan, batch_id)

    def _abort_batch(self, plan: PrefetchPlan, batch_id: str, msg: str) -> None:
        batch = self._batches.get(batch_id)
        if not batch or batch.canceled:
            return
        batch.canceled = True
        for queued_url in list(batch.queued):
            queued = self._queued.pop(queued_url, None)
            if queued is not None:
                self._queue = deque(
                    entry for entry in self._queue if entry.url != queued_url
                )
                plan.add("dequeued", url=queued_url)
        batch.queued.clear()
        for active_url in list(batch.active):
            active = self._active.pop(active_url, None)
            if active is not None:
                plan.add("cancel_active", url=active_url)
                plan.add("dequeued", url=active_url)
        batch.active.clear()
        plan.add("batch_error", batch_id=batch_id, message=msg)
        self._emit_batch_changed(plan, batch_id)

    def _emit_batch_changed(self, plan: PrefetchPlan, batch_id: str) -> None:
        if not batch_id:
            return
        if self._batch_signal_suppressed > 0:
            self._dirty_batches.add(batch_id)
            return
        plan.add("batch_changed", batch_id=batch_id)

    def _flush_deferred_batch_signals(self, plan: PrefetchPlan) -> None:
        if self._batch_signal_suppressed > 0:
            return
        dirty = list(self._dirty_batches)
        self._dirty_batches.clear()
        for batch_id in dirty:
            self._emit_batch_changed(plan, batch_id)

    @staticmethod
    def _is_fatal_cache_error(msg: str) -> bool:
        lowered = (msg or "").lower()
        fatal_markers = (
            "no space",
            "not enough space",
            "disk full",
            "quota",
            "permission denied",
            "access is denied",
            "winerror 112",
            "errno 28",
            "errno 13",
        )
        return any(marker in lowered for marker in fatal_markers)

    def _should_emit_progress(
        self,
        active: ActivePrefetch,
        downloaded: int,
        total: int,
    ) -> bool:
        now = self._clock()
        if total > 0:
            pct = int(downloaded * 100 / total)
            if pct != active.last_progress_pct:
                active.last_progress_pct = pct
                active.last_progress_emit_at = now
                active.last_progress_bytes = downloaded
                return True
            return False

        if active.last_progress_emit_at <= 0:
            active.last_progress_emit_at = now
            active.last_progress_bytes = downloaded
            return True
        if downloaded - active.last_progress_bytes >= PREFETCH_PROGRESS_MIN_BYTES:
            active.last_progress_emit_at = now
            active.last_progress_bytes = downloaded
            return True
        if now - active.last_progress_emit_at >= PREFETCH_PROGRESS_MIN_INTERVAL_SECONDS:
            active.last_progress_emit_at = now
            active.last_progress_bytes = downloaded
            return True
        return False
