from __future__ import annotations

from PySide6.QtCore import QCoreApplication

from solin.core.jw import congregation_lookup, congregation_lookup_service
from solin.core.jw.congregation_lookup import CongregationMatch
from solin.core.jw.congregation_lookup_service import CongregationLookupService
from solin.core.network.http import HttpStatusError


def _match(name: str) -> CongregationMatch:
    return CongregationMatch(guid=f"guid-{name}", name=name, formatted_name=name)


def _drain(service: CongregationLookupService) -> None:
    service._pool.waitForDone()
    QCoreApplication.processEvents()


def test_a_stale_failure_never_overrides_the_newest_search(monkeypatch):
    attempts: list[str] = []

    def search_congregations(name: str) -> list[CongregationMatch]:
        attempts.append(name)
        if name == "planal":
            raise OSError("connection reset")
        return [_match("Planalto - SP (97162)")]

    monkeypatch.setattr(
        congregation_lookup_service,
        "search_congregations",
        search_congregations,
    )

    service = CongregationLookupService()
    suggestions: list[list[CongregationMatch]] = []
    failures: list[str] = []
    service.suggestions_ready.connect(suggestions.append)
    service.failed.connect(failures.append)

    service.search("planal")
    service.search("planalto")
    _drain(service)

    # The pool may start either request first; what matters is that only the
    # newest one is allowed to report.
    assert sorted(attempts) == ["planal", "planalto"]
    assert failures == []
    assert suggestions == [[_match("Planalto - SP (97162)")]]
    service.shutdown()


def test_a_failed_current_search_is_reported(monkeypatch):
    def search_congregations(_name: str) -> list[CongregationMatch]:
        raise OSError("connection reset")

    monkeypatch.setattr(
        congregation_lookup_service,
        "search_congregations",
        search_congregations,
    )

    service = CongregationLookupService()
    failures: list[str] = []
    service.failed.connect(failures.append)

    service.search("planalto")
    _drain(service)

    assert len(failures) == 1
    service.shutdown()


def test_a_stale_schedule_result_never_overrides_a_newer_pick(monkeypatch):
    def fetch_meeting_schedule(guid: str):
        return guid

    monkeypatch.setattr(
        congregation_lookup_service,
        "fetch_meeting_schedule",
        fetch_meeting_schedule,
    )

    service = CongregationLookupService()
    resolved: list[object] = []
    service.schedule_ready.connect(resolved.append)

    service.fetch_schedule("first")
    service.fetch_schedule("second")
    _drain(service)

    assert resolved == ["second"]
    service.shutdown()


def test_shutdown_discards_work_that_finishes_late(monkeypatch):
    monkeypatch.setattr(
        congregation_lookup_service,
        "search_congregations",
        lambda _name: [_match("Planalto - SP (97162)")],
    )

    service = CongregationLookupService()
    suggestions: list[list[CongregationMatch]] = []
    service.suggestions_ready.connect(suggestions.append)

    service.search("planalto")
    service.shutdown()
    QCoreApplication.processEvents()

    assert suggestions == []


def test_repeating_a_query_is_answered_from_cache(monkeypatch):
    attempts: list[str] = []

    def search_congregations(name: str) -> list[CongregationMatch]:
        attempts.append(name)
        return [_match("Planalto - SP (97162)")]

    monkeypatch.setattr(
        congregation_lookup_service,
        "search_congregations",
        search_congregations,
    )

    service = CongregationLookupService()
    suggestions: list[list[CongregationMatch]] = []
    service.suggestions_ready.connect(suggestions.append)

    service.search("planalto")
    _drain(service)
    service.search("PLANALTO")
    _drain(service)

    assert attempts == ["planalto"]
    assert len(suggestions) == 2
    assert suggestions[0] == suggestions[1]
    service.shutdown()


def test_a_spent_request_budget_is_not_reported_as_a_connection_problem(monkeypatch):
    def search_congregations(_name: str) -> list[CongregationMatch]:
        raise HttpStatusError(congregation_lookup.CONGREGATION_SEARCH_URL, 429, "nope")

    monkeypatch.setattr(
        congregation_lookup_service,
        "search_congregations",
        search_congregations,
    )

    service = CongregationLookupService()
    failures: list[str] = []
    service.failed.connect(failures.append)

    service.search("planalto")
    _drain(service)

    assert failures == [congregation_lookup.RATE_LIMITED]
    service.shutdown()


def test_any_other_transport_failure_stays_unavailable(monkeypatch):
    def search_congregations(_name: str) -> list[CongregationMatch]:
        raise HttpStatusError(congregation_lookup.CONGREGATION_SEARCH_URL, 503, "nope")

    monkeypatch.setattr(
        congregation_lookup_service,
        "search_congregations",
        search_congregations,
    )

    service = CongregationLookupService()
    failures: list[str] = []
    service.failed.connect(failures.append)

    service.search("planalto")
    _drain(service)

    assert failures == [congregation_lookup.UNAVAILABLE]
    service.shutdown()
