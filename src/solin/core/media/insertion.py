"""Shared result contract for media insertions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from solin.core.jw.identifiers import MEPS_FROM_LANG

from .duration import normalize_duration_ticks


_MEDIA_TYPES = frozenset({"audio", "image", "video"})
_JW_SOURCES = frozenset({"jworg", "pub-media"})


def _int_or_zero(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if type(value) is int:
        return value
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


class MediaInsertPayload(NamedTuple):
    """Normalized media selected by a picker before destination projection."""

    title: str
    source_url: str
    media_type: str
    base_duration_ticks: int = 0
    thumbnail_url: str = ""
    thumbnail_path: str = ""
    key_symbol: str = ""
    track: int = 0
    issue_tag: int = 0
    doc_id: int = 0
    meps_language: int = 0
    language: str = ""
    jw_media_id: str = ""
    jw_identity_authoritative: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MediaInsertPayload":
        get = value.get
        language = str(get("language") or "").upper()
        meps_language = _int_or_zero(get("meps_language"))
        if not meps_language and language:
            meps_language = MEPS_FROM_LANG.get(language, 0)
        media_type = str(get("media_type") or get("type") or "video").lower()
        if media_type not in _MEDIA_TYPES:
            media_type = "video"
        key_symbol = str(get("pub") or get("key_symbol") or "")
        doc_id = _int_or_zero(get("docid") or get("doc_id"))
        jw_media_id = str(get("jw_media_id") or "")
        jw_identity_authoritative = bool(
            get("jw_identity_authoritative") or key_symbol or doc_id or jw_media_id
        )
        if not jw_identity_authoritative:
            source = str(get("source") or "").lower()
            jw_identity_authoritative = source in _JW_SOURCES or source.startswith("category:")
        duration_seconds = get("duration_seconds")
        return cls(
            str(get("title") or get("label") or "").strip(),
            str(get("download_url") or get("url") or get("jworg_url") or ""),
            media_type,
            normalize_duration_ticks(
                ticks=(get("base_duration_ticks") or get("duration_ticks")),
                seconds=(duration_seconds if duration_seconds is not None else get("duration")),
            ),
            str(get("thumbnail_url") or ""),
            str(get("thumbnail_path") or ""),
            key_symbol,
            _int_or_zero(get("track")),
            _int_or_zero(get("issue") or get("issue_tag")),
            doc_id,
            meps_language,
            language,
            jw_media_id,
            jw_identity_authoritative,
        )

    def to_identity_mapping(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.source_url,
            "download_url": self.source_url,
            "type": self.media_type,
            "media_type": self.media_type,
            "pub": self.key_symbol,
            "key_symbol": self.key_symbol,
            "track": self.track,
            "issue": self.issue_tag,
            "issue_tag": self.issue_tag,
            "docid": self.doc_id,
            "doc_id": self.doc_id,
            "meps_language": self.meps_language,
            "language": self.language,
            "jw_media_id": self.jw_media_id,
            "jw_identity_authoritative": self.jw_identity_authoritative,
        }


@dataclass(frozen=True, slots=True)
class MediaInsertResult:
    added_items: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    duplicate_items: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    target_valid: bool = True

    @property
    def added_count(self) -> int:
        return len(self.added_items)

    @property
    def duplicate_count(self) -> int:
        return len(self.duplicate_items)


__all__ = ["MediaInsertPayload", "MediaInsertResult"]
