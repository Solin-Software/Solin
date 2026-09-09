"""Shared contracts and eligibility rules for custom idle-screen media."""

from __future__ import annotations

import os
from dataclasses import dataclass

_SUPPORTED_MEDIA_TYPES = frozenset({"image", "video"})
_REMOTE_SOURCE_PREFIXES = ("http://", "https://")


@dataclass(frozen=True, slots=True)
class IdleMediaRequest:
    """One local media source proposed as the custom idle screen."""

    title: str
    path: str
    media_type: str
    thumbnail_path: str = ""


def supports_idle_media_source(media_type: str, source: str) -> bool:
    """Return whether a source has the same shape accepted by the preview action."""

    normalized_type = str(media_type or "").strip().lower()
    normalized_source = str(source or "").strip()
    return bool(
        normalized_type in _SUPPORTED_MEDIA_TYPES
        and normalized_source
        and not normalized_source.lower().startswith(_REMOTE_SOURCE_PREFIXES)
    )


def existing_idle_media_path(media_type: str, source: str) -> str:
    """Return an eligible local file path, or an empty string when unavailable."""

    normalized_source = str(source or "").strip()
    if not supports_idle_media_source(media_type, normalized_source):
        return ""
    return normalized_source if os.path.isfile(normalized_source) else ""


def create_idle_media_request(
    *,
    title: str,
    media_type: str,
    source: str,
    thumbnail_path: str = "",
) -> IdleMediaRequest | None:
    """Create a request only while the local source still exists."""

    path = existing_idle_media_path(media_type, source)
    if not path:
        return None
    normalized_type = str(media_type or "").strip().lower()
    normalized_thumbnail_path = str(thumbnail_path or "").strip()
    if not normalized_thumbnail_path or not os.path.isfile(normalized_thumbnail_path):
        normalized_thumbnail_path = path if normalized_type == "image" else ""
    return IdleMediaRequest(
        title=str(title or ""),
        path=path,
        media_type=normalized_type,
        thumbnail_path=normalized_thumbnail_path,
    )


__all__ = [
    "IdleMediaRequest",
    "create_idle_media_request",
    "existing_idle_media_path",
    "supports_idle_media_source",
]
