"""Framework-independent meeting data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


def media_type_for_mime_type(mime_type: str) -> str:
    normalized = str(mime_type or "").strip().casefold()
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("image/"):
        return "image"
    return "video"


@dataclass(slots=True)
class MeetingMedia:
    multimedia_id: int = 0
    mime_type: str = ""
    file_path: str = ""
    label: str = ""
    caption: str = ""
    begin_ordinal: int = 0
    key_symbol: str = ""
    track: int = 0
    issue_tag: int = 0
    meps_doc_id: int = 0
    section: str = ""
    is_song: bool = False
    cbs_article_title: str = ""
    image_framing: dict[str, Any] | None = None
    start_trim_ticks: int = 0
    end_trim_ticks: int = 0
    base_duration_ticks: int = 0
    origin_kind: str = ""
    origin_container_id: str = ""
    origin_item_id: str = ""

    @property
    def media_type(self) -> str:
        return media_type_for_mime_type(self.mime_type)


@dataclass(slots=True)
class MeetingPublicationRef:
    section: str = ""
    begin_ordinal: int = 0
    pub: str = ""
    issue: str = "0"
    publication_title: str = ""
    caption: str = ""
    meps_doc_id: int = 0
    is_cbs: bool = False
    items: list[MeetingMedia] = field(default_factory=list)


@dataclass(slots=True)
class WeekData:
    monday: date = field(default_factory=date.today)
    language_code: str = ""
    is_sign_language: bool = False
    request_generation: int = 0
    mwb_pub_dir: Path | None = None
    mwb_cover_bytes: bytes | None = None
    mwb_date_label: str = ""
    mwb_week_title: str = ""
    mwb_all_media: list[MeetingMedia] = field(default_factory=list)
    mwb_publication_refs: list[MeetingPublicationRef] = field(default_factory=list)
    mwb_status: str = "idle"
    mwb_issue: str = ""
    mwb_source_checksum: str = ""
    wt_pub_dir: Path | None = None
    wt_cover_bytes: bytes | None = None
    wt_study_title: str = ""
    wt_issue: str = ""
    wt_source_checksum: str = ""
    wt_all_media: list[MeetingMedia] = field(default_factory=list)
    wt_status: str = "idle"
    cbs_ref: dict[str, Any] | None = None
    cbs_pub_dir: Path | None = None
    cbs_items: list[MeetingMedia] = field(default_factory=list)
    cbs_status: str = "idle"


@dataclass(slots=True)
class MemorialData:
    year: int = 0
    memorial_date: date | None = None
    memorial_week: date | None = None
    cover_bytes: bytes | None = None
    videos: list[MeetingMedia] = field(default_factory=list)
    thumb_url: str = ""
    status: str = "idle"
    pub_dir: Path | None = None
