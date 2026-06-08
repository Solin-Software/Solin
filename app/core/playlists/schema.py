"""SQLite schema helpers for JW Library playlist packages."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION: int = 14

CREATE_SCHEMA_SQL: str = r"""
        CREATE TABLE IF NOT EXISTS PlaylistItem (
            PlaylistItemId          INTEGER PRIMARY KEY,
            Label                   TEXT,
            StartTrimOffsetTicks    INTEGER,
            EndTrimOffsetTicks      INTEGER,
            Accuracy                INTEGER DEFAULT 1,
            EndAction               INTEGER DEFAULT 0,
            ThumbnailFilePath       TEXT
        );

        CREATE TABLE IF NOT EXISTS IndependentMedia (
            IndependentMediaId  INTEGER PRIMARY KEY,
            OriginalFilename    TEXT,
            FilePath            TEXT    NOT NULL,
            MimeType            TEXT,
            Hash                TEXT
        );

        CREATE TABLE IF NOT EXISTS PlaylistItemIndependentMediaMap (
            PlaylistItemId      INTEGER NOT NULL,
            IndependentMediaId  INTEGER NOT NULL,
            DurationTicks       INTEGER,
            PRIMARY KEY (PlaylistItemId, IndependentMediaId),
            FOREIGN KEY (PlaylistItemId)     REFERENCES PlaylistItem(PlaylistItemId),
            FOREIGN KEY (IndependentMediaId) REFERENCES IndependentMedia(IndependentMediaId)
        );

        CREATE TABLE IF NOT EXISTS Location (
            LocationId          INTEGER PRIMARY KEY,
            BookNumber          INTEGER,
            ChapterNumber       INTEGER,
            DocumentId          INTEGER,
            Track               INTEGER,
            IssueTagNumber      INTEGER DEFAULT 0,
            KeySymbol           TEXT,
            MepsLanguage        INTEGER DEFAULT 0,
            Type                INTEGER DEFAULT 3,
            Title               TEXT    DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS PlaylistItemLocationMap (
            PlaylistItemId          INTEGER NOT NULL,
            LocationId              INTEGER NOT NULL,
            MajorMultimediaType     INTEGER,
            BaseDurationTicks       INTEGER,
            PRIMARY KEY (PlaylistItemId, LocationId),
            FOREIGN KEY (PlaylistItemId) REFERENCES PlaylistItem(PlaylistItemId),
            FOREIGN KEY (LocationId)     REFERENCES Location(LocationId)
        );

        -- BUG 2 CORRIGIDO: tabelas Tag e TagMap são obrigatórias para o JW Library
        -- reconhecer e abrir a playlist.
        CREATE TABLE IF NOT EXISTS Tag (
            TagId   INTEGER PRIMARY KEY,
            Type    INTEGER DEFAULT 2,
            Name    TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS TagMap (
            TagMapId        INTEGER PRIMARY KEY,
            PlaylistItemId  INTEGER,
            LocationId      INTEGER,
            NoteId          INTEGER,
            TagId           INTEGER NOT NULL,
            Position        INTEGER NOT NULL,
            FOREIGN KEY (PlaylistItemId) REFERENCES PlaylistItem(PlaylistItemId),
            FOREIGN KEY (TagId)          REFERENCES Tag(TagId)
        );

        -- Tabelas auxiliares presentes no schema real (podem ficar vazias)
        CREATE TABLE IF NOT EXISTS BlockRange (
            BlockRangeId    INTEGER PRIMARY KEY,
            BlockType       INTEGER NOT NULL,
            Identifier      INTEGER NOT NULL,
            StartToken      INTEGER,
            EndToken        INTEGER,
            UserMarkId      INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS Bookmark (
            BookmarkId          INTEGER PRIMARY KEY,
            LocationId          INTEGER NOT NULL,
            PublicationLocationId INTEGER NOT NULL,
            Slot                INTEGER NOT NULL,
            Title               TEXT,
            Snippet             TEXT,
            BlockType           INTEGER DEFAULT 0,
            BlockIdentifier     INTEGER
        );
        CREATE TABLE IF NOT EXISTS InputField (
            LocationId  INTEGER NOT NULL,
            TextTag     TEXT    NOT NULL,
            Value       TEXT,
            PRIMARY KEY (LocationId, TextTag)
        );
        CREATE TABLE IF NOT EXISTS Note (
            NoteId          INTEGER PRIMARY KEY,
            Guid            TEXT NOT NULL UNIQUE,
            UserMarkId      INTEGER,
            LocationId      INTEGER,
            Title           TEXT,
            Content         TEXT,
            LastModified    TEXT,
            Created         TEXT,
            BlockType       INTEGER DEFAULT 0,
            BlockIdentifier INTEGER
        );
        CREATE TABLE IF NOT EXISTS UserMark (
            UserMarkId      INTEGER PRIMARY KEY,
            ColorIndex      INTEGER NOT NULL,
            LocationId      INTEGER NOT NULL,
            StyleIndex      INTEGER NOT NULL,
            UserMarkGuid    TEXT NOT NULL UNIQUE,
            Version         INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS PlaylistItemMarker (
            PlaylistItemMarkerId    INTEGER PRIMARY KEY,
            PlaylistItemId          INTEGER NOT NULL,
            Label                   TEXT,
            StartTimeTicks          INTEGER NOT NULL,
            DurationTicks           INTEGER NOT NULL,
            EndTransitionDurationTicks INTEGER
        );
        CREATE TABLE IF NOT EXISTS PlaylistItemMarkerBibleVerseMap (
            PlaylistItemMarkerId    INTEGER NOT NULL,
            VerseId                 INTEGER NOT NULL,
            PRIMARY KEY (PlaylistItemMarkerId, VerseId)
        );
        CREATE TABLE IF NOT EXISTS PlaylistItemMarkerParagraphMap (
            PlaylistItemMarkerId    INTEGER NOT NULL,
            MepsDocumentId          INTEGER NOT NULL,
            ParagraphIndex          INTEGER NOT NULL,
            MarkerIndexWithinParagraph INTEGER NOT NULL,
            PRIMARY KEY (PlaylistItemMarkerId, MepsDocumentId, ParagraphIndex)
        );
        CREATE TABLE IF NOT EXISTS PlaylistItemAccuracy (
            PlaylistItemAccuracyId  INTEGER PRIMARY KEY,
            Description             TEXT
        );
        INSERT INTO PlaylistItemAccuracy VALUES (1, 'Accurate');
        INSERT INTO PlaylistItemAccuracy VALUES (2, 'NeedsUserVerification');

        CREATE TABLE IF NOT EXISTS LastModified (
            LastModified    TEXT
        );
        INSERT INTO LastModified VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
"""


def create_jwlplaylist_schema(con: sqlite3.Connection) -> None:
    """Create the SQLite schema expected by JW Library playlist imports."""
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.executescript(CREATE_SCHEMA_SQL)
