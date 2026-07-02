from __future__ import annotations

import sqlite3

import pytest

from solin.core.playlists import writer
from solin.core.media.jw_reference import parse_jw_media_reference


def test_parse_jw_media_reference_handles_standard_cdn_filename():
    assert parse_jw_media_reference("C:/Downloads/sjjm_T_002_r720P.mp4") == {
        "key_symbol": "sjjm",
        "track": 2,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 5,
    }


def test_parse_jw_media_reference_handles_legacy_pub_prefix():
    assert parse_jw_media_reference("C:/Downloads/pub-sjjm_T_1_r720P.mp4") == {
        "key_symbol": "sjjm",
        "track": 1,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 5,
    }


def test_parse_jw_media_reference_uses_original_name_for_opaque_cached_files():
    assert parse_jw_media_reference(
        "C:/cache/7dfda495.mp3",
        original_filename="rr_T_43.mp3",
    ) == {
        "key_symbol": "rr",
        "track": 43,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 5,
    }


def test_write_jwlplaylist_normalizes_sqlite_failures(monkeypatch, tmp_path):
    def _fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(writer, "_write_jwlplaylist", _fail)

    with pytest.raises(writer.PlaylistWriteError) as exc_info:
        writer.write_jwlplaylist(
            "Playlist",
            [],
            tmp_path / "playlist.jwlplaylist",
            tmp_path / "media_cache",
        )

    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
