from __future__ import annotations

from app.core.jw.metadata import _extract_best_meta, meps_to_lang, parse_jworg_url


def test_parse_jworg_url_handles_cdn_track_url():
    meta = parse_jworg_url(
        "https://akamd1.jw-cdn.org/sg2/p/64dbe62/2/o/sjjm_T_002_r720P.mp4"
    )

    assert meta == {
        "key_symbol": "sjjm",
        "track": 2,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 5,
    }


def test_parse_jworg_url_handles_docid_url():
    meta = parse_jworg_url(
        "https://akamd1.jw-cdn.org/sg2/p/c5f721/1/o/502100025_T_cnt_1_r720P.mp4"
    )

    assert meta == {
        "key_symbol": None,
        "track": 1,
        "issue_tag": None,
        "doc_id": 502100025,
        "meps_language": 5,
    }


def test_meps_to_lang_uses_fallback_for_unknown_ids():
    assert meps_to_lang(5) == "T"
    assert meps_to_lang(999999, fallback="E") == "E"


def test_extract_best_meta_prefers_matching_unsubtitled_track():
    data = {
        "files": {
            "T": {
                "MP4": [
                    {
                        "track": 1,
                        "title": "Wrong Track",
                        "duration": 10,
                        "file": {"url": "https://example.test/wrong.mp4"},
                    },
                    {
                        "track": 2,
                        "title": "720p",
                        "duration": 12.5,
                        "subtitled": False,
                        "file": {"url": "https://example.test/right.mp4"},
                    },
                ]
            }
        }
    }

    assert _extract_best_meta(data, "T", "MP4", target_track=2) == {
        "title": None,
        "duration_ticks": 125_000_000,
    }
