from __future__ import annotations

import sqlite3
import zipfile

import pytest

from app.core.playlists.reader import PlaylistReadError, read_jwlplaylist


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
