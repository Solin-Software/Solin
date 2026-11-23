from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from solin.core.jw.wol_finder import finder_headers
from solin.core.meetings.workbook import fetch_workbook, parse_workbook
from solin.core.network.http import HttpError, HttpHeaders, HttpResponse

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "workbook"


def _document(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "name",
    [
        "pt-202026169.json",
        "e-202026404.json",
        "j-202026404.json",
        "a-202026404.json",
        "chs-202026404.json",
    ],
)
def test_workbook_formats_extract_order_sections_and_durations(name):
    workbook = parse_workbook(_document(name)["content"])
    assert workbook is not None
    assert [part.section for part in workbook.parts] == [
        "opening_comments",
        "treasures",
        "treasures",
        "treasures",
        "ministry",
        "ministry",
        "ministry",
        "living",
        "living",
        "concluding_comments",
    ]
    assert [part.number for part in workbook.parts] == [None, 1, 2, 3, 4, 5, 6, 7, 8, None]
    assert [part.duration_seconds for part in workbook.parts] == [
        60,
        600,
        600,
        240,
        180,
        240,
        300,
        900,
        1800,
        180,
    ]
    assert [part.kind for part in workbook.parts].count("cbs") == 1
    assert workbook.parts[-2].kind == "cbs"
    assert all(part.title for part in workbook.parts)


def test_missing_duration_keeps_part_and_ambiguous_study_fails_closed():
    content = _document("pt-202026169.json")["content"]
    without_regular_duration = content.replace("(3 min) TESTEMUNHO", "TESTEMUNHO", 1)
    workbook = parse_workbook(without_regular_duration)
    assert workbook is not None
    assert workbook.parts[4].duration_seconds is None
    assert parse_workbook(content.replace("(30 min)", "(20 min)", 1)) is None
    assert parse_workbook(content.replace("dc-icon--wheat", "dc-icon--unknown", 1)) is None
    assert parse_workbook(content.replace("8. Estudo", "9. Estudo", 1)) is None


def test_unrelated_parenthetical_is_not_a_duration():
    content = _document("e-202026404.json")["content"]
    content = content.replace("(3 min.)", "(2 lessons)", 1)
    workbook = parse_workbook(content)
    assert workbook is not None
    assert workbook.parts[4].duration_seconds is None


def test_titles_extract_nested_markup_and_exclude_framing_songs_and_duration():
    content = _document("pt-202026169.json")["content"]
    workbook = parse_workbook(content)
    assert workbook is not None
    assert workbook.parts[0].title == "Comentários iniciais"
    assert workbook.parts[1].title == "Exemplo de título"
    assert workbook.parts[-1].title == "Comentários finais"


def test_variable_number_of_parts():
    content = _document("pt-202026169.json")["content"]
    content = content.replace("7. Necessidades", "8. Necessidades", 1)
    content = content.replace("8. Estudo", "9. Estudo", 1)
    content = content.replace(
        '<div class="dc-icon--sheep">',
        '<h3><strong>7. Outra parte</strong></h3><div><p>(2 min)</p></div>'
        '<div class="dc-icon--sheep">',
        1,
    )
    workbook = parse_workbook(content)
    assert workbook is not None
    ministry = [part for part in workbook.parts if part.section == "ministry"]
    assert len(ministry) == 4
    assert ministry[-1].duration_seconds == 120


@pytest.mark.parametrize("publication_class", ["pub-mwb", "pub-mwb26"])
def test_fetch_validates_week_locale_document_and_shared_protocol(monkeypatch, publication_class):
    document = _document("pt-202026169.json")
    path = document["url"]
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            html = f'<a class="{publication_class} docClass-106 ml-T" href="{path}">workbook</a>'
            return HttpResponse(
                "https://wol.jw.org/pt/wol/meetings/r5/lp-t/2026/27",
                200,
                HttpHeaders(),
                html.encode(),
            )
        return HttpResponse(url, 200, HttpHeaders(), json.dumps(document).encode())

    monkeypatch.setattr("solin.core.meetings.workbook.get", get)
    assert fetch_workbook(date(2026, 6, 29), "T") is not None
    assert parse_qs(urlsplit(calls[0][0]).query) == {
        "alias": ["meetings"],
        "date": ["2026-06-29"],
        "wtlocale": ["T"],
    }
    assert parse_qs(urlsplit(calls[1][0]).query)["docid"] == ["202026169"]
    assert all(call[1]["headers"] == finder_headers() and call[1]["max_bytes"] for call in calls)


@pytest.mark.parametrize(
    "failure",
    ["wrong_week", "wrong_locale", "ambiguous", "wrong_document", "offline", "challenge",
     "unexpected_redirect"],
)
def test_fetch_fails_closed(monkeypatch, failure):
    document = _document("pt-202026169.json")
    path = document["url"]
    calls = 0

    def get(url, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "offline":
            raise HttpError("offline")
        if calls == 1:
            locale = "E" if failure == "wrong_locale" else "T"
            html = f'<a class="pub-mwb docClass-106 ml-{locale}" href="{path}">one</a>'
            if failure == "ambiguous":
                html += '<a class="pub-mwb docClass-106 ml-T" href="/pt/wol/d/r5/lp-t/202026170">two</a>'
            week = 28 if failure == "wrong_week" else 27
            return HttpResponse(
                f"https://wol.jw.org/pt/wol/meetings/r5/lp-t/2026/{week}",
                200,
                HttpHeaders(),
                html.encode(),
            )
        if failure == "wrong_document":
            document["url"] = "/pt/wol/d/r5/lp-t/202026170"
        if failure == "unexpected_redirect":
            url = "https://example.com/document"
        return HttpResponse(
            url,
            202 if failure == "challenge" else 200,
            HttpHeaders(),
            json.dumps(document).encode(),
        )

    monkeypatch.setattr("solin.core.meetings.workbook.get", get)
    assert fetch_workbook(date(2026, 6, 29), "T") is None


def test_invalid_week_and_cancellation_do_not_request(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("unexpected HTTP request")

    monkeypatch.setattr("solin.core.meetings.workbook.get", unexpected)
    assert fetch_workbook(date(2026, 6, 30), "T") is None
    assert fetch_workbook(date(2026, 6, 29), "T", is_cancelled=lambda: True) is None


def test_fetch_accepts_iso_week_across_year_boundary(monkeypatch):
    document = _document("pt-202026169.json")
    path = document["url"]
    calls = 0

    def get(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            html = f'<a class="pub-mwb docClass-106 ml-T" href="{path}">workbook</a>'
            return HttpResponse(
                "https://wol.jw.org/pt/wol/meetings/r5/lp-t/2026/1",
                200,
                HttpHeaders(),
                html.encode(),
            )
        return HttpResponse(url, 200, HttpHeaders(), json.dumps(document).encode())

    monkeypatch.setattr("solin.core.meetings.workbook.get", get)
    assert fetch_workbook(date(2025, 12, 29), "T") is not None
