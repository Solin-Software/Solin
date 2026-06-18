"""
jwpub_import.py - Solin
====================================
Application service for reading .jwpub files into playlist-compatible items.

This module is intentionally free of Qt. Callers that need signals or threads
should use an adapter around ``JwpubPlaylistImportService``.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from solin.core.jw.publication_links import (
    PublicationMediaRequest,
    PublicationMediaResolver,
    PubMediaFile,
)

log = logging.getLogger(__name__)

_EXCLUDED_CATEGORY_TYPES = frozenset({9, 10, 15, 25})
_PUBLICATION_MEDIA_RESOLVER = PublicationMediaResolver()

PublicationMediaResolveFn: TypeAlias = Callable[
    [str, int | str | None, int | str | None, int | str | None, str],
    PubMediaFile | None,
]


@dataclass(frozen=True, slots=True)
class JwpubImportRequest:
    jwpub_path: str
    language: str = "T"
    dest_images_dir: str | None = None
    resolve_urls: bool = True


class JwpubPlaylistImportService:
    """Read a .jwpub archive and produce playlist-compatible item dictionaries."""

    def __init__(self, resolve_media: PublicationMediaResolveFn | None = None) -> None:
        self._resolve_media = resolve_media or _resolve_publication_media

    def read(self, request: JwpubImportRequest) -> tuple[list[dict], str]:
        return read_jwpub_playlist_items(request, resolve_media=self._resolve_media)


def read_jwpub_playlist_items(
    request: JwpubImportRequest,
    *,
    resolve_media: PublicationMediaResolveFn | None = None,
) -> tuple[list[dict], str]:
    """Parse a .jwpub file and return ``(items, stem)`` for playlist workflows."""

    resolver = resolve_media or _resolve_publication_media
    jwpub_path = request.jwpub_path
    stem = Path(jwpub_path).stem
    temporary_dir: tempfile.TemporaryDirectory | None = None

    try:
        with zipfile.ZipFile(jwpub_path, "r") as outer:
            if "contents" not in outer.namelist():
                raise ValueError("Not a valid .jwpub (no 'contents' entry)")
            inner_bytes = outer.read("contents")
        with zipfile.ZipFile(io.BytesIO(inner_bytes), "r") as inner:
            db_names = [name for name in inner.namelist() if name.endswith(".db")]
            if not db_names:
                raise ValueError("No SQLite database found inside .jwpub")
            if request.dest_images_dir:
                temporary_dir = tempfile.TemporaryDirectory(
                    prefix="solin_jwpub_",
                    ignore_cleanup_errors=True,
                )
                tmp_dir = temporary_dir.name
            else:
                tmp_dir = tempfile.mkdtemp(prefix="solin_jwpub_")
            inner.extractall(tmp_dir)
    except (
        EOFError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        if temporary_dir is not None:
            temporary_dir.cleanup()
        log.error("jwpub_import: cannot extract %s: %s", jwpub_path, exc)
        raise

    try:
        rows = _read_multimedia_rows(Path(tmp_dir) / os.path.basename(db_names[0]))
        img_dest = _image_destination(request.dest_images_dir, tmp_dir)
        image_items, video_rows = _build_raw_items(rows, tmp_dir, img_dest)
        video_items = _build_video_items(
            video_rows,
            stem,
            request.language,
            request.resolve_urls,
            resolver,
        )
        return image_items + video_items, stem
    finally:
        if temporary_dir is not None:
            temporary_dir.cleanup()


def _resolve_publication_media(
    key_symbol: str,
    track: int | str | None,
    issue_tag: int | str | None,
    meps_doc_id: int | str | None,
    language: str,
) -> PubMediaFile | None:
    return _PUBLICATION_MEDIA_RESOLVER.resolve_video(
        PublicationMediaRequest(
            key_symbol=key_symbol,
            track=_int_or_zero(track),
            issue_tag=_int_or_zero(issue_tag),
            meps_doc_id=_int_or_zero(meps_doc_id),
            language=language,
        )
    )


def _int_or_zero(value: int | str | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _read_multimedia_rows(db_path: Path) -> list[sqlite3.Row]:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        placeholders = ", ".join("?" for _ in _EXCLUDED_CATEGORY_TYPES)
        return conn.execute(
            f"""
            SELECT DISTINCT
                   m.MultimediaId,
                   m.FilePath,
                   m.MimeType,
                   m.CategoryType,
                   m.Label,
                   m.Caption,
                   m.KeySymbol,
                   m.Track,
                   m.IssueTagNumber,
                   m.MepsDocumentId
            FROM   Multimedia m
            WHERE  m.CategoryType NOT IN ({placeholders})
              AND  (
                     (m.FilePath IS NOT NULL AND m.FilePath != '')
                     OR m.KeySymbol IS NOT NULL
                     OR m.MepsDocumentId IS NOT NULL
                   )
            ORDER  BY m.MultimediaId
            """,
            tuple(_EXCLUDED_CATEGORY_TYPES),
        ).fetchall()
    except sqlite3.Error as exc:
        log.error("jwpub_import: cannot read db %s: %s", db_path, exc)
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                log.debug("jwpub_import: could not close db %s", db_path, exc_info=True)


def _image_destination(dest_images_dir: str | None, tmp_dir: str) -> str:
    if not dest_images_dir:
        return tmp_dir
    os.makedirs(dest_images_dir, exist_ok=True)
    return dest_images_dir


def _build_raw_items(
    rows: list[sqlite3.Row],
    tmp_dir: str,
    img_dest: str,
) -> tuple[list[dict], list[dict]]:
    image_items: list[dict] = []
    video_rows: list[dict] = []
    seen_ids: set[int] = set()

    for row in rows:
        multimedia_id = row["MultimediaId"]
        if multimedia_id in seen_ids:
            continue
        seen_ids.add(multimedia_id)

        mime = (row["MimeType"] or "").lower()
        file_path = row["FilePath"] or ""
        label = (row["Label"] or row["Caption"] or "").strip()

        if mime.startswith("image") and file_path:
            image_item = _image_item(tmp_dir, img_dest, file_path, label)
            if image_item is not None:
                image_items.append(image_item)
        elif mime.startswith("video") or mime.startswith("audio"):
            video_row = _video_row(row, mime, label)
            if video_row is not None:
                video_rows.append(video_row)

    return image_items, video_rows


def _image_item(tmp_dir: str, img_dest: str, file_path: str, label: str) -> dict | None:
    source = os.path.join(tmp_dir, file_path)
    if not os.path.exists(source):
        return None

    if img_dest != tmp_dir:
        ext = Path(file_path).suffix or ".jpg"
        destination = os.path.join(img_dest, f"{uuid.uuid4().hex}{ext}")
        shutil.copy2(source, destination)
    else:
        destination = source

    return {
        "title": label or Path(file_path).stem,
        "url": destination,
        "type": "image",
        "key_symbol": None,
        "track": None,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 0,
    }


def _video_row(row: sqlite3.Row, mime: str, label: str) -> dict | None:
    key_symbol = row["KeySymbol"] or ""
    meps_doc_id = row["MepsDocumentId"] or 0
    if not key_symbol and not meps_doc_id:
        return None

    issue = row["IssueTagNumber"]
    return {
        "sym": key_symbol,
        "track": row["Track"] if row["Track"] is not None else 0,
        "issue": 0 if issue is None else int(issue),
        "meps": meps_doc_id,
        "mtype": "audio" if mime.startswith("audio") else "video",
        "label": label,
    }


def _build_video_items(
    video_rows: list[dict],
    stem: str,
    language: str,
    resolve_urls: bool,
    resolver: PublicationMediaResolveFn,
) -> list[dict]:
    items: list[dict] = []
    for row in video_rows:
        url = ""
        title = row["label"] or row["sym"] or stem
        if resolve_urls:
            try:
                media_file = resolver(
                    row["sym"],
                    row["track"],
                    row["issue"],
                    row["meps"],
                    language,
                )
                if media_file is not None:
                    url = media_file.url
                    title = media_file.title or title
            except Exception:  # noqa: BLE001 - per-item external resolver isolation
                log.debug("jwpub_import: resolve failed sym=%s", row["sym"], exc_info=True)

        items.append({
            "title": title,
            "url": url,
            "type": row["mtype"],
            "key_symbol": row["sym"] or None,
            "track": row["track"] if row["track"] else None,
            "issue_tag": row["issue"] if row["issue"] != 0 else None,
            "doc_id": row["meps"] if row["meps"] else None,
            "meps_language": 0,
        })
    return items


__all__ = [
    "JwpubImportRequest",
    "JwpubPlaylistImportService",
    "PublicationMediaResolveFn",
    "read_jwpub_playlist_items",
]
