"""Meeting-targeted source scanning for linked-folder ingestion."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os

from solin.core.foundation.constants import (
    DOCX_EXTS,
    JWPUB_EXTS,
    PDF_EXTS,
    PLAYLIST_EXTS,
    PPTX_EXTS,
)
from solin.core.ingest.staging import is_watched_folder_staging_path
from solin.core.media.formats import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    media_type_from_path,
)
from solin.core.ingest.sync.resources import content_signature
from solin.core.meetings.folder_matcher import match_meeting_folder

DIRECT_MEDIA_SOURCE_EXTS: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS
MEETING_FOLDER_SOURCE_EXTS: frozenset[str] = (
    DIRECT_MEDIA_SOURCE_EXTS
    | PDF_EXTS
    | PPTX_EXTS
    | DOCX_EXTS
    | JWPUB_EXTS
    | PLAYLIST_EXTS
)


def _source_key(path: Path) -> str:
    """Stable per-machine key for a source file in a meeting-targeted folder."""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _file_signature(path: Path) -> dict[str, Any]:
    """Portable content identity, cached using strictly local stat revisions."""
    return content_signature(path)


def _source_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in DIRECT_MEDIA_SOURCE_EXTS:
        return "media"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in JWPUB_EXTS:
        return "jwpub"
    if ext in PLAYLIST_EXTS:
        return "jwlplaylist"
    if ext in (PPTX_EXTS | DOCX_EXTS):
        return "lo"
    return ""


def meeting_folder_source_needs_processing(
    source: dict[str, Any],
    record: dict[str, Any] | None,
) -> bool:
    """Return True when a meeting-folder source should be imported.

    Matching ``processed`` or ``failed`` records suppress retries until the file
    changes. This keeps a removed autoimported item from reappearing while the
    original file remains untouched in the meeting folder.
    """
    if not isinstance(record, dict):
        return True
    status = str(record.get("status") or "")
    if status not in {"processed", "failed"}:
        return True
    return record.get("signature") != source.get("signature")


def scan_meeting_folder_sources(folder_path: str | Path) -> list[dict[str, Any]]:
    """Return direct source files from meeting-targeted subfolders.

    The scanner never reads or writes the linked-folder manifest and never
    inspects ``.solin_cache``. Meeting controllers keep normal linked-folder
    semantics while using profile caches for processed outputs.
    """
    root = Path(folder_path)
    if not root.is_dir():
        return []

    result: list[dict[str, Any]] = []
    for subfolder in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if not subfolder.is_dir() or subfolder.name.startswith("."):
            continue
        match = match_meeting_folder(subfolder.name)
        if not match:
            continue

        sources: list[dict[str, Any]] = []
        for file_path in sorted(subfolder.iterdir(), key=lambda path: path.name.lower()):
            if is_watched_folder_staging_path(file_path):
                continue
            if not file_path.is_file():
                continue
            if file_path.name.startswith(".") or file_path.name.startswith("_solin"):
                continue
            ext = file_path.suffix.lower()
            if ext not in MEETING_FOLDER_SOURCE_EXTS:
                continue
            kind = _source_kind(file_path)
            if not kind:
                continue
            source = {
                "source_key": _source_key(file_path),
                "path": str(file_path),
                "name": file_path.name,
                "title": file_path.stem,
                "ext": ext,
                "kind": kind,
                "signature": _file_signature(file_path),
            }
            if kind == "media":
                source["media_type"] = media_type_from_path(file_path, default="image")
            sources.append(source)

        result.append(
            {
                "path": str(subfolder),
                "name": subfolder.name,
                "monday": match.monday.isoformat(),
                "meeting_tag": match.meeting_tag,
                "sources": sources,
            }
        )
    return result
