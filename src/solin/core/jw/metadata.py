"""Remote JW media metadata resolution."""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict
from urllib.parse import urlencode

from solin.core.network.http import HttpError, get_json

from .identifiers import meps_to_lang

log = logging.getLogger(__name__)

JW_API_URL = "https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS"
JW_API_TIMEOUT_SECONDS = 8


class ResolvedMediaMetadata(TypedDict):
    title: str | None
    duration_ticks: int | None


_ResolveKey = tuple[
    str | None,
    int | None,
    int | None,
    int | None,
    int,
    str,
    str,
]
_RESOLVE_CACHE: dict[_ResolveKey, ResolvedMediaMetadata | None] = {}
_QUALITY_LABEL = re.compile(r"^\d+[pP]$|^\d+kbps$", re.IGNORECASE)


def resolve_jworg_meta(
    key_symbol: str | None,
    doc_id: int | None,
    track: int | None,
    issue_tag: int | None,
    meps_language: int,
    fallback_lang: str = "E",
    major_multimedia_type: int | None = None,
) -> ResolvedMediaMetadata | None:
    """Resolve a canonical title and duration through the JW media API."""
    if not key_symbol and not doc_id:
        return None

    file_format = "MP3" if major_multimedia_type == 0 else "MP4"
    language_code = meps_to_lang(meps_language, fallback_lang)
    cache_key: _ResolveKey = (
        key_symbol,
        doc_id,
        track,
        issue_tag,
        meps_language,
        language_code,
        file_format,
    )
    if cache_key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[cache_key]

    params = {
        "langwritten": language_code,
        "fileformat": file_format,
    }
    if key_symbol:
        params["pub"] = key_symbol
    if track is not None:
        params["track"] = str(track)
    if issue_tag:
        params["issue"] = str(issue_tag)
    if doc_id:
        params["docid"] = str(doc_id)

    api_url = f"{JW_API_URL}?{urlencode(params)}"
    log.debug("[jw_api] Resolving JW.org metadata: %s", api_url)

    try:
        data = get_json(
            api_url,
            timeout=JW_API_TIMEOUT_SECONDS,
            headers={"User-Agent": "Solin/1.0"},
        )
    except HttpError as error:
        log.debug(
            "[jw_api] JW.org API unavailable (%s/%s): %s",
            key_symbol,
            doc_id,
            error,
        )
        _RESOLVE_CACHE[cache_key] = None
        return None

    result = _extract_best_meta(data, language_code, file_format, track)
    log.debug("[jw_api] Resolved metadata: %s", result)
    _RESOLVE_CACHE[cache_key] = result
    return result


def _extract_best_meta(
    api_response: dict[str, Any],
    lang_code: str,
    fileformat: str,
    target_track: int | None = None,
) -> ResolvedMediaMetadata | None:
    """Select the best matching playable entry from a JW API response."""
    files = api_response.get("files", {})
    if not isinstance(files, dict):
        return None

    file_format = fileformat.upper()
    language_section = files.get(lang_code, {})
    entries = (
        language_section.get(file_format, [])
        if isinstance(language_section, dict)
        else []
    )
    if not entries:
        for section in files.values():
            if not isinstance(section, dict):
                continue
            candidate = section.get(file_format, [])
            if candidate:
                entries = candidate
                break
    if not isinstance(entries, list) or not entries:
        return None

    def title(entry: dict[str, Any]) -> str | None:
        value = entry.get("title")
        if (
            isinstance(value, str)
            and value.strip()
            and not _QUALITY_LABEL.match(value.strip())
        ):
            return value.strip()
        return None

    def duration(entry: dict[str, Any]) -> int | None:
        file_data = entry.get("file")
        nested_duration = (
            file_data.get("duration")
            if isinstance(file_data, dict)
            else None
        )
        raw = entry.get("duration") or nested_duration
        try:
            return int(float(raw) * 10_000_000) if raw else None
        except (TypeError, ValueError):
            return None

    def has_url(entry: dict[str, Any]) -> bool:
        file_data = entry.get("file")
        nested_url = file_data.get("url") if isinstance(file_data, dict) else None
        return bool(
            nested_url
            or entry.get("progressiveDownloadURL")
            or entry.get("url")
        )

    def track_matches(entry: dict[str, Any]) -> bool:
        if target_track is None:
            return True
        raw_track = entry.get("track")
        if raw_track is None:
            return True
        try:
            return int(raw_track) == target_track
        except (TypeError, ValueError):
            return True

    typed_entries = [entry for entry in entries if isinstance(entry, dict)]
    for entry in typed_entries:
        if (
            has_url(entry)
            and track_matches(entry)
            and entry.get("subtitled") is not True
        ):
            entry_title = title(entry)
            entry_duration = duration(entry)
            if entry_title or entry_duration:
                return {
                    "title": entry_title,
                    "duration_ticks": entry_duration,
                }

    for entry in typed_entries:
        if has_url(entry):
            return {
                "title": title(entry),
                "duration_ticks": duration(entry),
            }

    return None
