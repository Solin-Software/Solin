from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import models as meeting_models

log = logging.getLogger(__name__)

SONG_SYMBOLS = {"sjj", "sjjm"}
EXCLUDED_MULTIMEDIA_CATEGORIES = {9, 10, 15, 25}
WATCHTOWER_DOCUMENT_CLASS = 40
MWB_APPLY_YOURSELF_PARAGRAPH = 18
SKIP_MEETING_REFERENCE_PUBLICATIONS = {"th", "lmd"}
MWB_STUDY_REFERENCE_TYPES = {"Book", "Brochure", "Watchtower", "Article"}


@dataclass(frozen=True, slots=True)
class MwbPublicationContent:
    date_label: str
    media_items: list[meeting_models.MeetingMedia]
    publication_refs: list[meeting_models.MeetingPublicationRef]
    cover_bytes: bytes | None
    cbs_ref: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class WatchtowerPublicationContent:
    title: str
    media_items: list[meeting_models.MeetingMedia]
    cover_bytes: bytes | None


def open_publication_database(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def find_mwb_document_id(conn: Any, monday: date) -> int | None:
    target = int(monday.strftime("%Y%m%d"))
    try:
        row = conn.execute(
            "SELECT DocumentId FROM DatedText WHERE FirstDateOffset=? LIMIT 1",
            (target,),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def find_watchtower_document_id(conn: Any, monday: date) -> int | None:
    target = int(monday.strftime("%Y%m%d"))
    try:
        rows = conn.execute(
            "SELECT FirstDateOffset FROM DatedText ORDER BY FirstDateOffset"
        ).fetchall()
        week_nr = next((i for i, row in enumerate(rows) if row[0] == target), -1)
        if week_nr == -1:
            return None
        row = conn.execute(
            "SELECT DocumentId FROM Document WHERE Class=? "
            "ORDER BY DocumentId LIMIT 1 OFFSET ?",
            (WATCHTOWER_DOCUMENT_CLASS, week_nr),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def is_week_in_dated_text(conn: Any, monday: date) -> bool:
    target = int(monday.strftime("%Y%m%d"))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM DatedText WHERE FirstDateOffset=?",
            (target,),
        ).fetchone()
        return bool(row and row[0] > 0)
    except sqlite3.Error:
        return False


def watchtower_issue_contains_week(db_path: Path, monday: date) -> bool:
    conn = open_publication_database(db_path)
    try:
        return find_watchtower_document_id(conn, monday) is not None
    finally:
        conn.close()


def document_title(conn: Any, doc_id: int) -> str:
    try:
        row = conn.execute(
            "SELECT Title FROM Document WHERE DocumentId=?",
            (doc_id,),
        ).fetchone()
        return (row[0] or "").strip() if row else ""
    except sqlite3.Error:
        return ""


def read_cover_bytes(pub_dir: Path) -> bytes | None:
    dbs = list(pub_dir.glob("*.db"))
    if not dbs:
        return None
    try:
        conn = open_publication_database(dbs[0])
        try:
            for category in (9, 15):
                row = conn.execute(
                    "SELECT FilePath FROM Multimedia WHERE CategoryType=? LIMIT 1",
                    (category,),
                ).fetchone()
                if row and row[0]:
                    image_path = pub_dir / row[0]
                    if image_path.exists():
                        return image_path.read_bytes()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        log.debug("Failed to read cover bytes from publication database", exc_info=True)
    return None


def row_value(row: Any, key: str) -> str:
    try:
        return row[key] or ""
    except (IndexError, KeyError, TypeError):
        return ""


def clean_reference_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return re.sub(r"\s+", " ", text).strip()


def publication_title_from_row(row: Any, fallback: str) -> str:
    for key in (
        "display_title",
        "reference_title",
        "short_title",
        "title",
        "undated_reference_title",
    ):
        title = clean_reference_text(row_value(row, key))
        if title:
            return title
    return fallback


def mwb_song_ordinals(conn: Any, doc_id: int) -> list[int]:
    try:
        return [
            int(row["par"] or 0)
            for row in conn.execute(
                "SELECT dm.BeginParagraphOrdinal AS par FROM Multimedia m "
                "JOIN DocumentMultimedia dm ON dm.MultimediaId=m.MultimediaId "
                "WHERE dm.DocumentId=? AND m.KeySymbol IN ('sjj','sjjm') "
                "AND dm.BeginParagraphOrdinal IS NOT NULL ORDER BY par",
                (doc_id,),
            ).fetchall()
        ]
    except sqlite3.Error:
        log.debug("Failed to read MWB song ordinals", exc_info=True)
        return []


def mwb_mid_ordinal_from_songs(song_ordinals: list[int]) -> int:
    return song_ordinals[1] if len(song_ordinals) >= 2 else 9999


def mwb_final_song_ordinal_from_songs(song_ordinals: list[int]) -> int:
    return song_ordinals[-1] if len(song_ordinals) >= 3 else 9999


def mwb_mid_ordinal(conn: Any, doc_id: int) -> int:
    return mwb_mid_ordinal_from_songs(mwb_song_ordinals(conn, doc_id))


def mwb_final_song_ordinal(conn: Any, doc_id: int) -> int:
    return mwb_final_song_ordinal_from_songs(mwb_song_ordinals(conn, doc_id))


def is_cbs_row(
    row: Any,
    cbs_ref: dict[str, Any] | None,
    final_song_ord: int = 9999,
) -> bool:
    if not cbs_ref:
        return False
    pub = str(row["undated"] or "").strip()
    meps_id = int(row["meps_id"] or 0)
    paragraph = int(row["par"] or 0)
    cbs_docs = {int(value) for value in cbs_ref.get("meps_doc_ids", [])}
    if pub != str(cbs_ref.get("pub") or "") or meps_id not in cbs_docs:
        return False
    if paragraph < int(cbs_ref.get("cbs_start") or 9999):
        return False
    return final_song_ord >= 9999 or paragraph < final_song_ord


def mwb_publication_ref_rows(
    conn: Any,
    doc_id: int,
    include_web: bool = False,
) -> list[Any]:
    pub_types = "'Book','Brochure','Watchtower','Article'"
    if include_web:
        pub_types = f"{pub_types},'Web'"
    try:
        return conn.execute(
            f"""
            SELECT de.BeginParagraphOrdinal AS par,
                   e.Caption,
                   e.RefMepsDocumentId       AS meps_id,
                   rp.UndatedSymbol          AS undated,
                   rp.IssueTagNumber         AS issue_tag,
                   rp.PublicationType        AS pub_type,
                   rp.DisplayTitle           AS display_title,
                   rp.ReferenceTitle         AS reference_title,
                   rp.ShortTitle             AS short_title,
                   rp.Title                  AS title,
                   rp.UndatedReferenceTitle  AS undated_reference_title
            FROM   DocumentExtract de
            JOIN   Extract e         ON e.ExtractId         = de.ExtractId
            JOIN   RefPublication rp ON rp.RefPublicationId = e.RefPublicationId
            WHERE  de.DocumentId = ?
              AND  rp.UndatedSymbol NOT IN ('sjj','sjjm')
              AND  rp.PublicationType IN ({pub_types})
            ORDER  BY de.BeginParagraphOrdinal ASC
            """,
            (doc_id,),
        ).fetchall()
    except sqlite3.Error:
        try:
            return conn.execute(
                f"""
                SELECT de.BeginParagraphOrdinal AS par,
                       e.Caption,
                       e.RefMepsDocumentId       AS meps_id,
                       rp.UndatedSymbol          AS undated,
                       rp.IssueTagNumber         AS issue_tag,
                       rp.PublicationType        AS pub_type,
                       NULL                      AS display_title,
                       NULL                      AS reference_title,
                       NULL                      AS short_title,
                       NULL                      AS title,
                       NULL                      AS undated_reference_title
                FROM   DocumentExtract de
                JOIN   Extract e         ON e.ExtractId         = de.ExtractId
                JOIN   RefPublication rp ON rp.RefPublicationId = e.RefPublicationId
                WHERE  de.DocumentId = ?
                  AND  rp.UndatedSymbol NOT IN ('sjj','sjjm')
                  AND  rp.PublicationType IN ({pub_types})
                ORDER  BY de.BeginParagraphOrdinal ASC
                """,
                (doc_id,),
            ).fetchall()
        except sqlite3.Error:
            log.debug("Failed to read MWB publication references", exc_info=True)
            return []


def mwb_study_ref_rows(rows: list[Any]) -> list[Any]:
    return [
        row
        for row in rows
        if str(row["pub_type"] or "") in MWB_STUDY_REFERENCE_TYPES
    ]


def infer_cbs_rows(rows: list[Any], final_song_ord: int) -> list[Any]:
    candidates = [
        row
        for row in rows
        if (row["undated"] or "").strip()
        and (final_song_ord >= 9999 or int(row["par"] or 0) < final_song_ord)
    ]
    if not candidates:
        return []

    ordered = sorted(candidates, key=lambda row: int(row["par"] or 0), reverse=True)
    top_symbol = str(ordered[0]["undated"] or "").strip()
    cbs_rows = []
    last_cbs_par = int(ordered[0]["par"] or 0)
    for row in ordered:
        pub = str(row["undated"] or "").strip()
        paragraph = int(row["par"] or 0)
        if pub == top_symbol:
            cbs_rows.append(row)
            last_cbs_par = paragraph
            continue
        if cbs_rows and paragraph < last_cbs_par:
            break
    return sorted(cbs_rows, key=lambda row: int(row["par"] or 0))


def mwb_cbs_start_from_rows(rows: list[Any], final_song_ord: int) -> int:
    rows = infer_cbs_rows(
        mwb_study_ref_rows(rows),
        final_song_ord,
    )
    if rows:
        return min(int(row["par"] or 9999) for row in rows)
    return 9999


def mwb_cbs_start(conn: Any, doc_id: int) -> int:
    song_ordinals = mwb_song_ordinals(conn, doc_id)
    return mwb_cbs_start_from_rows(
        mwb_publication_ref_rows(conn, doc_id),
        mwb_final_song_ordinal_from_songs(song_ordinals),
    )


def mwb_section_for_par(paragraph: int, mid_ord: int, cbs_start: int) -> str:
    if cbs_start < 9999 and paragraph >= cbs_start:
        return "lac"
    if paragraph >= mid_ord:
        return "lac"
    if paragraph >= MWB_APPLY_YOURSELF_PARAGRAPH:
        return "ayfm"
    return "tgw"


def get_cbs_reference(
    conn: Any,
    doc_id: int,
    rows: list[Any] | None = None,
    final_song_ord: int | None = None,
) -> dict[str, Any] | None:
    if rows is None:
        rows = mwb_publication_ref_rows(conn, doc_id)
    if final_song_ord is None:
        final_song_ord = mwb_final_song_ordinal(conn, doc_id)
    cbs_rows = infer_cbs_rows(mwb_study_ref_rows(rows), final_song_ord)
    if not cbs_rows:
        return None

    top_row = max(cbs_rows, key=lambda row: row["par"])
    top_symbol = top_row["undated"] or ""
    top_issue = str(top_row["issue_tag"] or "0")
    if not top_symbol:
        return None
    pub_title = ""
    for key in (
        "display_title",
        "reference_title",
        "short_title",
        "title",
        "undated_reference_title",
    ):
        pub_title = clean_reference_text(row_value(top_row, key))
        if pub_title:
            break
    meps_ids: list[int] = []
    doc_titles: dict[int, str] = {}
    cbs_start = min(int(row["par"] or 0) for row in cbs_rows)
    for row in cbs_rows:
        if (row["undated"] or "") != top_symbol:
            continue
        meps_id = row["meps_id"]
        if not meps_id:
            continue
        if meps_id not in meps_ids:
            meps_ids.append(meps_id)
        if meps_id not in doc_titles and row["Caption"]:
            doc_titles[meps_id] = clean_reference_text(row["Caption"])
    if not meps_ids:
        return None
    return {
        "pub": top_symbol,
        "issue": top_issue,
        "publication_title": pub_title or top_symbol,
        "meps_doc_ids": meps_ids,
        "doc_titles": doc_titles,
        "title": doc_titles.get(meps_ids[0], ""),
        "cbs_start": cbs_start,
    }


def get_mwb_publication_refs(
    conn: Any,
    doc_id: int,
    cbs_ref: dict[str, Any] | None = None,
    rows: list[Any] | None = None,
    mid_ord: int | None = None,
    final_song_ord: int | None = None,
    cbs_start: int | None = None,
) -> list[meeting_models.MeetingPublicationRef]:
    if rows is None:
        rows = mwb_publication_ref_rows(conn, doc_id, include_web=True)
    if not rows:
        return []

    if final_song_ord is None:
        final_song_ord = mwb_final_song_ordinal(conn, doc_id)
    cbs_ref = cbs_ref or get_cbs_reference(conn, doc_id, rows, final_song_ord)
    if mid_ord is None:
        mid_ord = mwb_mid_ordinal(conn, doc_id)
    if cbs_start is None:
        cbs_start = int(
            (cbs_ref or {}).get("cbs_start")
            or mwb_cbs_start_from_rows(rows, final_song_ord)
        )

    refs_by_key: dict[
        tuple[str, str, str, int, bool],
        meeting_models.MeetingPublicationRef,
    ] = {}
    order: list[tuple[str, str, str, int, bool]] = []
    for row in rows:
        pub = str(row["undated"] or "").strip()
        meps_id = int(row["meps_id"] or 0)
        if (
            not pub
            or pub.lower() in SKIP_MEETING_REFERENCE_PUBLICATIONS
            or not meps_id
        ):
            continue
        paragraph = int(row["par"] or 0)
        issue = str(row["issue_tag"] or "0")
        section = mwb_section_for_par(paragraph, mid_ord, cbs_start)
        is_cbs = is_cbs_row(row, cbs_ref, final_song_ord)
        key = (section, pub, issue, meps_id, is_cbs)
        caption = clean_reference_text(row["Caption"] or "")
        if key in refs_by_key:
            ref = refs_by_key[key]
            ref.begin_ordinal = min(ref.begin_ordinal, paragraph)
            if len(caption) > len(ref.caption):
                ref.caption = caption
            continue
        refs_by_key[key] = meeting_models.MeetingPublicationRef(
            section=section,
            begin_ordinal=paragraph,
            pub=pub,
            issue=issue,
            publication_title=publication_title_from_row(row, pub),
            caption=caption,
            meps_doc_id=meps_id,
            is_cbs=is_cbs,
        )
        order.append(key)
    return sorted((refs_by_key[key] for key in order), key=lambda ref: ref.begin_ordinal)


def excluded_multimedia_categories_sql() -> str:
    return ",".join(str(category) for category in EXCLUDED_MULTIMEDIA_CATEGORIES)


def query_multimedia(conn: Any, doc_id: int) -> list[Any]:
    excluded = excluded_multimedia_categories_sql()
    for clause in ["AND m.SuppressZoom IS NOT 1", ""]:
        try:
            return conn.execute(
                f"""
                SELECT m.MultimediaId, m.FilePath, m.MimeType, m.CategoryType,
                       m.Label, m.Caption, m.KeySymbol, m.Track,
                       m.IssueTagNumber, m.MepsDocumentId,
                       MIN(dm.BeginParagraphOrdinal) AS par
                FROM   Multimedia m
                JOIN   DocumentMultimedia dm ON dm.MultimediaId=m.MultimediaId
                WHERE  dm.DocumentId=?
                  AND  m.CategoryType NOT IN ({excluded})
                  AND  dm.BeginParagraphOrdinal IS NOT NULL
                  {clause}
                GROUP BY m.MultimediaId
                ORDER  BY par
                """,
                (doc_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
    return []


def media_file_path(raw_path: str, pub_dir: Path) -> str:
    raw_path = str(raw_path or "").strip()
    if not raw_path:
        return ""
    if raw_path.startswith(("http://", "https://")):
        return raw_path

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = pub_dir / raw_path
    if candidate.exists():
        return str(candidate)
    return ""


def make_media_item(
    row: Any,
    pub_dir: Path,
    section: str,
    is_song: bool,
    cbs_title: str = "",
) -> meeting_models.MeetingMedia:
    mime_type = (row["MimeType"] or "").lower()
    raw_path = row["FilePath"] or ""
    file_path = media_file_path(raw_path, pub_dir)
    return meeting_models.MeetingMedia(
        multimedia_id=row["MultimediaId"],
        mime_type=mime_type,
        file_path=file_path,
        label=row["Label"] or "",
        caption=row["Caption"] or "",
        begin_ordinal=row["par"] or 0,
        key_symbol=row["KeySymbol"] or "",
        track=row["Track"] or 0,
        issue_tag=row["IssueTagNumber"] or 0,
        meps_doc_id=row["MepsDocumentId"] or 0,
        section=section,
        is_song=is_song,
        cbs_article_title=cbs_title,
    )


def dedup_multimedia_rows(rows: list[Any]) -> list[Any]:
    seen_multimedia_ids: set[int] = set()
    seen_video_keys: set[tuple[Any, ...]] = set()
    seen_images: set[str] = set()
    result = []
    for row in rows:
        multimedia_id = row["MultimediaId"]
        if multimedia_id in seen_multimedia_ids:
            continue
        seen_multimedia_ids.add(multimedia_id)

        mime_type = (row["MimeType"] or "").lower()
        if mime_type.startswith("image"):
            file_path = row["FilePath"] or ""
            if file_path and file_path in seen_images:
                continue
            if file_path:
                seen_images.add(file_path)
        else:
            symbol = (row["KeySymbol"] or "").strip()
            track = row["Track"]
            issue = row["IssueTagNumber"]
            meps_id = row["MepsDocumentId"]
            if symbol:
                video_key = (symbol.lower(), track, issue)
            elif meps_id:
                video_key = ("__meps__", meps_id, None)
            else:
                video_key = None
            if video_key is not None:
                if video_key in seen_video_keys:
                    continue
                seen_video_keys.add(video_key)

        result.append(row)
    return result


def parse_mwb_media(
    conn: Any,
    doc_id: int,
    pub_dir: Path,
    mid_ord: int | None = None,
    cbs_start: int | None = None,
) -> list[meeting_models.MeetingMedia]:
    if mid_ord is None:
        mid_ord = mwb_mid_ordinal(conn, doc_id)
    if cbs_start is None:
        cbs_start = mwb_cbs_start(conn, doc_id)
    result = []
    for row in dedup_multimedia_rows(query_multimedia(conn, doc_id)):
        paragraph = row["par"] or 0
        symbol = (row["KeySymbol"] or "").lower()
        is_song = symbol in SONG_SYMBOLS
        section = mwb_section_for_par(paragraph, mid_ord, cbs_start)
        result.append(make_media_item(row, pub_dir, section, is_song))
    return result


def parse_watchtower_media(
    conn: Any,
    doc_id: int,
    pub_dir: Path,
) -> list[meeting_models.MeetingMedia]:
    return [
        make_media_item(
            row,
            pub_dir,
            "wt",
            (row["KeySymbol"] or "").lower() in SONG_SYMBOLS,
        )
        for row in dedup_multimedia_rows(query_multimedia(conn, doc_id))
    ]


def parse_publication_ref_items(
    pub_dir: Path,
    db_path: Path,
    meps_doc_id: int,
    section: str,
    marker_title: str,
) -> list[meeting_models.MeetingMedia]:
    result = []
    try:
        conn = open_publication_database(db_path)
        try:
            row = conn.execute(
                "SELECT DocumentId FROM Document WHERE MepsDocumentId=? LIMIT 1",
                (meps_doc_id,),
            ).fetchone()
            if not row:
                return []
            for media_row in dedup_multimedia_rows(query_multimedia(conn, row[0])):
                symbol = (media_row["KeySymbol"] or "").lower()
                result.append(
                    make_media_item(
                        media_row,
                        pub_dir,
                        section,
                        symbol in SONG_SYMBOLS,
                        marker_title,
                    )
                )
        finally:
            conn.close()
    except (IndexError, KeyError, OSError, sqlite3.Error, TypeError, ValueError):
        log.exception("Could not parse publication reference media")
    return result


def read_mwb_week_content(
    pub_dir: Path,
    db_path: Path,
    monday: date,
) -> MwbPublicationContent | None:
    conn = open_publication_database(db_path)
    try:
        if not is_week_in_dated_text(conn, monday):
            return None
        doc_id = find_mwb_document_id(conn, monday)
        if doc_id is None:
            return None

        date_label = document_title(conn, doc_id)
        song_ordinals = mwb_song_ordinals(conn, doc_id)
        mid_ord = mwb_mid_ordinal_from_songs(song_ordinals)
        final_song_ord = mwb_final_song_ordinal_from_songs(song_ordinals)
        publication_rows = mwb_publication_ref_rows(
            conn,
            doc_id,
            include_web=True,
        )
        cbs_ref = get_cbs_reference(
            conn,
            doc_id,
            publication_rows,
            final_song_ord,
        )
        cbs_start = int(
            (cbs_ref or {}).get("cbs_start")
            or mwb_cbs_start_from_rows(publication_rows, final_song_ord)
        )
        return MwbPublicationContent(
            date_label=date_label,
            media_items=parse_mwb_media(conn, doc_id, pub_dir, mid_ord, cbs_start),
            publication_refs=get_mwb_publication_refs(
                conn,
                doc_id,
                cbs_ref,
                publication_rows,
                mid_ord,
                final_song_ord,
                cbs_start,
            ),
            cover_bytes=read_cover_bytes(pub_dir),
            cbs_ref=cbs_ref,
        )
    finally:
        conn.close()


def read_watchtower_study_content(
    pub_dir: Path,
    db_path: Path,
    monday: date,
) -> WatchtowerPublicationContent | None:
    conn = open_publication_database(db_path)
    try:
        doc_id = find_watchtower_document_id(conn, monday)
        if doc_id is None:
            return None
        return WatchtowerPublicationContent(
            title=document_title(conn, doc_id),
            media_items=parse_watchtower_media(conn, doc_id, pub_dir),
            cover_bytes=read_cover_bytes(pub_dir),
        )
    finally:
        conn.close()


def sync_cbs_from_publication_refs(wd: meeting_models.WeekData) -> None:
    refs = [
        ref
        for ref in getattr(wd, "mwb_publication_refs", []) or []
        if getattr(ref, "is_cbs", False)
    ]
    if not refs:
        wd.cbs_items = []
        return

    refs = sorted(refs, key=lambda ref: int(ref.begin_ordinal or 0))
    items: list[meeting_models.MeetingMedia] = []
    meps_doc_ids: list[int] = []
    doc_titles: dict[int, str] = {}
    for ref in refs:
        meps_doc_id = int(ref.meps_doc_id or 0)
        if meps_doc_id and meps_doc_id not in meps_doc_ids:
            meps_doc_ids.append(meps_doc_id)
        if meps_doc_id and ref.caption:
            doc_titles[meps_doc_id] = ref.caption
        items.extend(ref.items or [])

    first = refs[0]
    if wd.cbs_ref:
        existing_ids = [
            int(value)
            for value in wd.cbs_ref.get("meps_doc_ids", [])
            if int(value or 0)
        ]
        existing_titles = {
            int(key): value
            for key, value in (wd.cbs_ref.get("doc_titles") or {}).items()
            if int(key or 0)
        }
        for meps_doc_id in meps_doc_ids:
            if meps_doc_id not in existing_ids:
                existing_ids.append(meps_doc_id)
            if meps_doc_id in doc_titles and meps_doc_id not in existing_titles:
                existing_titles[meps_doc_id] = doc_titles[meps_doc_id]
        wd.cbs_ref = dict(wd.cbs_ref)
        wd.cbs_ref["issue"] = first.issue or wd.cbs_ref.get("issue", "0")
        wd.cbs_ref["meps_doc_ids"] = existing_ids
        wd.cbs_ref["doc_titles"] = existing_titles
        wd.cbs_ref["title"] = (
            wd.cbs_ref.get("title")
            or (existing_titles.get(existing_ids[0], "") if existing_ids else "")
        )
    else:
        wd.cbs_ref = {
            "pub": first.pub,
            "issue": first.issue or "0",
            "publication_title": first.publication_title or first.pub,
            "meps_doc_ids": meps_doc_ids,
            "doc_titles": doc_titles,
            "title": doc_titles.get(meps_doc_ids[0], "") if meps_doc_ids else "",
            "cbs_start": first.begin_ordinal,
        }
    wd.cbs_items = items


__all__ = [
    "MwbPublicationContent",
    "WatchtowerPublicationContent",
    "find_mwb_document_id",
    "get_cbs_reference",
    "get_mwb_publication_refs",
    "make_media_item",
    "open_publication_database",
    "parse_publication_ref_items",
    "read_mwb_week_content",
    "read_watchtower_study_content",
    "sync_cbs_from_publication_refs",
    "watchtower_issue_contains_week",
]
