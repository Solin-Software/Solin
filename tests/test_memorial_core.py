from __future__ import annotations

from datetime import date

from app.core.meetings.memorial import (
    _monday_of,
    _parse_mi_jwpub_response,
    memorial_date_for_year,
)


def test_parse_mi_jwpub_response_uses_requested_language_payload():
    data = {
        "files": {
            "T": {
                "JWPUB": [
                    {
                        "file": {
                            "url": "https://example.test/mi26_T.jwpub",
                            "checksum": "abc123",
                        },
                        "images": {
                            "sqr": {
                                "sm": {"url": "https://example.test/thumb.jpg"},
                            }
                        },
                    }
                ]
            }
        }
    }

    assert _parse_mi_jwpub_response(data, "T") == (
        "https://example.test/mi26_T.jwpub",
        "https://example.test/thumb.jpg",
        "abc123",
    )


def test_parse_mi_jwpub_response_falls_back_to_english_payload():
    data = {
        "files": {
            "E": {
                "JWPUB": [
                    {
                        "file": {"url": "https://example.test/mi26_E.jwpub"},
                        "images": {"wss": {"md": {"url": "https://example.test/wss.jpg"}}},
                    }
                ]
            }
        }
    }

    assert _parse_mi_jwpub_response(data, "T") == (
        "https://example.test/mi26_E.jwpub",
        "https://example.test/wss.jpg",
        "",
    )


def test_monday_of_returns_week_start():
    assert _monday_of(date(2026, 4, 2)) == date(2026, 3, 30)


def test_memorial_date_calculator_matches_verified_modern_dates():
    expected = {
        2002: date(2002, 3, 28),
        2003: date(2003, 4, 16),
        2004: date(2004, 4, 4),
        2005: date(2005, 3, 24),
        2006: date(2006, 4, 12),
        2007: date(2007, 4, 2),
        2008: date(2008, 3, 22),
        2009: date(2009, 4, 9),
        2010: date(2010, 3, 30),
        2011: date(2011, 4, 17),
        2012: date(2012, 4, 5),
        2013: date(2013, 3, 26),
        2014: date(2014, 4, 14),
        2015: date(2015, 4, 3),
        2016: date(2016, 3, 23),
        2017: date(2017, 4, 11),
        2018: date(2018, 3, 31),
        2019: date(2019, 4, 19),
        2020: date(2020, 4, 7),
        2021: date(2021, 3, 27),
        2022: date(2022, 4, 15),
        2023: date(2023, 4, 4),
        2024: date(2024, 3, 24),
        2025: date(2025, 4, 12),
        2026: date(2026, 4, 2),
        2027: date(2027, 3, 22),
        2028: date(2028, 4, 9),
    }

    actual = {year: memorial_date_for_year(year) for year in expected}

    assert actual == expected
