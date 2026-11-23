from datetime import date
import threading

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QThread, QTimer
from shiboken6 import isValid

from solin.controllers.timer_workbook_lookup import TimerWorkbookLookup


MONDAY = date(2026, 11, 23)


def _wait_until(predicate, timeout_ms: int = 2_000) -> None:
    if predicate():
        return
    loop = QEventLoop()
    poll = QTimer(loop)
    poll.setInterval(5)
    poll.timeout.connect(lambda: loop.quit() if predicate() else None)
    timeout = QTimer(loop)
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    poll.start()
    timeout.start(timeout_ms)
    loop.exec()
    assert predicate(), "Timed out waiting for background lookup"


def test_latest_pending_request_wins_and_completes_on_owner_thread() -> None:
    started = threading.Event()
    release = threading.Event()
    first_request_cancelled: bool | None = None
    calls = []
    result = object()

    def fetch(monday, api_code, *, is_cancelled):
        nonlocal first_request_cancelled
        calls.append((monday, api_code))
        if api_code == "first":
            started.set()
            if release.wait(2):
                first_request_cancelled = is_cancelled()
        return result

    lookup = TimerWorkbookLookup(fetch)
    completed = []
    lookup.completed.connect(
        lambda token, value: completed.append((token, value, QThread.currentThread()))
    )
    try:
        lookup.request(1, MONDAY, "first")
        assert started.wait(1)
        lookup.request(1, MONDAY, "duplicate")
        lookup.request(2, MONDAY, "superseded")
        lookup.request(3, MONDAY, "latest")
        lookup.request(3, MONDAY, "duplicate")
        release.set()
        _wait_until(lambda: bool(completed))
        assert first_request_cancelled is not None, "First lookup timed out waiting for release"
        assert first_request_cancelled, "Superseded lookup did not receive cancellation"
        assert calls == [(MONDAY, "first"), (MONDAY, "latest")]
        assert completed == [(3, result, lookup.thread())]
    finally:
        release.set()
        lookup.shutdown()


def test_invalidation_reentry_discards_stale_response_even_with_same_token() -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []
    old, new = object(), object()

    def fetch(_monday, _api_code, *, is_cancelled):
        calls.append(True)
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
            return old
        return new

    lookup = TimerWorkbookLookup(fetch)
    completed = []
    lookup.completed.connect(lambda token, value: completed.append((token, value)))
    try:
        lookup.request("week", MONDAY, "T")
        assert started.wait(1)
        lookup.invalidate()
        lookup.request("week", MONDAY, "T")
        release.set()
        _wait_until(lambda: bool(completed))
        assert completed == [("week", new)]
        assert len(calls) == 2
    finally:
        release.set()
        lookup.shutdown()


def test_failure_waits_for_explicit_new_generation() -> None:
    calls = []
    result = object()

    def fetch(_monday, _api_code, *, is_cancelled):
        calls.append(True)
        if len(calls) == 1:
            raise ValueError("lookup failed")
        return result

    lookup = TimerWorkbookLookup(fetch)
    completed = []
    lookup.completed.connect(lambda token, value: completed.append((token, value)))
    try:
        lookup.request(1, MONDAY, "T")
        _wait_until(lambda: len(calls) == 1 and lookup._active is None)
        lookup.request(1, MONDAY, "T")
        assert len(calls) == 1
        lookup.request(2, MONDAY, "T")
        _wait_until(lambda: bool(completed))
        assert completed == [(2, result)]
        assert len(calls) == 2
    finally:
        lookup.shutdown()


def test_empty_result_does_not_emit_and_can_be_retried() -> None:
    calls = []
    result = object()

    def fetch(_monday, _api_code, *, is_cancelled):
        calls.append(True)
        return None if len(calls) == 1 else result

    lookup = TimerWorkbookLookup(fetch)
    completed = []
    lookup.completed.connect(lambda token, value: completed.append((token, value)))
    try:
        lookup.request(1, MONDAY, "T")
        _wait_until(lambda: len(calls) == 1 and lookup._active is None)
        assert completed == []
        lookup.invalidate()
        lookup.request(2, MONDAY, "T")
        _wait_until(lambda: bool(completed))
        assert completed == [(2, result)]
    finally:
        lookup.shutdown()


def test_shutdown_discards_result_already_queued() -> None:
    finished = threading.Event()

    def fetch(_monday, _api_code, *, is_cancelled):
        finished.set()
        return object()

    lookup = TimerWorkbookLookup(fetch)
    completed = []
    lookup.completed.connect(lambda *args: completed.append(args))
    lookup.request(1, MONDAY, "T")
    assert finished.wait(1)
    lookup.shutdown()
    QCoreApplication.processEvents()
    assert completed == []


def test_shutdown_drops_pending_and_late_results_and_allows_deletion() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = []

    def fetch(_monday, api_code, *, is_cancelled):
        calls.append(api_code)
        started.set()
        release.wait(2)
        finished.set()
        return object()

    lookup = TimerWorkbookLookup(fetch)
    completed = []
    lookup.completed.connect(lambda *args: completed.append(args))
    try:
        lookup.request(1, MONDAY, "active")
        assert started.wait(1)
        lookup.request(2, MONDAY, "pending")
        lookup.shutdown()
        lookup.request(3, MONDAY, "closed")
        lookup.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not isValid(lookup)
        release.set()
        assert finished.wait(1)
        QCoreApplication.processEvents()
        assert calls == ["active"]
        assert completed == []
    finally:
        release.set()
