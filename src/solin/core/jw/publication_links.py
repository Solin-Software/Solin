from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

from solin.core.network.http import HttpError, get_json

log = logging.getLogger(__name__)

PUB_MEDIA_URL = "https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS"
DEFAULT_USER_AGENT = "Mozilla/5.0"
DEFAULT_TIMEOUT = 20
VIDEO_FORMATS = ("MP4", "M4V", "mp4", "m4v")


@dataclass(frozen=True, slots=True)
class PubMediaFile:
    url: str
    title: str = ""
    checksum: str = ""
    thumbnail_url: str = ""
    label: str = ""


def build_pub_media_url(params: dict[str, Any], base_url: str = PUB_MEDIA_URL) -> str:
    return f"{base_url}?{urllib.parse.urlencode(params)}" if params else base_url


def fetch_pub_media_json(
    params: dict[str, Any],
    *,
    base_url: str = PUB_MEDIA_URL,
    timeout: int = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict | None:
    url = build_pub_media_url(params, base_url)
    try:
        data = get_json(url, timeout=timeout, headers={"User-Agent": user_agent})
    except HttpError as exc:
        log.warning("GET %s -> %s", url, exc)
        return None

    return data if isinstance(data, dict) else None


def select_pub_media_file(
    data: dict,
    language: str,
    file_formats: tuple[str, ...],
    *,
    fallback_languages: tuple[str, ...] = (),
    prefer_highest_label: bool = False,
) -> PubMediaFile | None:
    files_root = data.get("files", {})
    if not isinstance(files_root, dict):
        return None

    languages = _dedupe_nonempty((language, *fallback_languages))
    for lang in languages:
        lang_files = files_root.get(lang)
        if not isinstance(lang_files, dict):
            continue

        for file_format in file_formats:
            items = lang_files.get(file_format, [])
            if not isinstance(items, list):
                continue
            candidates = [item for item in items if isinstance(item, dict)]
            if not candidates:
                continue
            item = _best_labeled_item(candidates) if prefer_highest_label else candidates[0]
            media_file = _pub_media_file_from_item(data, item)
            if media_file is not None:
                return media_file
    return None


def _dedupe_nonempty(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(out)


def _best_labeled_item(items: list[dict]) -> dict:
    return sorted(items, key=lambda item: str(item.get("label", "0")), reverse=True)[0]


def _pub_media_file_from_item(data: dict, item: dict) -> PubMediaFile | None:
    file_obj = item.get("file", {})
    if not isinstance(file_obj, dict):
        return None

    url = str(file_obj.get("url") or "")
    if not url:
        return None

    title = str(item.get("title") or data.get("pubName") or "")
    checksum = str(file_obj.get("checksum") or "")
    label = str(item.get("label") or "")
    images = item.get("images", {})
    thumbnail_url = _thumbnail_url(images if isinstance(images, dict) else {})
    return PubMediaFile(
        url=url,
        title=title,
        checksum=checksum,
        thumbnail_url=thumbnail_url,
        label=label,
    )


def _thumbnail_url(images: dict) -> str:
    for size in ("sm", "md", "lg"):
        direct = images.get(size, {})
        if isinstance(direct, dict) and direct.get("url"):
            return str(direct["url"])

    for shape in ("sqr", "wss", "lsr"):
        section = images.get(shape, {})
        if not isinstance(section, dict):
            continue
        for size in ("sm", "md", "lg"):
            candidate = section.get(size, {})
            if isinstance(candidate, dict) and candidate.get("url"):
                return str(candidate["url"])
    return ""
