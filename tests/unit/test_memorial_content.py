from __future__ import annotations

import io
import sqlite3
import zipfile

from solin.core.meetings.jwpub_cache import JwpubCache
from solin.core.meetings.memorial_content import (
    extract_memorial_jwpub,
    memorial_publication_symbol,
    read_memorial_publication_content,
)


def test_memorial_publication_symbol_uses_two_digit_year_suffix():
    assert memorial_publication_symbol(2026) == "mi26"


def test_extract_memorial_jwpub_extracts_outer_and_inner_archive(tmp_path):
    cache = JwpubCache(tmp_path)
    jwpub_path = cache.jwpub_path("mi26", "T", "0")

    inner_bytes = io.BytesIO()
    with zipfile.ZipFile(inner_bytes, "w") as inner:
        inner.writestr("memorial.db", b"sqlite bytes")
    with zipfile.ZipFile(jwpub_path, "w") as outer:
        outer.writestr("contents", inner_bytes.getvalue())
        outer.writestr("cover_univ_sqr.jpg", b"thumb")

    extract_dir = extract_memorial_jwpub("mi26", "T", "0", cache)

    assert extract_dir == tmp_path / "mi26_T" / "x_0"
    assert extract_dir is not None
    assert (extract_dir / "memorial.db").read_bytes() == b"sqlite bytes"
    assert (extract_dir / "cover_univ_sqr.jpg").read_bytes() == b"thumb"


def test_extract_memorial_jwpub_rejects_inner_members_outside_extract_dir(tmp_path):
    cache = JwpubCache(tmp_path)
    jwpub_path = cache.jwpub_path("mi26", "T", "0")

    inner_bytes = io.BytesIO()
    with zipfile.ZipFile(inner_bytes, "w") as inner:
        inner.writestr("../escape.db", b"nope")
    with zipfile.ZipFile(jwpub_path, "w") as outer:
        outer.writestr("contents", inner_bytes.getvalue())

    assert extract_memorial_jwpub("mi26", "T", "0", cache) is None
    assert not (tmp_path / "escape.db").exists()


def test_read_memorial_publication_content_prefers_square_thumb_and_media(tmp_path):
    db_path = tmp_path / "memorial.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE Multimedia (
                MultimediaId INTEGER,
                FilePath TEXT,
                MimeType TEXT,
                CategoryType INTEGER,
                Label TEXT,
                Caption TEXT,
                KeySymbol TEXT,
                Track INTEGER,
                IssueTagNumber INTEGER,
                MepsDocumentId INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO Multimedia VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "cover.jpg", "image/jpeg", 26, "Cover", "", "", 0, 0, 0),
                (2, "intro.mp4", "video/mp4", -1, "Intro", "", "mi26", 1, 0, 0),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    (tmp_path / "cover.jpg").write_bytes(b"cover")
    (tmp_path / "cover_univ_sqr.jpg").write_bytes(b"thumb")

    content = read_memorial_publication_content(tmp_path)

    assert content.cover_bytes == b"thumb"
    assert [item.multimedia_id for item in content.media_items] == [1, 2]
    assert content.media_items[0].file_path == str(tmp_path / "cover.jpg")
    assert content.media_items[1].file_path == "intro.mp4"
