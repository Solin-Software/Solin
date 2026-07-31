"""Normalized JW media metadata adapter used by import and export workflows."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from .identifiers import meps_to_lang
from .publication_links import (
    PublicationMediaRequest,
    PublicationMediaResolver,
)


class ResolvedMediaMetadata(TypedDict):
    url: str
    title: str | None
    thumbnail_url: NotRequired[str]
    duration_ticks: int | None


_RESOLVER = PublicationMediaResolver()


def resolve_jworg_meta(
    key_symbol: str | None,
    doc_id: int | None,
    track: int | None,
    issue_tag: int | None,
    meps_language: int,
    fallback_lang: str = "E",
    major_multimedia_type: int | None = None,
) -> ResolvedMediaMetadata | None:
    """Resolve one JW reference without caching transient failures globally."""

    if not key_symbol and not doc_id:
        return None
    media_file = _RESOLVER.resolve_media(
        PublicationMediaRequest(
            key_symbol=str(key_symbol or ""),
            track=track,
            issue_tag=issue_tag,
            meps_doc_id=doc_id,
            language=meps_to_lang(meps_language, fallback_lang),
            media_type="audio" if major_multimedia_type == 0 else "video",
        )
    )
    if media_file is None:
        return None
    return {
        "url": media_file.url,
        "title": media_file.title or None,
        "thumbnail_url": media_file.thumbnail_url,
        "duration_ticks": media_file.duration_ticks or None,
    }


__all__ = ["ResolvedMediaMetadata", "resolve_jworg_meta"]
