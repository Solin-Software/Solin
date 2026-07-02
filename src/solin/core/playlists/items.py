"""Playlist media item model and framework-independent factory."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

from ..media.formats import MediaKind, media_kind_from_path, media_type_from_path
from solin.core.media.jw_reference import parse_jw_media_reference


class PlaylistMediaItem(TypedDict):
    id: str
    title: str
    url: str
    type: str
    auto_title: bool
    key_symbol: str | None
    track: int | None
    issue_tag: int | None
    doc_id: int | None
    meps_language: int
    language: NotRequired[str]
    jw_media_id: NotRequired[str]
    image_framing: NotRequired[dict[str, Any]]


def looks_like_filename_title(title: str) -> bool:
    """Return whether a title is just a raw audio/video filename."""
    if not title:
        return True
    return media_kind_from_path(title) in {MediaKind.AUDIO, MediaKind.VIDEO}


def create_playlist_item(
    title: str,
    url: str,
    **attributes: Any,
) -> PlaylistMediaItem:
    """Create the stable persisted representation of a playlist media item."""
    media_type = attributes.pop("type", media_type_from_path(url, default="video"))
    url_stem = Path(url.split("?", 1)[0]).stem if url else ""
    auto_title = looks_like_filename_title(title) or (bool(url_stem) and title == url_stem)
    item: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "title": title,
        "url": url,
        "type": media_type,
        "auto_title": auto_title,
        "key_symbol": None,
        "track": None,
        "issue_tag": None,
        "doc_id": None,
        "meps_language": 0,
    }
    item.update(attributes)

    if not item.get("key_symbol") and url:
        parsed = parse_jw_media_reference(
            url,
            original_filename=str(item.get("original_filename") or ""),
        )
        if parsed:
            item.update(parsed)
            if parsed["key_symbol"] or parsed["doc_id"]:
                item["auto_title"] = True

    return cast(PlaylistMediaItem, item)


def playlist_items_from_jwpub(
    raw_items: list[dict[str, Any]],
    fallback_title: str,
) -> list[PlaylistMediaItem]:
    """Map JWPUB import output to the canonical playlist item representation."""
    items: list[PlaylistMediaItem] = []
    for raw in raw_items:
        item = create_playlist_item(
            title=raw.get("title", fallback_title),
            url=raw.get("url", ""),
            type=raw.get("type", "video"),
            key_symbol=raw.get("key_symbol"),
            track=raw.get("track"),
            issue_tag=raw.get("issue_tag"),
            doc_id=raw.get("doc_id"),
            meps_language=raw.get("meps_language", raw.get("language", 0)),
        )
        if raw.get("url") and raw.get("type") != "image":
            item["auto_title"] = False
        items.append(item)
    return items
