from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import struct
import zlib
import zipfile
from pathlib import Path


_ARCHIVE_TIMESTAMP = (2000, 1, 1, 0, 0, 0)
_MANIFEST_TIMESTAMP = "2000-01-01T00:00:00Z"
_DATABASE_NAME = "synthetic_meeting_workbook.db"
_IMAGE_NAME = "synthetic_illustration.png"


def build_synthetic_jwpub(destination: Path) -> Path:
    """Build a deterministic, copyright-free JWPub archive for tests."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    database_path = destination.with_suffix(".db")
    try:
        _write_database(database_path)
        database_bytes = database_path.read_bytes()
    finally:
        database_path.unlink(missing_ok=True)

    image_bytes = _synthetic_png()
    contents = _inner_archive(database_bytes, image_bytes)
    manifest = _manifest(database_bytes, contents, len(database_bytes) + len(image_bytes))

    with zipfile.ZipFile(destination, "w") as archive:
        _write_archive_entry(archive, "contents", contents)
        _write_archive_entry(
            archive,
            "manifest.json",
            json.dumps(
                manifest,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )
    return destination


def _write_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA page_size = 4096;

            CREATE TABLE DatedText (
                DatedTextId INTEGER PRIMARY KEY,
                DocumentId INTEGER NOT NULL,
                FirstDateOffset INTEGER NOT NULL
            );

            CREATE TABLE Document (
                DocumentId INTEGER PRIMARY KEY,
                Class INTEGER,
                Title TEXT
            );

            CREATE TABLE Multimedia (
                MultimediaId INTEGER PRIMARY KEY,
                FilePath TEXT,
                MimeType TEXT,
                CategoryType INTEGER,
                Label TEXT,
                Caption TEXT,
                KeySymbol TEXT,
                Track INTEGER,
                IssueTagNumber INTEGER,
                MepsDocumentId INTEGER,
                SuppressZoom BOOLEAN
            );

            CREATE TABLE DocumentMultimedia (
                DocumentMultimediaId INTEGER PRIMARY KEY,
                DocumentId INTEGER NOT NULL,
                MultimediaId INTEGER NOT NULL,
                BeginParagraphOrdinal INTEGER,
                EndParagraphOrdinal INTEGER
            );

            CREATE TABLE RefPublication (
                RefPublicationId INTEGER PRIMARY KEY,
                Title TEXT,
                ShortTitle TEXT,
                DisplayTitle TEXT,
                ReferenceTitle TEXT,
                UndatedReferenceTitle TEXT,
                UndatedSymbol TEXT NOT NULL,
                IssueTagNumber TEXT,
                PublicationType TEXT NOT NULL
            );

            CREATE TABLE Extract (
                ExtractId INTEGER PRIMARY KEY,
                Caption TEXT,
                RefPublicationId INTEGER NOT NULL,
                RefMepsDocumentId INTEGER NOT NULL
            );

            CREATE TABLE DocumentExtract (
                DocumentExtractId INTEGER PRIMARY KEY,
                DocumentId INTEGER NOT NULL,
                ExtractId INTEGER NOT NULL,
                BeginParagraphOrdinal INTEGER,
                EndParagraphOrdinal INTEGER
            );
            """
        )

        connection.execute(
            "INSERT INTO Document (DocumentId, Class, Title) VALUES (?, ?, ?)",
            (1000, 0, "Synthetic meeting document"),
        )
        connection.execute(
            "INSERT INTO DatedText (DatedTextId, DocumentId, FirstDateOffset) VALUES (?, ?, ?)",
            (1, 1000, 20260525),
        )

        multimedia_rows = [
            (1, "", "video/mp4", 9, "Opening song", "", "sjjm", 1, 0, 0, 0),
            (2, "", "video/mp4", 9, "Middle song", "", "sjjm", 2, 0, 0, 0),
            (3, "", "video/mp4", 9, "Closing song", "", "sjjm", 3, 0, 0, 0),
            (
                4,
                _IMAGE_NAME,
                "image/png",
                1,
                "Synthetic illustration",
                "",
                "",
                0,
                0,
                0,
                0,
            ),
            (
                5,
                "",
                "video/mp4",
                1,
                "Synthetic video",
                "",
                "synthetic",
                1,
                20260500,
                5001,
                0,
            ),
        ]
        connection.executemany(
            """
            INSERT INTO Multimedia (
                MultimediaId, FilePath, MimeType, CategoryType, Label, Caption,
                KeySymbol, Track, IssueTagNumber, MepsDocumentId, SuppressZoom
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            multimedia_rows,
        )
        connection.executemany(
            """
            INSERT INTO DocumentMultimedia (
                DocumentMultimediaId, DocumentId, MultimediaId,
                BeginParagraphOrdinal, EndParagraphOrdinal
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (1, 1000, 1, 1, 1),
                (2, 1000, 2, 18, 18),
                (3, 1000, 3, 40, 40),
                (4, 1000, 4, 8, 8),
                (5, 1000, 5, 12, 12),
            ],
        )

        publication_rows = [
            (
                1,
                "Synthetic Reference Manual",
                "Reference Manual",
                "Synthetic Reference Manual",
                "Synthetic Reference Manual",
                "Synthetic Reference Manual",
                "reference",
                "0",
                "Book",
            ),
            (
                2,
                "Synthetic Study Guide",
                "Study Guide",
                "Synthetic Study Guide",
                "Synthetic Study Guide",
                "Synthetic Study Guide",
                "studyguide",
                "0",
                "Book",
            ),
            (
                3,
                "Synthetic excluded reference",
                "Excluded",
                "Synthetic excluded reference",
                "Synthetic excluded reference",
                "Synthetic excluded reference",
                "th",
                "0",
                "Book",
            ),
        ]
        connection.executemany(
            """
            INSERT INTO RefPublication (
                RefPublicationId, Title, ShortTitle, DisplayTitle, ReferenceTitle,
                UndatedReferenceTitle, UndatedSymbol, IssueTagNumber, PublicationType
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            publication_rows,
        )
        connection.executemany(
            """
            INSERT INTO Extract (
                ExtractId, Caption, RefPublicationId, RefMepsDocumentId
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (1, "Synthetic reference topic", 1, 2001),
                (2, "Synthetic study - Part 1", 2, 3001),
                (3, "Synthetic study - Part 2", 2, 3002),
                (4, "Synthetic excluded topic", 3, 4001),
            ],
        )
        connection.executemany(
            """
            INSERT INTO DocumentExtract (
                DocumentExtractId, DocumentId, ExtractId,
                BeginParagraphOrdinal, EndParagraphOrdinal
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (1, 1000, 1, 10, 10),
                (2, 1000, 4, 20, 20),
                (3, 1000, 2, 30, 30),
                (4, 1000, 3, 31, 31),
            ],
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def _inner_archive(database_bytes: bytes, image_bytes: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        _write_archive_entry(archive, _DATABASE_NAME, database_bytes)
        _write_archive_entry(archive, _IMAGE_NAME, image_bytes)
    return output.getvalue()


def _manifest(
    database_bytes: bytes,
    contents: bytes,
    expanded_size: int,
) -> dict[str, object]:
    return {
        "contentFormat": "z-a",
        "expandedSize": expanded_size,
        "hash": hashlib.sha256(contents).hexdigest(),
        "htmlValidated": False,
        "name": "synthetic_meeting_workbook.jwpub",
        "publication": {
            "displayTitle": "Synthetic Meeting Workbook",
            "fileName": _DATABASE_NAME,
            "hash": hashlib.sha1(database_bytes).hexdigest(),  # noqa: S324
            "issueId": 20260500,
            "issueNumber": 5,
            "language": 0,
            "publicationType": "Meeting Workbook",
            "referenceTitle": "Synthetic Meeting Workbook",
            "schemaVersion": 9,
            "shortTitle": "Synthetic Workbook",
            "symbol": "synthetic_mwb",
            "timestamp": _MANIFEST_TIMESTAMP,
            "title": "Synthetic Meeting Workbook",
            "type": 3,
            "undatedReferenceTitle": "Synthetic Meeting Workbook",
            "undatedSymbol": "synthetic_mwb",
            "uniqueEnglishSymbol": "synthetic_mwb",
            "uniqueSymbol": "synthetic_mwb",
            "variation": "",
            "year": 2026,
        },
        "timestamp": _MANIFEST_TIMESTAMP,
        "version": 1,
    }


def _synthetic_png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x33\x66\x99", level=9)
    return (
        signature
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", pixels)
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(data, checksum)
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _write_archive_entry(
    archive: zipfile.ZipFile,
    name: str,
    content: bytes,
) -> None:
    info = zipfile.ZipInfo(name, date_time=_ARCHIVE_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content)


__all__ = ["build_synthetic_jwpub"]
