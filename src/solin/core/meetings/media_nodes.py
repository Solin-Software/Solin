"""Factories and helpers for meeting media tree nodes."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..media.formats import media_type_from_path
from ..playlists.items import looks_like_filename_title
from .models import MeetingMedia
from .tree_types import Node, new_node_id


def clean_media_title(value: str) -> str:
    return (value or "").strip()


def meeting_media_type_from_path(path: str) -> str:
    return media_type_from_path(path, default="video")


def mime_for_meeting_media(path: str, media_type: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    if media_type == "image":
        return "image/*"
    if media_type == "audio":
        return "audio/*"
    return "video/*"


def media_ref_title(ref: dict[str, Any]) -> str:
    return clean_media_title(str(ref.get("label") or ref.get("caption") or ""))


def should_accept_resolved_media_title(
    node: Node,
    *,
    placeholder_titles: tuple[str, ...] = (),
) -> bool:
    if node.get("user_title_override"):
        return False
    title = clean_media_title(str(node.get("title") or ""))
    ref = node.get("media_ref") or {}
    placeholders = {"", "Media", *placeholder_titles}
    return (
        bool(node.get("auto_title"))
        or (isinstance(ref, dict) and not media_ref_title(ref) and title in placeholders)
        or looks_like_filename_title(title)
    )


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def meeting_media_from_ref(
    ref: dict[str, Any],
    *,
    image_framing: dict[str, Any] | None = None,
    start_trim_ticks: int = 0,
    end_trim_ticks: int = 0,
    base_duration_ticks: int = 0,
) -> MeetingMedia:
    return MeetingMedia(
        multimedia_id=int_or_zero(ref.get("multimedia_id")),
        mime_type=str(ref.get("mime_type") or ""),
        file_path=str(ref.get("file_path") or ""),
        label=str(ref.get("label") or ""),
        caption=str(ref.get("caption") or ""),
        begin_ordinal=int_or_zero(ref.get("begin_ordinal")),
        key_symbol=str(ref.get("key_symbol") or ""),
        track=int_or_zero(ref.get("track")),
        issue_tag=int_or_zero(ref.get("issue_tag")),
        meps_doc_id=int_or_zero(ref.get("meps_doc_id")),
        section=str(ref.get("section") or ""),
        is_song=bool(ref.get("is_song", False)),
        cbs_article_title=str(ref.get("cbs_article_title") or ""),
        image_framing=image_framing,
        start_trim_ticks=start_trim_ticks,
        end_trim_ticks=end_trim_ticks,
        base_duration_ticks=base_duration_ticks,
    )


def playlist_item_media_url(raw: dict[str, Any]) -> str:
    return str(raw.get("url") or raw.get("jworg_url") or "")


def create_manual_media_node(
    path: str,
    title: str = "",
    *,
    node_id_factory: Callable[[], str] = new_node_id,
) -> Node:
    media_type = meeting_media_type_from_path(path)
    display_title = clean_media_title(title) or Path(path).stem
    node_id = node_id_factory()
    ref = {
        "multimedia_id": 0,
        "mime_type": mime_for_meeting_media(path, media_type),
        "file_path": path,
        "label": display_title,
        "caption": "",
        "begin_ordinal": 0,
        "key_symbol": "",
        "track": 0,
        "issue_tag": 0,
        "meps_doc_id": 0,
        "section": "",
        "is_song": False,
        "cbs_article_title": "",
    }
    node: Node = {
        "id": node_id,
        "type": "media",
        "title": display_title,
        "media_type": media_type,
        "media_ref": ref,
        "children": [],
        "meeting_generated": False,
    }
    return node


def create_playlist_media_node(
    raw: dict[str, Any],
    fallback_title: str,
    *,
    node_id: str,
    url: str,
    media_fallback_title: str,
) -> Node:
    media_type = str(raw.get("type") or "").lower()
    if media_type not in ("image", "audio", "video"):
        media_type = meeting_media_type_from_path(url)
    title = clean_media_title(str(raw.get("title") or fallback_title or Path(url).stem))
    ref = {
        "multimedia_id": int_or_zero(raw.get("multimedia_id")),
        "mime_type": str(raw.get("mime_type") or mime_for_meeting_media(url, media_type)),
        "file_path": url,
        "label": title,
        "caption": "",
        "begin_ordinal": 0,
        "key_symbol": raw.get("key_symbol") or raw.get("pub") or "",
        "track": int_or_zero(raw.get("track")),
        "issue_tag": int_or_zero(raw.get("issue_tag") or raw.get("issue")),
        "meps_doc_id": int_or_zero(raw.get("doc_id") or raw.get("meps_doc_id")),
        "meps_language": int_or_zero(raw.get("meps_language")),
        "language": str(raw.get("language") or "").upper(),
        "jw_media_id": str(raw.get("jw_media_id") or ""),
        "section": "",
        "is_song": False,
        "cbs_article_title": "",
    }
    node: Node = {
        "id": node_id,
        "type": "media",
        "title": title or media_fallback_title,
        "media_type": media_type,
        "media_ref": ref,
        "children": [],
        "meeting_generated": False,
        "auto_title": bool(raw.get("auto_title", False)),
    }
    for field in (
        "start_trim_ticks",
        "end_trim_ticks",
        "base_duration_ticks",
    ):
        if raw.get(field) is not None:
            node[field] = int_or_zero(raw.get(field))
    return node
