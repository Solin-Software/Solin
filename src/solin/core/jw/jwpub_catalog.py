"""Immutable publication catalogs and schema-adaptive, local database inspection."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from solin.core.jw.identifiers import lang_to_meps, meps_to_lang

_EXCLUDED_CATEGORIES = frozenset({9, 10, 15, 25})
MediaType = Literal["image", "video", "audio"]


@dataclass(frozen=True, slots=True)
class JwpubMedia:
    id: str
    title: str
    media_type: MediaType
    preview_member_path: str = ""
    member_path: str = field(default="", repr=False)
    key_symbol: str = field(default="", repr=False)
    track: int = field(default=0, repr=False)
    issue_tag: int = field(default=0, repr=False)
    doc_id: int = field(default=0, repr=False)
    meps_language: int = field(default=0, repr=False)
    language: str = field(default="E", repr=False)


@dataclass(frozen=True, slots=True)
class JwpubDocument:
    id: str
    title: str
    media: tuple[JwpubMedia, ...]


@dataclass(frozen=True, slots=True)
class JwpubCatalog:
    title: str
    symbol: str
    source_path: str
    documents: tuple[JwpubDocument, ...]


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default  # type: ignore[arg-type]
    except (ValueError, TypeError, OverflowError):
        return default


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _rows(
    connection: sqlite3.Connection,
    table: str,
    desired: tuple[str, ...],
    order: str,
) -> list[dict]:
    columns = _columns(connection, table)
    if not columns:
        return []
    fields = ", ".join(f'"{name}"' for name in desired if name in columns)
    if not fields:
        return []
    ordering = f' ORDER BY "{order}"' if order in columns else ""
    return [dict(row) for row in connection.execute(f'SELECT {fields} FROM "{table}"{ordering}')]


def read_catalog(
    database: Path,
    *,
    manifest: Mapping,
    source_path: str,
    language: str,
    members: set[str],
    check_cancelled: Callable[[], None],
) -> JwpubCatalog:
    """Read only catalog columns, without loading document bodies or media bytes."""
    with closing(
        sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.set_progress_handler(lambda: (check_cancelled(), 0)[1], 1000)
        try:
            return _read_catalog(
                connection, manifest, source_path, language, members, check_cancelled
            )
        except sqlite3.Error:
            check_cancelled()
            raise
        finally:
            connection.set_progress_handler(None, 0)


def _read_catalog(
    connection: sqlite3.Connection,
    manifest: Mapping,
    source_path: str,
    language: str,
    members: set[str],
    check_cancelled: Callable[[], None],
) -> JwpubCatalog:
    if "MultimediaId" not in _columns(connection, "Multimedia"):
        raise ValueError("Publication has no multimedia catalog")
    publication_rows = _rows(
        connection, "Publication", ("Title", "Symbol", "MepsLanguageIndex"), "PublicationId"
    )
    publication = publication_rows[0] if publication_rows else {}
    title = (
        _text(publication.get("Title")) or _text(manifest.get("title")) or Path(source_path).stem
    )
    symbol = _text(publication.get("Symbol")) or _text(manifest.get("symbol"))
    pub_meps = publication.get("MepsLanguageIndex", manifest.get("language"))
    pub_meps = _integer(pub_meps, lang_to_meps(language))
    pub_language = meps_to_lang(pub_meps, language)
    documents = _rows(connection, "Document", ("DocumentId", "Title", "TocTitle"), "DocumentId")
    rows = _rows(
        connection,
        "Multimedia",
        (
            "MultimediaId",
            "DocumentId",
            "LinkMultimediaId",
            "DataType",
            "MajorType",
            "MinorType",
            "Width",
            "Height",
            "MimeType",
            "CategoryType",
            "Label",
            "LabelRich",
            "Caption",
            "CaptionRich",
            "CaptionContent",
            "CreditLine",
            "CreditLineRich",
            "CreditLineContent",
            "FilePath",
            "KeySymbol",
            "Track",
            "IssueTagNumber",
            "MepsDocumentId",
            "MepsLanguageIndex",
            "SuppressZoom",
            "SizeConstraint",
            "Specialty",
            "Edition",
        ),
        "MultimediaId",
    )
    by_id = {str(row["MultimediaId"]): row for row in rows}
    previews: dict[str, str] = {}
    preview_ids: set[str] = set()
    for row in rows:
        check_cancelled()
        linked = by_id.get(str(row.get("LinkMultimediaId")))
        if linked is None:
            continue
        image, owner = (row, linked) if _type(row) == "image" else (linked, row)
        if _type(image) == "image" and _type(owner) in {"video", "audio"}:
            preview_ids.add(str(image["MultimediaId"]))
            path = _text(image.get("FilePath"))
            if path in members:
                previews.setdefault(str(owner["MultimediaId"]), path)

    media: dict[str, JwpubMedia] = {}
    for row in rows:
        check_cancelled()
        media_id = str(row["MultimediaId"])
        kind = _type(row)
        if not kind or media_id in preview_ids or row.get("CategoryType") in _EXCLUDED_CATEGORIES:
            continue
        path = _text(row.get("FilePath"))
        path = path if path in members else ""
        key = _text(row.get("KeySymbol"))
        doc_id = _integer(row.get("MepsDocumentId"))
        if not path and (kind == "image" or not (key or doc_id)):
            continue
        meps = _integer(row.get("MepsLanguageIndex"), pub_meps)
        media[media_id] = JwpubMedia(
            id=media_id,
            title=_text(row.get("Label"))
            or _text(row.get("Caption"))
            or (Path(path).stem if path else key)
            or title,
            media_type=kind,
            preview_member_path=path if kind == "image" else previews.get(media_id, ""),
            member_path=path,
            key_symbol=key,
            track=_integer(row.get("Track")),
            issue_tag=_integer(row.get("IssueTagNumber")),
            doc_id=doc_id,
            meps_language=meps,
            language=meps_to_lang(meps, pub_language),
        )

    grouped: dict[str, dict[str, JwpubMedia]] = defaultdict(dict)
    relations = _rows(
        connection,
        "DocumentMultimedia",
        (
            "DocumentMultimediaId",
            "DocumentId",
            "MultimediaId",
            "BeginParagraphOrdinal",
            "EndParagraphOrdinal",
        ),
        "DocumentMultimediaId",
    )
    aliases = _alias_relation_indices(relations, by_id, media, check_cancelled)
    related: dict[str, set[str]] = defaultdict(set)
    for index, relation in enumerate(relations):
        check_cancelled()
        media_id = str(relation.get("MultimediaId"))
        if media_id in media:
            doc_id = str(relation.get("DocumentId"))
            related[doc_id].add(media_id)
            if index not in aliases:
                grouped[doc_id].setdefault(media_id, media[media_id])
    for row in rows:
        check_cancelled()
        media_id = str(row["MultimediaId"])
        if media_id in media and row.get("DocumentId") is not None:
            doc_id = str(row["DocumentId"])
            if media_id not in related[doc_id]:
                grouped[doc_id].setdefault(media_id, media[media_id])
                related[doc_id].add(media_id)

    result: list[JwpubDocument] = []
    # Missing document metadata must not restore aliases in the generic group.
    # An alias with a surviving occurrence elsewhere still belongs in that group.
    assigned = {str(relations[index]["MultimediaId"]) for index in aliases}
    assigned.difference_update(media_id for group in grouped.values() for media_id in group)
    for document in documents:
        check_cancelled()
        doc_id = str(document.get("DocumentId"))
        group = tuple(grouped.get(doc_id, {}).values())
        if group:
            result.append(
                JwpubDocument(
                    doc_id,
                    _text(document.get("Title")) or _text(document.get("TocTitle")) or title,
                    group,
                )
            )
            assigned.update(related[doc_id])
    ungrouped = tuple(item for item in media.values() if item.id not in assigned)
    if ungrouped:
        result.append(JwpubDocument("ungrouped", title, ungrouped))
    return JwpubCatalog(title, symbol, source_path, tuple(result))


def _alias_relation_indices(
    relations: list[dict],
    rows: Mapping[str, dict],
    media: Mapping[str, JwpubMedia],
    check_cancelled: Callable[[], None],
) -> set[int]:
    """Find equivalent local image aliases at an exact document paragraph range.

    Missing dimensions can complement known dimensions. Conflicting explicit
    dimensions make the whole candidate group ambiguous, so none are merged.
    Relations at another position remain independent even for an alias media ID.
    """
    candidates: dict[tuple, list[int]] = defaultdict(list)
    for index, relation in enumerate(relations):
        check_cancelled()
        media_id = str(relation.get("MultimediaId"))
        item = media.get(media_id)
        begin, end = relation.get("BeginParagraphOrdinal"), relation.get("EndParagraphOrdinal")
        if (
            item is None
            or item.media_type != "image"
            or not item.member_path
            or not isinstance(begin, int)
            or not isinstance(end, int)
            or not 0 <= begin <= end
        ):
            continue
        row = rows[media_id]
        metadata = tuple(
            (name, value)
            for name, value in row.items()
            if name not in {"MultimediaId", "DocumentId", "Width", "Height"}
        )
        candidates[(str(relation.get("DocumentId")), begin, end, metadata)].append(index)

    aliases: set[int] = set()
    for indices in candidates.values():
        check_cancelled()
        if len(indices) < 2:
            continue
        candidate_rows = [rows[str(relations[index]["MultimediaId"])] for index in indices]
        if any(
            len({row[axis] for row in candidate_rows if row.get(axis) is not None}) > 1
            for axis in ("Width", "Height")
        ):
            continue
        first_id = str(relations[indices[0]]["MultimediaId"])
        aliases.update(
            index for index in indices if str(relations[index]["MultimediaId"]) != first_id
        )
    return aliases


def _type(row: Mapping) -> MediaType | None:
    mime = _text(row.get("MimeType")).lower()
    for kind in ("image", "video", "audio"):
        if mime.startswith(f"{kind}/"):
            return kind
    return None
