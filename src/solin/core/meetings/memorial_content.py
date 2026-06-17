from __future__ import annotations

import logging
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import models as meeting_models
from .jwpub_cache import JwpubCache

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemorialSqliteData:
    cover: dict[str, Any] | None
    videos: list[dict[str, Any]]
    thumb_path: Path | None


@dataclass(frozen=True, slots=True)
class MemorialPublicationContent:
    cover_bytes: bytes | None
    media_items: list[meeting_models.MeetingMedia]


def memorial_publication_symbol(year: int) -> str:
    return f"mi{str(year)[2:]}"


def extract_memorial_jwpub(
    pub: str,
    lang: str,
    issue: str,
    cache: JwpubCache,
) -> Path | None:
    jwpub_path = cache.jwpub_path(pub, lang, issue)
    if not jwpub_path.exists():
        return None

    extract_path = cache.extract_dir(pub, lang, issue)
    if extract_path.exists() and any(extract_path.glob("*.db")):
        return extract_path

    extract_path.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(jwpub_path) as outer:
            _safe_extract_all(outer, extract_path)

        contents = extract_path / "contents"
        if contents.is_file():
            with zipfile.ZipFile(contents) as inner:
                _safe_extract_all(inner, extract_path)

        if any(extract_path.glob("*.db")):
            return extract_path
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        log.error("extract_memorial_jwpub %s: %s", jwpub_path, exc)
    return None


def _safe_extract_all(archive: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for member in archive.infolist():
        member_path = (target_dir / member.filename).resolve()
        if member_path != target_root and target_root not in member_path.parents:
            raise RuntimeError(f"Archive member escapes extraction dir: {member.filename}")
    archive.extractall(target_dir)


def query_memorial_sqlite(pub_dir: Path) -> MemorialSqliteData:
    dbs = list(pub_dir.glob("*.db"))
    if not dbs:
        return MemorialSqliteData(cover=None, videos=[], thumb_path=None)

    db_path = dbs[0]
    cover: dict[str, Any] | None = None
    videos: list[dict[str, Any]] = []
    thumb_path: Path | None = None
    conn: sqlite3.Connection | None = None

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        has_dm = bool(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='DocumentMultimedia'"
            ).fetchone()[0]
        )

        join = (
            "INNER JOIN DocumentMultimedia dm "
            "ON m.MultimediaId = dm.MultimediaId"
        ) if has_dm else ""
        dm_col = ", dm.BeginParagraphOrdinal AS par" if has_dm else ""

        row = conn.execute(
            f"""
            SELECT m.*{dm_col}
            FROM Multimedia m {join}
            WHERE m.CategoryType = 26
            LIMIT 1
            """
        ).fetchone()
        if row:
            cover = dict(row)

        for path in pub_dir.iterdir():
            if path.is_file() and "univ_sqr" in path.name:
                thumb_path = path
                break

        null_cond = " AND dm.BeginParagraphOrdinal IS NULL" if has_dm else ""
        rows = conn.execute(
            f"""
            SELECT m.*{dm_col}
            FROM Multimedia m {join}
            WHERE m.CategoryType = -1{null_cond}
            """
        ).fetchall()
        videos = [dict(row) for row in rows]
    except (OSError, sqlite3.Error) as exc:
        log.error("query_memorial_sqlite %s: %s", db_path, exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                log.debug("Could not close memorial database %s", db_path, exc_info=True)

    return MemorialSqliteData(cover=cover, videos=videos, thumb_path=thumb_path)


def read_memorial_publication_content(pub_dir: Path) -> MemorialPublicationContent:
    sqlite_data = query_memorial_sqlite(pub_dir)
    cover_bytes = _read_cover_bytes(pub_dir, sqlite_data)

    media_items: list[meeting_models.MeetingMedia] = []
    if sqlite_data.cover:
        media_items.append(make_memorial_media(sqlite_data.cover, pub_dir))
    media_items.extend(
        make_memorial_media(video, pub_dir)
        for video in sqlite_data.videos
    )

    return MemorialPublicationContent(
        cover_bytes=cover_bytes,
        media_items=media_items,
    )


def _read_cover_bytes(pub_dir: Path, sqlite_data: MemorialSqliteData) -> bytes | None:
    if sqlite_data.thumb_path and sqlite_data.thumb_path.exists():
        try:
            return sqlite_data.thumb_path.read_bytes()
        except OSError:
            log.debug("Failed to read memorial thumbnail bytes", exc_info=True)

    if sqlite_data.cover:
        file_path = str(sqlite_data.cover.get("FilePath") or "")
        if file_path:
            cover_path = pub_dir / file_path
            if cover_path.exists():
                try:
                    return cover_path.read_bytes()
                except OSError:
                    log.debug("Failed to read memorial cover bytes", exc_info=True)
    return None


def make_memorial_media(
    value: dict[str, Any],
    pub_dir: Path,
) -> meeting_models.MeetingMedia:
    mime_type = str(value.get("MimeType") or "").lower()
    file_path = str(value.get("FilePath") or "")
    resolved_file_path = str(pub_dir / file_path) if file_path and mime_type.startswith("image") else file_path
    return meeting_models.MeetingMedia(
        multimedia_id=int(value.get("MultimediaId") or 0),
        mime_type=mime_type,
        file_path=resolved_file_path,
        label=str(value.get("Label") or ""),
        caption=str(value.get("Caption") or ""),
        key_symbol=str(value.get("KeySymbol") or ""),
        track=int(value.get("Track") or 0),
        issue_tag=int(value.get("IssueTagNumber") or 0),
        meps_doc_id=int(value.get("MepsDocumentId") or 0),
        section="memorial",
    )


__all__ = [
    "MemorialPublicationContent",
    "MemorialSqliteData",
    "extract_memorial_jwpub",
    "make_memorial_media",
    "memorial_publication_symbol",
    "query_memorial_sqlite",
    "read_memorial_publication_content",
]
