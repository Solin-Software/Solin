from __future__ import annotations

from app.core.playlists.writer import _parse_jw_filename


def test_parse_jw_filename_handles_standard_cdn_filename():
    assert _parse_jw_filename("C:/Downloads/sjjm_T_002_r720P.mp4") == {
        "key_symbol": "sjjm",
        "track": 2,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 5,
    }


def test_parse_jw_filename_handles_legacy_pub_prefix():
    assert _parse_jw_filename("C:/Downloads/pub-sjjm_T_1_r720P.mp4") == {
        "key_symbol": "sjjm",
        "track": 1,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 5,
    }


def test_parse_jw_filename_uses_original_name_for_opaque_cached_files():
    assert _parse_jw_filename(
        "C:/cache/7dfda495.mp3",
        orig_name="rr_T_43.mp3",
    ) == {
        "key_symbol": "rr",
        "track": 43,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 5,
    }
