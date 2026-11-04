from __future__ import annotations

from datetime import date

import pytest

import solin.core.meetings.publication_worker as worker_module
from solin.core.jw.publication_links import JwpubMediaInfo
from solin.core.meetings.content_policy import MeetingContentOperation, PublicationContentRequest
from solin.core.meetings.jwpub_cache import JwpubChecksumStore
from solin.core.meetings.publication_worker import JwpubWorker


MONDAY = date(2026, 5, 25)


@pytest.mark.parametrize("cancel_on_failure", [False, True])
def test_checksum_failure_is_isolated_to_its_publication(monkeypatch, tmp_path, cancel_on_failure):
    worker = JwpubWorker(tmp_path / "jwpub", JwpubChecksumStore(tmp_path / "checksums.json"))
    observed = []
    errors = []
    finished = []
    requested = []
    worker.content_checked.connect(lambda _context, pub, value: observed.append((pub, value)))
    worker.error.connect(lambda _key, pub, *_: errors.append(pub))
    worker.load_finished.connect(lambda *context: finished.append(context))

    def resolve(pub, *_):
        requested.append(pub)
        if pub == "mwb":
            if cancel_on_failure:
                worker.cancel_request((MONDAY.isoformat(), "T", False, 1))
            raise OSError("Midweek checksum unavailable")
        return JwpubMediaInfo("https://invalid.test/source", "weekend-checksum")

    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", resolve)
    worker.load_week(
        MONDAY,
        generation=1,
        content_request=PublicationContentRequest(
            operation=MeetingContentOperation.CHECK,
            dependencies={
                "mwb": [{"pub": "mwb", "language": "T", "issue": "20260500"}],
                "wt": [{"pub": "w", "language": "T", "issue": "20260400"}],
            },
        ),
    )

    assert errors == ([] if cancel_on_failure else ["mwb"])
    assert observed == ([] if cancel_on_failure else [("wt", {"w:T:20260400": "weekend-checksum"})])
    assert requested == (["mwb"] if cancel_on_failure else ["mwb", "w"])
    assert finished == [(MONDAY.isoformat(), "T", False, 1)]


def test_matching_remote_revision_is_only_observed_without_materialization(monkeypatch, tmp_path):
    checksums = JwpubChecksumStore(tmp_path / "checksums.json")
    worker = JwpubWorker(tmp_path / "jwpub", checksums)
    checksums.save("mwb", "T", "20260500", "current")
    observed = []
    worker.content_checked.connect(lambda _context, _pub, value: observed.append(value))
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: JwpubMediaInfo("https://invalid.test/source", "current"),
    )
    monkeypatch.setattr(
        worker,
        "_parse_mwb",
        lambda *_: (_ for _ in ()).throw(AssertionError("Check must not parse")),
    )
    worker.load_week(
        MONDAY,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}),
            operation=MeetingContentOperation.CHECK,
            dependencies={
                "mwb": [{"pub": "mwb", "language": "T", "issue": "20260500", "checksum": "current"}]
            },
        ),
    )
    assert observed == [{"mwb:T:20260500": "current"}]


def test_changed_checksum_does_not_advance_applied_revision_during_check(monkeypatch, tmp_path):
    checksums = JwpubChecksumStore(tmp_path / "checksums.json")
    worker = JwpubWorker(tmp_path / "jwpub", checksums)
    checksums.save("mwb", "T", "20260500", "old")
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: JwpubMediaInfo("https://invalid.test/source", "new"),
    )
    parsed = []
    monkeypatch.setattr(worker, "_parse_mwb", lambda *_: parsed.append("mwb"))
    worker.load_week(
        MONDAY,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}),
            operation=MeetingContentOperation.CHECK,
            dependencies={
                "mwb": [{"pub": "mwb", "language": "T", "issue": "20260500", "checksum": "old"}]
            },
        ),
    )
    assert checksums.get("mwb", "T", "20260500") == "old"
    assert parsed == []


def test_explicit_update_downloads_and_parses_only_selected_publication(monkeypatch, tmp_path):
    checksums = JwpubChecksumStore(tmp_path / "checksums.json")
    worker = JwpubWorker(tmp_path / "jwpub", checksums)
    parsed = []
    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_: JwpubMediaInfo("https://invalid.test/source", "new"),
    )
    monkeypatch.setattr(worker, "_download", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker, "_parse_mwb", lambda *_: parsed.append("mwb"))
    monkeypatch.setattr(worker, "_try_wt_cached", lambda *_: parsed.append("wt"))
    worker.load_week(
        MONDAY,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}), operation=MeetingContentOperation.UPDATE
        ),
    )
    assert parsed == ["mwb"]
    assert checksums.get("mwb", "T", "20260500") == "new"


def test_initial_weekend_preparation_does_not_prepare_midweek(monkeypatch, tmp_path):
    worker = JwpubWorker(tmp_path / "jwpub", JwpubChecksumStore(tmp_path / "checksums.json"))
    obtained = []
    parsed = []
    monkeypatch.setattr(worker, "_resolve_archive", lambda *_: JwpubMediaInfo(None))
    monkeypatch.setattr(
        worker, "_obtain_publication", lambda pub, *_args, **_kwargs: obtained.append(pub) or True
    )
    monkeypatch.setattr(worker, "_try_wt_cached", lambda *_: parsed.append("wt") or True)
    worker.load_week(
        MONDAY,
        known_wt_issue="20260400",
        content_request=PublicationContentRequest(publications=frozenset({"wt"})),
    )
    assert obtained == ["w"]
    assert parsed == ["wt"]


def test_missing_remote_checksum_never_confirms_a_source_update(monkeypatch, tmp_path):
    worker = JwpubWorker(tmp_path / "jwpub", JwpubChecksumStore(tmp_path / "checksums.json"))
    observed = []
    worker.content_checked.connect(lambda _context, _pub, value: observed.append(value))
    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", lambda *_: JwpubMediaInfo(""))
    worker.load_week(
        MONDAY,
        content_request=PublicationContentRequest(
            publications=frozenset({"mwb"}),
            operation=MeetingContentOperation.CHECK,
            dependencies={
                "mwb": [{"pub": "mwb", "language": "T", "issue": "20260500", "checksum": "current"}]
            },
        ),
    )
    assert observed == [{}]


def test_facade_retires_old_generations_and_rejects_stale_ready_callbacks(tmp_path):
    from PySide6.QtCore import QCoreApplication
    from solin.core.meetings.models import WeekData
    from solin.core.meetings.publications import JwpubService

    app = QCoreApplication.instance() or QCoreApplication([])
    service = JwpubService(tmp_path / "jwpub", JwpubChecksumStore(tmp_path / "checksums.json"))
    context = (MONDAY.isoformat(), "T", False)
    other = (MONDAY.isoformat(), "E", False, 1)
    try:
        for generation in (1, 2, 3):
            service._active[(*context, generation)] = WeekData(
                monday=MONDAY, language_code="T", request_generation=generation
            )
        service._active[other] = WeekData(monday=MONDAY, language_code="E", request_generation=1)
        service._active_load_key = (*context, 3)
        service._on_load_finished(*context, 3)
        assert set(service._active) == {(*context, 3), other}
        service._on_mwb_done(
            MONDAY.isoformat(), WeekData(monday=MONDAY, language_code="T", request_generation=1)
        )
        assert set(service._active) == {(*context, 3), other}
        service.clear_week(MONDAY)
        assert service.get_week_data(MONDAY, language_code="T") is None
        assert service.get_week_data(MONDAY, language_code="E") is None
        app.processEvents()
    finally:
        service.shutdown(wait_ms=1000)
