from __future__ import annotations

import sqlite3
from concurrent.futures import CancelledError

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
        "resolve_jworg_meta",
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


def test_write_jwlplaylist_reports_progress(tmp_path):
    events = []
    archive = tmp_path / "progress.jwlplaylist"

    writer.write_jwlplaylist(
        "Progress",
        [
            {
                "title": "Image",
                "url": "",
                "type": "image",
                "data": b"image-data",
                "filename": "image.png",
                "mime_type": "image/png",
            }
        ],
        archive,
        tmp_path / "cache",
        progress_callback=lambda phase, completed, total: events.append((phase, completed, total)),
    )

    assert ("items", 0, 1) in events
    assert ("items", 1, 1) in events
    archive_events = [event for event in events if event[0] == "archive"]
    assert archive_events[0][1] == 0
    assert archive_events[-1][1] == archive_events[-1][2]


def test_write_jwlplaylist_cancel_keeps_existing_destination_and_removes_temporary_file(
    tmp_path,
):
    archive = tmp_path / "existing.jwlplaylist"
    archive.write_bytes(b"existing archive")
    cancelled = False

    def progress(phase, completed, _total):
        nonlocal cancelled
        if phase == "archive" and completed > 0:
            cancelled = True

    with pytest.raises(CancelledError):
        writer.write_jwlplaylist(
            "Cancelled",
            [{"title": "Empty", "url": "https://example.test/not-cached.mp4"}],
            archive,
            tmp_path / "cache",
            progress_callback=progress,
            should_cancel=lambda: cancelled,
        )

    assert archive.read_bytes() == b"existing archive"
    assert list(tmp_path.glob(f".{archive.name}.*.tmp")) == []
