"""Playlist media item model and framework-independent factory."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

from ..media.formats import MediaKind, media_kind_from_path, media_type_from_path
from ..media.insertion import MediaInsertPayload
from solin.core.jw.identifiers import is_jw_url
from solin.core.media.jw_reference import parse_jw_media_reference


class PlaylistMediaItem(TypedDict):
    id: str
    title: str
    url: str
    source_url: NotRequired[str]
    type: str
    auto_title: bool
    key_symbol: str | None
    track: int | None
    issue_tag: int | None
    doc_id: int | None
    meps_language: int
    start_trim_ticks: NotRequired[int | None]
    end_trim_ticks: NotRequired[int | None]
    base_duration_ticks: NotRequired[int | None]
    accuracy: NotRequired[int | None]
    end_action: NotRequired[int | None]
    language: NotRequired[str]
    jw_media_id: NotRequired[str]
    jw_identity_authoritative: NotRequired[bool]
    image_framing: NotRequired[dict[str, Any]]
    thumbnail_url: NotRequired[str]
    thumbnail_binding: NotRequired[str]


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
    explicit_jw_identity = bool(
        attributes.get("jw_identity_authoritative")
        or attributes.get("jw_media_id")
        or attributes.get("key_symbol")
        or attributes.get("doc_id")
    )
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
    if explicit_jw_identity:
        item["jw_identity_authoritative"] = True

    if not item.get("key_symbol") and url:
        parsed = parse_jw_media_reference(
            url,
            original_filename=str(item.get("original_filename") or ""),
        )
        if parsed and (explicit_jw_identity or is_jw_url(url)):
            item.update(parsed)
            item["jw_identity_authoritative"] = True
            if parsed["key_symbol"] or parsed["doc_id"]:
                item["auto_title"] = True

    return cast(PlaylistMediaItem, item)


def copy_playlist_item_for_destination(
    item: Mapping[str, Any],
) -> PlaylistMediaItem:
    """Copy media into another collection without leaking source-tree identity."""

    attributes = copy.deepcopy(dict(item))
    title = str(attributes.pop("title", "") or "")
    url = str(attributes.pop("url", "") or "")
    attributes.pop("id", None)
    attributes.pop("section_id", None)
    attributes.pop("origin_kind", None)
    attributes.pop("origin_container_id", None)
    attributes.pop("origin_item_id", None)
    return create_playlist_item(title, url, **attributes)


def distinct_playlist_item_ids(
    existing_items: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Copy insertions and assign a fresh node ID only when one collides."""

    occupied = {
        str(item.get("id") or "")
        for item in existing_items
        if str(item.get("id") or "")
    }
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        item = copy.deepcopy(dict(candidate))
        identifier = str(item.get("id") or "")
        if not identifier or identifier in occupied:
            identifier = str(uuid.uuid4())
            while identifier in occupied:
                identifier = str(uuid.uuid4())
            item["id"] = identifier
        occupied.add(identifier)
        result.append(item)
    return result


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


def create_playlist_item_from_insert(
    payload: MediaInsertPayload,
    *,
    item_id: str | None = None,
) -> PlaylistMediaItem:
    """Project one normalized picker selection into the persisted playlist shape."""

    attributes: dict[str, Any] = {
        "type": payload.media_type,
        "auto_title": True,
        "key_symbol": payload.key_symbol or None,
        "track": payload.track or None,
        "issue_tag": payload.issue_tag or None,
        "doc_id": payload.doc_id or None,
        "meps_language": payload.meps_language,
        "language": payload.language,
        "jw_media_id": payload.jw_media_id,
        "jw_identity_authoritative": payload.jw_identity_authoritative,
    }
    if item_id:
        attributes["id"] = item_id
    if payload.base_duration_ticks > 0:
        attributes["base_duration_ticks"] = payload.base_duration_ticks
    if payload.thumbnail_url:
        attributes["thumbnail_url"] = payload.thumbnail_url
        attributes["thumbnail_binding"] = "jw_artwork"
    return create_playlist_item(
        payload.title,
        payload.source_url,
        **attributes,
    )
