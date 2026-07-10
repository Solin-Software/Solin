from __future__ import annotations

from datetime import date

import solin.core.meetings.publication_worker as worker_module
from solin.core.jw.publication_links import JwpubMediaInfo
from solin.core.meetings.jwpub_cache import JwpubChecksumStore
from solin.core.meetings.meeting_weeks import mwb_issue_for_week
from solin.core.meetings.publication_worker import JwpubWorker


def test_persisted_trees_are_only_revalidated_when_jwpub_checksums_match(
    monkeypatch,
    tmp_path,
) -> None:
    monday = date(2026, 5, 25)
    mwb_issue = mwb_issue_for_week(monday)
    wt_issue = "20260400"
    checksums = JwpubChecksumStore(tmp_path / "checksums.json")
    checksums.save("mwb", "T", mwb_issue, "mwb-current")
    checksums.save("w", "T", wt_issue, "wt-current")
    worker = JwpubWorker(tmp_path / "jwpub", checksums)
    requests: list[tuple[str, str, str]] = []
    parsed: list[str] = []

    def resolve(pub: str, language: str, issue: str) -> JwpubMediaInfo:
        requests.append((pub, language, issue))
        checksum = "mwb-current" if pub == "mwb" else "wt-current"
        return JwpubMediaInfo("https://example.invalid/publication.jwpub", checksum)

    monkeypatch.setattr(worker_module, "resolve_jwpub_archive", resolve)
    monkeypatch.setattr(worker, "_parse_mwb", lambda *_args: parsed.append("mwb"))
    monkeypatch.setattr(worker, "_try_wt_cached", lambda *_args: parsed.append("wt"))

    worker.load_week(
        monday,
        language="T",
        materialize_cached_publications=frozenset(),
        known_wt_issue=wt_issue,
        persisted_source_checksums={
            "mwb": "mwb-current",
            "wt": "wt-current",
        },
    )

    assert requests == [("mwb", "T", mwb_issue), ("w", "T", wt_issue)]
    assert parsed == []


def test_changed_checksum_rebuilds_the_persisted_midweek_tree(monkeypatch, tmp_path) -> None:
    monday = date(2026, 5, 25)
    issue = mwb_issue_for_week(monday)
    checksums = JwpubChecksumStore(tmp_path / "checksums.json")
    checksums.save("mwb", "T", issue, "old")
    worker = JwpubWorker(tmp_path / "jwpub", checksums)
    parsed: list[str] = []

    monkeypatch.setattr(
        worker_module,
        "resolve_jwpub_archive",
        lambda *_args: JwpubMediaInfo("https://example.invalid/publication.jwpub", "new"),
    )
    monkeypatch.setattr(worker, "_download", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(worker, "_parse_mwb", lambda *_args: parsed.append("mwb"))
    monkeypatch.setattr(worker, "_revalidate_wt", lambda *_args, **_kwargs: None)

    worker.load_week(
        monday,
        language="T",
        materialize_cached_publications=frozenset(),
        persisted_source_checksums={"mwb": "old"},
    )

    assert parsed == ["mwb"]
    assert checksums.get("mwb", "T", issue) == "new"
