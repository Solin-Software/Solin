from __future__ import annotations

import pytest

from solin.core.jw.identifiers import (
    MEPS_FROM_LANG,
    is_jw_url,
    lang_to_meps,
    meps_to_lang,
    parse_jworg_url,
)


def test_parse_jworg_url_handles_cdn_track_url():
    identifier = parse_jworg_url("https://akamd1.jw-cdn.org/sg2/p/64dbe62/2/o/sjjm_T_002_r720P.mp4")

    assert identifier == {
        "key_symbol": "sjjm",
        "track": 2,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 5,
    }


def test_parse_jworg_url_handles_docid_url():
    identifier = parse_jworg_url(
        "https://akamd1.jw-cdn.org/sg2/p/c5f721/1/o/502100025_T_cnt_1_r720P.mp4"
    )

    assert identifier == {
        "key_symbol": None,
        "track": 1,
        "issue_tag": None,
        "doc_id": 502100025,
        "meps_language": 5,
    }


def test_parse_jworg_url_handles_publication_issue():
    identifier = parse_jworg_url("https://download.jw.org/files/media_pub/pub-w_T_202201.mp4")

    assert identifier == {
        "key_symbol": "w",
        "track": None,
        "issue_tag": 202201,
        "doc_id": None,
        "meps_language": 5,
    }


def test_jw_url_detection_validates_hostname_boundaries():
    assert is_jw_url("https://download.jw.org/media/file.mp4") is True
    assert is_jw_url("https://akamd1.jw-cdn.org/media/file.mp4") is True
    assert is_jw_url("https://jw.org.evil.example/media/file.mp4") is False
    assert is_jw_url("https://example.test/path/jw.org/file.mp4") is False


def test_language_identifier_conversions_use_explicit_fallbacks():
    assert lang_to_meps("t") == 5
    assert lang_to_meps("unknown", fallback=99) == 99
    assert meps_to_lang(5) == "T"
    assert meps_to_lang(999999, fallback="E") == "E"


def test_language_mapping_is_immutable():
    with pytest.raises(TypeError):
        MEPS_FROM_LANG["ZZZ"] = 9999  # type: ignore[index]
