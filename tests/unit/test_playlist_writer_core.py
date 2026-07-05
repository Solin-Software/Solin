from __future__ import annotations

import sqlite3

import pytest

from solin.core.media.jw_reference import parse_jw_media_reference
from solin.core.playlists import reader, writer


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


def test_jwl_round_trip_preserves_embedded_media_trim_and_duration(tmp_path):
    archive = tmp_path / "embedded.jwlplaylist"
    writer.write_jwlplaylist(
        "Embedded",
        [
            {
                "title": "Clip",
                "url": "",
                "type": "video",
                "data": b"embedded-video",
                "filename": "clip.mp4",
                "mime_type": "video/mp4",
                "start_trim_ticks": 10_000_000,
                "end_trim_ticks": 20_000_000,
                "base_duration_ticks": 90_000_000,
            }
        ],
        archive,
        tmp_path / "cache",
    )

    [item] = reader.read_jwlplaylist(archive)["items"]

    assert item["start_trim_ticks"] == 10_000_000
    assert item["end_trim_ticks"] == 20_000_000
    assert item["base_duration_ticks"] == 90_000_000


def test_jwl_round_trip_prefers_location_duration_from_database(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        writer,
        "resolve_jworg_meta",
        lambda **_kwargs: {
            "title": "Resolved title",
            "duration_ticks": 999_000_000,
        },
    )
    archive = tmp_path / "jworg.jwlplaylist"
    writer.write_jwlplaylist(
        "JW.org",
        [
            {
                "title": "Video",
                "url": "",
                "type": "video",
                "key_symbol": "lff",
                "track": 1,
                "meps_language": 5,
                "start_trim_ticks": 30_000_000,
                "end_trim_ticks": 40_000_000,
                "base_duration_ticks": 120_000_000,
            }
        ],
        archive,
        tmp_path / "cache",
    )
    monkeypatch.setattr(
        reader,
        "resolve_jworg_metadata",
        lambda **_kwargs: {
            "title": "Resolved title",
            "url": "https://example.test/video.mp4",
            "duration_ticks": 888_000_000,
        },
    )

    [item] = reader.read_jwlplaylist(archive)["items"]

    assert item["start_trim_ticks"] == 30_000_000
    assert item["end_trim_ticks"] == 40_000_000
    assert item["base_duration_ticks"] == 120_000_000
