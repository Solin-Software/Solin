"""Small synthetic publications for catalog and selected materialization tests."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

from tests._jwpub_fixture import _synthetic_png

IMAGE = _synthetic_png()
PREVIEW = _synthetic_png() + b"preview"


def build_catalog_jwpub(
    destination: Path,
    *,
    direct: bool = False,
    grouped: bool = True,
    schema_sql: str = "",
    extra_outer: dict[str, bytes] | None = None,
    extra_inner: dict[str, bytes] | None = None,
) -> Path:
    database = destination.with_suffix(".db")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript("""
            CREATE TABLE Publication (PublicationId INTEGER PRIMARY KEY, Title TEXT,
                Symbol TEXT, MepsLanguageIndex INTEGER);
            INSERT INTO Publication VALUES (1, 'Synthetic publication', 'fixture', 5);
            CREATE TABLE Multimedia (MultimediaId INTEGER PRIMARY KEY, DocumentId INTEGER,
                LinkMultimediaId INTEGER, FilePath TEXT, MimeType TEXT, CategoryType INTEGER,
                Label TEXT, Caption TEXT, KeySymbol TEXT, Track INTEGER,
                IssueTagNumber INTEGER, MepsDocumentId INTEGER, MepsLanguageIndex INTEGER);
        """)
        # Two distinct paths have identical bytes. Video 4 owns image 5 as a
        # preview. Media 9 intentionally has no document relationship.
        rows = [
            (1, 10, None, "images/first.png", "image/png", 8, "First image", "", "", 0, 0, 0, None),
            (
                2,
                20,
                None,
                "images/duplicate.png",
                "image/png",
                8,
                "Shared image",
                "",
                "",
                0,
                0,
                0,
                None,
            ),
            (3, 10, None, "cover.png", "image/png", 9, "Cover", "", "", 0, 0, 0, None),
            (4, 10, 5, "", "video/mp4", -1, "Video", "", "remote", 4, 20270300, 104, 5),
            (5, 10, None, "preview.png", "image/png", 17, "Preview", "", "", 0, 0, 0, None),
            (6, 10, None, "", "audio/mpeg", None, "Audio", "", "sound", 6, 0, 0, 0),
            (7, 20, None, "embedded.mp4", "video/mp4", -1, "Local video", "", "", 0, 0, 0, None),
            (8, 10, None, "missing.png", "image/png", 8, "Missing", "", "", 0, 0, 0, None),
            (9, None, None, "orphan.png", "image/png", 8, "Orphan", "", "", 0, 0, 0, None),
            (10, 20, None, "", "video/mp4", -1, "Unresolved", "", "", 0, 0, 990, None),
        ]
        if not direct:
            rows = [(row[0], None, *row[2:]) for row in rows]
        connection.executemany("INSERT INTO Multimedia VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if grouped:
            connection.executescript("""
                CREATE TABLE Document (DocumentId INTEGER PRIMARY KEY, Title TEXT);
                INSERT INTO Document VALUES (20, 'Second'), (10, 'First');
            """)
            if not direct:
                connection.execute("""CREATE TABLE DocumentMultimedia (
                    DocumentMultimediaId INTEGER PRIMARY KEY, DocumentId INTEGER,
                    MultimediaId INTEGER, BeginParagraphOrdinal INTEGER)""")
                pairs = [
                    (10, 4),
                    (10, 1),
                    (10, 1),
                    (10, 6),
                    (10, 5),
                    (10, 3),
                    (20, 2),
                    (20, 7),
                    (20, 4),
                    (20, 10),
                ]
                connection.executemany(
                    "INSERT INTO DocumentMultimedia VALUES (?,?,?,?)",
                    [
                        (index, document, media, index)
                        for index, (document, media) in enumerate(pairs, 1)
                    ],
                )
        if schema_sql:
            connection.executescript(schema_sql)
    try:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as inner:
            inner.writestr("nested/publication.db", database.read_bytes())
            for name, payload in {
                "images/first.png": IMAGE,
                "images/duplicate.png": IMAGE,
                "cover.png": IMAGE,
                "preview.png": PREVIEW,
                "orphan.png": IMAGE + b"orphan",
                "embedded.mp4": b"embedded video bytes",
                **(extra_inner or {}),
            }.items():
                _write_member(inner, name, payload)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as outer:
            outer.writestr("contents", output.getvalue())
            outer.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "publication": {
                            "title": "Manifest title",
                            "symbol": "manifest",
                            "language": 5,
                            "fileName": "nested/publication.db",
                        }
                    }
                ),
            )
            for name, payload in (extra_outer or {}).items():
                _write_member(outer, name, payload)
    finally:
        database.unlink()
    return destination


def build_alias_jwpub(destination: Path, *, schema_sql: str = "") -> Path:
    """Two image records for one member and paragraph, with incomplete dimensions."""
    return build_catalog_jwpub(
        destination,
        schema_sql="""
            DELETE FROM Multimedia WHERE MultimediaId NOT IN (1, 2);
            UPDATE Multimedia SET FilePath = 'images/first.png', Label = 'Image', Caption = '';
            ALTER TABLE Multimedia ADD COLUMN Width INTEGER;
            ALTER TABLE Multimedia ADD COLUMN Height INTEGER;
            ALTER TABLE Multimedia ADD COLUMN DataType INTEGER;
            ALTER TABLE Multimedia ADD COLUMN MajorType INTEGER;
            ALTER TABLE Multimedia ADD COLUMN MinorType INTEGER;
            ALTER TABLE Multimedia ADD COLUMN LabelRich TEXT;
            ALTER TABLE Multimedia ADD COLUMN CaptionRich TEXT;
            ALTER TABLE Multimedia ADD COLUMN CaptionContent BLOB;
            ALTER TABLE Multimedia ADD COLUMN CreditLine TEXT;
            ALTER TABLE Multimedia ADD COLUMN CreditLineRich TEXT;
            ALTER TABLE Multimedia ADD COLUMN CreditLineContent BLOB;
            ALTER TABLE Multimedia ADD COLUMN SuppressZoom INTEGER;
            ALTER TABLE Multimedia ADD COLUMN SizeConstraint INTEGER;
            ALTER TABLE Multimedia ADD COLUMN Specialty TEXT;
            ALTER TABLE Multimedia ADD COLUMN Edition TEXT;
            UPDATE Multimedia SET Width = 1920, Height = 1080 WHERE MultimediaId = 1;
            ALTER TABLE DocumentMultimedia ADD COLUMN EndParagraphOrdinal INTEGER;
            DELETE FROM DocumentMultimedia;
            INSERT INTO DocumentMultimedia VALUES (1, 10, 1, 2, 2), (2, 10, 2, 2, 2);
        """
        + schema_sql,
    )


def _write_member(archive: zipfile.ZipFile, name: str, payload: bytes) -> None:
    member = zipfile.ZipInfo(name)
    # Preserve deliberately unsafe raw ZIP names on Windows too.
    member.filename = member.orig_filename = name
    member.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(member, payload)
