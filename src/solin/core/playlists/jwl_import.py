"""Application policies for turning JW Library playlist entries into media."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from solin.core.media.formats import mime_to_ext
from solin.core.media.playback_request import MediaTrim
from solin.core.playlists.items import create_playlist_item


class EmbeddedMediaSaver(Protocol):
    def __call__(
        self,
        data: bytes,
        filename: str,
        identifier: str,
        default_suffix: str,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class JwlPlaylistImportResult:
    items: list[dict[str, Any]]
    skipped_titles: list[str]


def playlist_items_from_jwl_document_items(
    raw_items: Iterable[Mapping[str, Any]],
    *,
    source_name: str,
    save_embedded: EmbeddedMediaSaver,
    section_id: str = "",
    mark_embedded_tmp: bool = False,
) -> JwlPlaylistImportResult:
    """Map parsed JWL items into Solin playlist-item dictionaries.

    The caller owns how embedded media is persisted by providing
    ``save_embedded``.  This keeps the mapping policy independent from profile
    storage, temporary files, and presentation widgets.
    """
    items: list[dict[str, Any]] = []
    skipped_titles: list[str] = []
    fallback_title = Path(source_name).stem or "Item"

    for raw in raw_items:
        media_type = str(raw.get("type") or "video")
        title = str(raw.get("title") or fallback_title)
        url = _raw_item_url(raw)
        attributes = _playlist_attributes(raw)
        if section_id:
            attributes["section_id"] = section_id
        filename = str(raw.get("filename") or "media")
        if filename:
            attributes["original_filename"] = filename

        item = dict(
            create_playlist_item(
                title=title,
                url=url,
                type=media_type,
                **attributes,
            )
        )

        if raw.get("data") and not url:
            data = raw["data"]
            if not isinstance(data, bytes) or not data:
                skipped_titles.append(title)
                continue
            default_suffix = _embedded_default_suffix(raw, media_type)
            try:
                item["url"] = save_embedded(
                    data,
                    filename,
                    str(item["id"]),
                    default_suffix,
                )
            except (OSError, ValueError):
                skipped_titles.append(title)
                continue
            item["type"] = media_type
            if mark_embedded_tmp:
                item["_tmp"] = True
        elif not url:
            skipped_titles.append(title)
            continue

        items.append(item)

    return JwlPlaylistImportResult(items=items, skipped_titles=skipped_titles)


def _raw_item_url(raw: Mapping[str, Any]) -> str:
    return str(raw.get("url") or raw.get("jworg_url") or "")


def _playlist_attributes(raw: Mapping[str, Any]) -> dict[str, Any]:
    attributes = {
        "key_symbol": raw.get("key_symbol"),
        "track": raw.get("track"),
        "issue_tag": raw.get("issue_tag"),
        "doc_id": raw.get("doc_id"),
        "meps_language": raw.get("meps_language", raw.get("language", 0)),
    }
    trim_fields = (
        "start_trim_ticks",
        "end_trim_ticks",
        "base_duration_ticks",
    )
    if any(field in raw for field in trim_fields):
        values = {field: raw.get(field, 0) for field in trim_fields}
        try:
            trim = MediaTrim(**values)
        except (TypeError, ValueError):
            trim = None
        if trim is not None:
            for field in trim_fields:
                if field in raw:
                    attributes[field] = getattr(trim, field)
    for field in (
        "accuracy",
        "end_action",
    ):
        value = raw.get(field)
        if field in raw and isinstance(value, int) and not isinstance(value, bool):
            attributes[field] = value
    return attributes


def _embedded_default_suffix(raw: Mapping[str, Any], media_type: str) -> str:
    filename = str(raw.get("filename") or "")
    suffix = Path(filename).suffix.lower()
    if suffix:
        return suffix
    default_mime = "image/jpeg" if media_type == "image" else "video/mp4"
    return mime_to_ext(str(raw.get("mime_type") or default_mime))
