"""Factories and helpers for meeting media tree nodes."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..media.formats import media_type_from_path
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
    return {
        "id": node_id,
        "type": "media",
        "title": display_title,
        "media_type": media_type,
        "media_ref": ref,
        "children": [],
        "meeting_generated": False,
    }
