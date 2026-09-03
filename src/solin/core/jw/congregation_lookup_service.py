"""Threaded adapter that keeps congregation lookups off the GUI thread."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from solin.core.jw.congregation_lookup import (
    CongregationMatch,
    classify_failure,
    fetch_meeting_schedule,
    search_congregations,
)

log = logging.getLogger(__name__)


class _LookupTask(QRunnable):
    """Run one lookup and report it on the long-lived service.

    The service owns the signals on purpose: a per-task emitter dies with the
    auto-deleted runnable, and a queued emission whose receiver is gone is
    dropped, which silently loses results.
    """

    def __init__(
        self,
        service: "CongregationLookupService",
        revision: int,
        work: Callable[[], Any],
    ) -> None:
        super().__init__()
        self._service = service
        self._revision = revision
        self._work = work
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            result = self._work()
        except Exception as exc:  # noqa: BLE001 - QRunnable reports all failures via signal
            log.warning("[CongregationLookupService] jw.org lookup failed: %s", exc)
            self._service.raised.emit(self._revision, exc)
            return
        self._service.completed.emit(self._revision, result)


class CongregationLookupService(QObject):
    """Search congregations and resolve their published meeting schedule."""

    suggestions_ready = Signal(list)   # list[CongregationMatch]
    schedule_ready = Signal(object)    # MeetingSchedule | None
    failed = Signal(str)               # RATE_LIMITED or UNAVAILABLE

    # Worker-thread reports, delivered on the GUI thread.
    completed = Signal(int, object)
    raised = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._revision = 0
        self._handlers: dict[int, Callable[[Any], None]] = {}
        self._cache: dict[str, list[CongregationMatch]] = {}
        self.completed.connect(self._on_completed)
        self.raised.connect(self._on_raised)

    def search(self, name: str) -> None:
        revision = self._next_revision()
        query = name.strip()
        if not query:
            self.suggestions_ready.emit([])
            return
        cached = self._cache.get(query.casefold())
        if cached is not None:
            self.suggestions_ready.emit(list(cached))
            return
        self._start(
            revision,
            lambda: search_congregations(query),
            lambda matches: self._accept_suggestions(query, matches),
        )

    def fetch_schedule(self, guid: str) -> None:
        self._start(
            self._next_revision(),
            lambda: fetch_meeting_schedule(guid),
            self.schedule_ready.emit,
        )

    def shutdown(self) -> None:
        self._next_revision()
        self._handlers.clear()
        self._pool.clear()
        self._pool.waitForDone()

    def _next_revision(self) -> int:
        """Invalidate whatever is in flight; only the newest request may report."""

        self._revision += 1
        return self._revision

    def _start(
        self,
        revision: int,
        work: Callable[[], Any],
        on_success: Callable[[Any], None],
    ) -> None:
        self._handlers[revision] = on_success
        self._pool.start(_LookupTask(self, revision, work))

    def _on_completed(self, revision: int, result: Any) -> None:
        handler = self._handlers.pop(revision, None)
        if handler is not None and revision == self._revision:
            handler(result)

    def _on_raised(self, revision: int, error: Exception) -> None:
        self._handlers.pop(revision, None)
        if revision == self._revision:
            self.failed.emit(classify_failure(error))

    def _accept_suggestions(
        self,
        query: str,
        matches: list[CongregationMatch],
    ) -> None:
        self._cache[query.casefold()] = list(matches)
        self.suggestions_ready.emit(matches)


__all__ = ["CongregationLookupService"]
