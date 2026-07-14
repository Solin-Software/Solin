from __future__ import annotations

import logging
import math
import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

from solin.core.network.http import HttpError, HttpStatusError, get_json

log = logging.getLogger(__name__)

PUB_MEDIA_URL = "https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS"
MEDIATOR_MEDIA_ITEM_URL = (
    "https://b.jw-cdn.org/apis/mediator/v1/media-items/{language}/{item_id}"
)
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
    duration_ticks: int = 0


@dataclass(frozen=True, slots=True)
class PublicationMediaRequest:
    key_symbol: str
    track: int | None
    issue_tag: int | None
    meps_doc_id: int | None
    language: str
    is_sign_language: bool = False


@dataclass(frozen=True, slots=True)
class JwpubMediaRequest:
    pub: str
    language: str
    issue: str
    fallback_languages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JwpubMediaInfo:
    download_url: str | None
    checksum: str = ""
    not_found: bool = False


class PublicationMediaResolver:
    """Resolve JW publication media metadata through GETPUBMEDIALINKS."""

    def resolve_video(self, request: PublicationMediaRequest) -> PubMediaFile | None:
        return resolve_publication_video_link(
            request.key_symbol,
            request.track,
            request.issue_tag,
            request.meps_doc_id,
            request.language,
            is_sign_language=request.is_sign_language,
        )

    def resolve_jwpub(self, request: JwpubMediaRequest) -> JwpubMediaInfo:
        data = fetch_pub_media_json({
            "pub": request.pub,
            "issue": request.issue,
            "langwritten": request.language,
            "fileformat": "JWPUB",
            "output": "json",
            "alllangs": "0",
            "txtCMSLang": "E",
        })
        if not data:
            return JwpubMediaInfo(None, "", False)

        media_file = select_pub_media_file(
            data,
            request.language,
            ("JWPUB",),
            fallback_languages=request.fallback_languages,
        )
        if media_file is None:
            return JwpubMediaInfo(None, "", True)
        return JwpubMediaInfo(media_file.url, media_file.checksum, False)


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


def resolve_publication_video_link(
    key_symbol: str,
    track: int | None,
    issue_tag: int | None,
    meps_doc_id: int | None,
    language: str,
    *,
    is_sign_language: bool = False,
) -> PubMediaFile | None:
    """Resolve a JW publication video/audio CDN link from known media identifiers."""
    try:
        track_number = int(track or 0)
        issue_number = int(issue_tag or 0)
        document_id = int(meps_doc_id or 0)
    except (TypeError, ValueError):
        return None

    publication_symbol = key_symbol or ""
    if is_sign_language and publication_symbol.lower() == "sjjm":
        publication_symbol = "sjj"

    if publication_symbol:
        params: dict[str, Any] = {
            "pub": publication_symbol,
            "track": track_number,
            "langwritten": language,
            "fileformat": "mp4,m4v",
            "output": "json",
            "alllangs": "0",
        }
        if issue_number != 0:
            params["issue"] = issue_number
    elif document_id:
        params = {
            "docid": document_id,
            "langwritten": language,
            "fileformat": "mp4,m4v",
            "output": "json",
            "alllangs": "0",
        }
    else:
        return None

    data = fetch_pub_media_json(params)
    if not data:
        return None
    media_file = select_pub_media_file(
        data,
        language,
        VIDEO_FORMATS,
        prefer_highest_label=True,
    )
    if media_file is None or media_file.thumbnail_url:
        return media_file

    media_item = resolve_mediator_media_item(
        publication_symbol,
        track_number,
        issue_number,
        document_id,
        language,
    )
    if media_item is None:
        return media_file
    return replace(
        media_file,
        title=media_file.title or str(media_item.get("title") or ""),
        thumbnail_url=_thumbnail_url(media_item.get("images") or {}),
        duration_ticks=(
            media_file.duration_ticks
            or _media_duration_ticks(media_item, {})
        ),
    )


def resolve_mediator_media_item(
    publication_symbol: str,
    track: int,
    issue: int,
    document_id: int,
    language: str,
) -> dict[str, Any] | None:
    """Return the first matching targeted Mediator item, when available."""
    for item_id in mediator_media_item_ids(
        publication_symbol,
        track,
        issue,
        document_id,
    ):
        url = MEDIATOR_MEDIA_ITEM_URL.format(
            language=urllib.parse.quote(language, safe=""),
            item_id=urllib.parse.quote(item_id, safe=""),
        )
        try:
            data = get_json(
                url,
                timeout=DEFAULT_TIMEOUT,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
        except HttpStatusError as exc:
            log.debug("GET %s -> %s", url, exc)
            if exc.status_code != 404:
                return None
            continue
        except HttpError as exc:
            log.debug("GET %s -> %s", url, exc)
            return None
        media = data.get("media") if isinstance(data, dict) else None
        if not isinstance(media, list) or not media:
            continue
        first = media[0]
        if isinstance(first, dict):
            return first
    return None


def mediator_media_item_ids(
    publication_symbol: str,
    track: int | str,
    issue: int | str | None,
    document_id: int | str | None,
    media_type: str = "VIDEO",
) -> Iterator[str]:
    source = (
        f"pub-{publication_symbol}"
        if publication_symbol
        else f"docid-{document_id}"
    )
    normalized_issue = re.sub(r"(\d{6})00$", r"\1", str(issue)) if issue else ""
    tracks = (str(track), "x", "0", "1")
    seen: set[str] = set()
    for candidate_track in tracks:
        parts = [
            source,
            normalized_issue if publication_symbol and normalized_issue else "",
            candidate_track,
            str(media_type or "VIDEO").upper(),
        ]
        item_id = "_".join(part for part in parts if part)
        if item_id in seen:
            continue
        seen.add(item_id)
        yield item_id


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
        duration_ticks=_media_duration_ticks(item, file_obj),
    )


def _media_duration_ticks(item: dict, file_obj: dict) -> int:
    for raw_duration in (item.get("duration"), file_obj.get("duration")):
        if raw_duration is None or isinstance(raw_duration, bool):
            continue
        try:
            seconds = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return round(seconds * 10_000_000)
    return 0


def _thumbnail_url(images: dict) -> str:
    if not isinstance(images, dict):
        return ""

    def image_url(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and value.get("url"):
            return str(value["url"])
        return ""

    for size in ("sm", "md", "lg"):
        if direct := image_url(images.get(size)):
            return direct

    for shape in ("wss", "lsr", "sqr", "pnr"):
        section = images.get(shape, {})
        if not isinstance(section, dict):
            continue
        for size in ("sm", "md", "lg", "xl"):
            if candidate := image_url(section.get(size)):
                return candidate
    return ""
