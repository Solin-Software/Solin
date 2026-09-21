from __future__ import annotations

import sqlite3
import zipfile
from concurrent.futures import CancelledError

import pytest

from solin.core.playlists.reader import PlaylistReadError, read_jwlplaylist


def _write_playlist_archive(tmp_path, schema_and_data: str, *, entries=None):
    database = tmp_path / "userData.db"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(schema_and_data)
        connection.commit()
    finally:
        connection.close()

    archive = tmp_path / "malformed-data.jwlplaylist"
    with zipfile.ZipFile(archive, "w") as playlist_zip:
        playlist_zip.write(database, "userData.db")
        for name, data in (entries or {}).items():
            playlist_zip.writestr(name, data)
    return archive


def test_read_jwlplaylist_normalizes_invalid_sqlite_database(tmp_path):
    archive = tmp_path / "invalid.jwlplaylist"
    with zipfile.ZipFile(archive, "w") as playlist_zip:
        playlist_zip.writestr("userData.db", b"not a sqlite database")

    with pytest.raises(PlaylistReadError) as exc_info:
        read_jwlplaylist(archive)

    assert isinstance(exc_info.value.__cause__, sqlite3.DatabaseError)


def test_read_jwlplaylist_normalizes_invalid_database_schema(tmp_path):
    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()

    archive = tmp_path / "invalid-schema.jwlplaylist"
    with zipfile.ZipFile(archive, "w") as playlist_zip:
        playlist_zip.write(database, "userData.db")

    with pytest.raises(PlaylistReadError) as exc_info:
        read_jwlplaylist(archive)

    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)


def test_read_jwlplaylist_normalizes_missing_independent_media_path(tmp_path):
    archive = _write_playlist_archive(
        tmp_path,
        """
        CREATE TABLE PlaylistItem (
            PlaylistItemId INTEGER,
            Position INTEGER
        );
        CREATE TABLE IndependentMedia (
            IndependentMediaId INTEGER,
            FilePath TEXT,
            OriginalFilename TEXT,
            MimeType TEXT,
            Hash TEXT
        );
        CREATE TABLE PlaylistItemIndependentMediaMap (
            PlaylistItemId INTEGER,
            IndependentMediaId INTEGER
        );
        INSERT INTO PlaylistItem VALUES (1, 0);
        INSERT INTO IndependentMedia VALUES (1, NULL, NULL, 'image/png', '');
        INSERT INTO PlaylistItemIndependentMediaMap VALUES (1, 1);
        """,
    )

    with pytest.raises(
        PlaylistReadError,
        match="Missing required playlist field: IndependentMedia.FilePath",
    ):
        read_jwlplaylist(archive)


def test_read_jwlplaylist_normalizes_non_text_independent_media_mime_type(tmp_path):
    archive = _write_playlist_archive(
        tmp_path,
        """
        CREATE TABLE PlaylistItem (
            PlaylistItemId INTEGER,
            Position INTEGER
        );
        CREATE TABLE IndependentMedia (
            IndependentMediaId INTEGER,
            FilePath TEXT,
            OriginalFilename TEXT,
            MimeType,
            Hash TEXT
        );
        CREATE TABLE PlaylistItemIndependentMediaMap (
            PlaylistItemId INTEGER,
            IndependentMediaId INTEGER
        );
        INSERT INTO PlaylistItem VALUES (1, 0);
        INSERT INTO IndependentMedia VALUES (1, 'media/image.png', NULL, 42, '');
        INSERT INTO PlaylistItemIndependentMediaMap VALUES (1, 1);
        """,
        entries={"media/image.png": b"image"},
    )

    with pytest.raises(
        PlaylistReadError,
        match="Invalid playlist field IndependentMedia.MimeType",
    ):
        read_jwlplaylist(archive)


def test_read_jwlplaylist_honors_cancellation_before_opening(tmp_path):
    archive = tmp_path / "playlist.jwlplaylist"
    archive.write_bytes(b"not opened")

    with pytest.raises(CancelledError):
        read_jwlplaylist(archive, should_cancel=lambda: True)


def test_read_jwlplaylist_reports_chunk_and_item_progress(tmp_path):
    archive = _write_playlist_archive(
        tmp_path,
        """
        CREATE TABLE PlaylistItem (
            PlaylistItemId INTEGER,
            Label TEXT,
            Position INTEGER
        );
        CREATE TABLE IndependentMedia (
            IndependentMediaId INTEGER,
            FilePath TEXT,
            OriginalFilename TEXT,
            MimeType TEXT,
            Hash TEXT
        );
        CREATE TABLE PlaylistItemIndependentMediaMap (
            PlaylistItemId INTEGER,
            IndependentMediaId INTEGER,
            DurationTicks INTEGER
        );
        INSERT INTO PlaylistItem VALUES (1, 'Image', 0);
        INSERT INTO IndependentMedia VALUES (1, 'image.png', 'image.png', 'image/png', '');
        INSERT INTO PlaylistItemIndependentMediaMap VALUES (1, 1, 0);
        """,
        entries={"image.png": b"image"},
    )
    events = []

    document = read_jwlplaylist(
        archive,
        progress_callback=lambda phase, completed, total: events.append(
            (phase, completed, total)
        ),
    )

    assert document["items"][0]["title"] == "Image"
    assert ("opening", 1, 1) in events
    assert ("items", 1, 1) in events
    assert any(phase == "database" and completed == total for phase, completed, total in events)
    assert any(phase == "media_bytes" and completed == total for phase, completed, total in events)


def test_read_jwlplaylist_retains_occurrence_ids_for_repeated_media(tmp_path):
    archive = _write_playlist_archive(
        tmp_path,
        """
        CREATE TABLE PlaylistItem (PlaylistItemId INTEGER, Label TEXT, Position INTEGER);
        CREATE TABLE IndependentMedia (
            IndependentMediaId INTEGER, FilePath TEXT, OriginalFilename TEXT, MimeType TEXT, Hash TEXT
        );
        CREATE TABLE PlaylistItemIndependentMediaMap (
            PlaylistItemId INTEGER, IndependentMediaId INTEGER, DurationTicks INTEGER
        );
        INSERT INTO PlaylistItem VALUES (91, 'Second', 1), (72, 'First', 0);
        INSERT INTO IndependentMedia VALUES (1, 'video.mp4', 'video.mp4', 'video/mp4', '');
        INSERT INTO PlaylistItemIndependentMediaMap VALUES (91, 1, 0), (72, 1, 0);
        """,
        entries={"video.mp4": b"video"},
    )
    first = read_jwlplaylist(archive)
    second = read_jwlplaylist(archive)
    assert [item["source_item_id"] for item in first["items"]] == ["72", "91"]
    assert [item["source_item_id"] for item in second["items"]] == ["72", "91"]
