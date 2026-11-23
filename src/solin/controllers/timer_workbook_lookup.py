"""Background workbook lookups for the timer's currently visible week."""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from datetime import date
import logging
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, Qt, Signal, Slot

from solin.core.foundation.thread_workers import CancellationFlag, ThreadedWorkerPool

if TYPE_CHECKING:
    from solin.core.meetings.workbook import Workbook


log = logging.getLogger(__name__)


class _FetchWorkbook(Protocol):
    def __call__(
        self, monday: date, api_code: str, /, *, is_cancelled: Callable[[], bool],
    ) -> Workbook | None: ...


@dataclass(frozen=True, slots=True)
class _Request:
    token: Hashable
    monday: date
    api_code: str
    cancelled: CancellationFlag = field(default_factory=CancellationFlag)


class TimerWorkbookLookup(QObject):
    """Run one lookup at a time and deliver only the latest valid request."""

    completed = Signal(object, object)
    _finished = Signal(object, object)

    def __init__(
        self,
        fetch: _FetchWorkbook,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._fetch = fetch
        self._workers = ThreadedWorkerPool()
        self._closed = CancellationFlag()
        self._current: _Request | None = None
        self._active: _Request | None = None
        self._pending: _Request | None = None
        closed = self._closed
        self.destroyed.connect(lambda: closed.set())
        self._finished.connect(self._consume, Qt.ConnectionType.QueuedConnection)

    def request(self, token: Hashable, monday: date, api_code: str) -> None:
        if self._closed.is_set() or (self._current is not None and self._current.token == token):
            return
        request = _Request(token, monday, api_code)
        self._current = request
        if self._active is not None:
            self._active.cancelled.set()
            self._pending = request
        else:
            self._start(request)

    def invalidate(self) -> None:
        self._current = None
        self._pending = None
        if self._active is not None:
            self._active.cancelled.set()

    def shutdown(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.invalidate()
        self._workers.shutdown(timeout=0.25)

    def _start(self, request: _Request) -> None:
        self._active = request
        fetch = self._fetch
        closed = self._closed
        finished = self._finished.emit

        def run() -> None:
            try:
                result = fetch(request.monday, request.api_code, is_cancelled=request.cancelled.is_set)
            except Exception:  # noqa: BLE001 - background lookup must not disrupt the timer
                log.exception("Timer workbook lookup failed")
                result = None
            try:
                closed.run_if_active(lambda: finished(request, result))
            except RuntimeError:
                # The parent can be destroyed independently of the request.
                return

        try:
            if self._workers.submit("timer-workbook-lookup", run) is not None:
                return
        except RuntimeError:
            log.warning("Could not start timer workbook lookup", exc_info=True)
        self._active = None

    @Slot(object, object)
    def _consume(self, request: _Request, result: Workbook | None) -> None:
        if self._closed.is_set() or request is not self._active:
            return
        self._active = None
        if request is self._current and result is not None:
            self.completed.emit(request.token, result)
        if not self._closed.is_set() and self._active is None and self._pending is not None:
            pending = self._pending
            self._pending = None
            self._start(pending)
