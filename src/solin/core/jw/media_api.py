"""
media_api.py — Solin
====================
Centralized client for JW.org media APIs (Songs and Original Songs).

Sign language support:
The JW.org API distinguishes two song collections:
  • sjjm — "Sing Out Joyfully" to Jehovah with music (video with a music track).
  • sjj — "Sing Out Joyfully" to Jehovah (sign language video, no separate track).

Derive `is_sign_language: bool` from `JWLanguageService.is_media_sign_language`
(which reads `isSignLanguage` from the JW API) and pass it to all functions here.

Original Songs (pub=osg):
Optimized endpoint: GETPUBMEDIALINKS with pub=osg.
  • Spoken languages: fileformat=MP3.
  • Sign languages: fileformat=MP4 (the clip is a sign language video).

The osg JSON structure differs from sjjm:
  • pubName is at the root.
  • files > {code} > MP3|MP4 → item list.
  • Each item: title at item level, file > {url}, label for MP4.

Video quality:
VIDEO_PREFERRED_QUALITY and VIDEO_QUALITY_FALLBACK_DIR in constants.py control
the preferred resolution and fallback direction. pick_quality() is the single
decision point for quality selection.

Language fallback:
When the JW media language lacks content and falls back to the interface
language, fallback_is_sign is ALWAYS False: interface languages are never
sign languages. This prevents fallback "T", for example, from fetching sjj
instead of sjjm.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from solin.core.foundation.constants import (
    CACHE_TTL_DAYS,
    VIDEO_PREFERRED_QUALITY,
    VIDEO_QUALITY_FALLBACK_DIR,
    VIDEO_QUALITY_ORDER,
)
from solin.core.network.http import HttpError, get_json as http_get_json

log = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15
_SONG_CACHE_SCHEMA_VERSION = 2

# Common base URL

_JW_PUBMEDIA = (
    "https://b.jw-cdn.org/apis/pub-media/GETPUBMEDIALINKS"
    "?output=json&pub={pub}&fileformat={fmt}&alllangs=0"
    "&langwritten={code}&txtCMSLang={code}"
)

# Mediator endpoint: Original Songs in spoken languages (excludes audio description).
_JW_MEDIATOR_CLIPS = (
    "https://b.jw-cdn.org/apis/mediator/v1/categories/{code}/AudioOriginalSongs"
    "?detailed=1&clientType=www"
)


def song_publication_symbol(is_sign_language: bool) -> str:
    """
    Return the correct song publication symbol:
      • True → 'sjj' (sign language, no music track).
      • False → 'sjjm' (spoken language, with music track).
    All programmatic references to 'sjj'/'sjjm' must use this centralized helper.
    """
    return "sjj" if is_sign_language else "sjjm"


def _songs_fmt(is_sign: bool, audio: bool) -> str:
    """
    Song file format:
      • Sign language → MP4, regardless of audio/video mode.
      • Spoken language, audio mode → MP3.
      • Spoken language, video mode → MP4.
    """
    if is_sign:
        return "MP4"
    return "MP3" if audio else "MP4"


def _clips_fmt(is_sign: bool) -> str:
    """
    osg clip file format:
      • Sign language → MP4.
      • Spoken language → MP3.
    """
    return "MP4" if is_sign else "MP3"


def _build_songs_url(api_code: str, is_sign: bool, audio: bool) -> str:
    pub = song_publication_symbol(is_sign)
    fmt = _songs_fmt(is_sign, audio)
    return _JW_PUBMEDIA.format(pub=pub, fmt=fmt, code=api_code)


def _build_clips_url(api_code: str, is_sign: bool) -> str:
    """Clip endpoint URL for the language type."""
    if is_sign:
        # Sign language: GETPUBMEDIALINKS with pub=osg and MP4.
        fmt = "MP4"
        return _JW_PUBMEDIA.format(pub="osg", fmt=fmt, code=api_code)
    else:
        # Spoken language: mediator endpoint (correctly excludes audio description).
        return _JW_MEDIATOR_CLIPS.format(code=api_code)


def pick_quality(
    items: list[dict],
    preferred: str = VIDEO_PREFERRED_QUALITY,
    fallback_dir: str = VIDEO_QUALITY_FALLBACK_DIR,
) -> str | None:
    """
    Select the ideal quality label from the available items.

    Algorithm (using VIDEO_PREFERRED_QUALITY / VIDEO_QUALITY_FALLBACK_DIR):
      1. Try an exact match for preferred.
      2. Find qualities above and below preferred in VIDEO_QUALITY_ORDER.
      3. fallback_dir='below': try below first, then above (conservative default).
         fallback_dir='above': try above first, then below.
      4. If no known quality exists, return any available label to accommodate
         new resolutions released by JW.
      5. Return None if no items have a label.
    """
    labels_set = {e.get("label") for e in items if isinstance(e, dict) and e.get("label")}
    if not labels_set:
        return None

    # 1. Exact preference
    if preferred in labels_set:
        return preferred

    # 2. Locate preferred in the canonical order.
    order = list(VIDEO_QUALITY_ORDER)
    if preferred in order:
        idx = order.index(preferred)
        above = [q for q in order[:idx] if q in labels_set]  # highest first
        below = [q for q in order[idx + 1 :] if q in labels_set]  # lowest first
    else:
        above = [q for q in order if q in labels_set]
        below = []

    # 3. Order according to fallback_dir.
    ordered = (below + above) if fallback_dir != "above" else (above + below)

    if ordered:
        return ordered[0]

    # 4. Any available label (unknown resolution)
    return next(iter(labels_set))


# ══════════════════════════════════════════════════════════════════════════════
# Songs: video (MP4 / sjjm or sjj)
# ══════════════════════════════════════════════════════════════════════════════


def _cache_path(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> str:
    root = os.fspath(cache_dir)
    os.makedirs(root, exist_ok=True)
    suffix = "_sl" if is_sign else ""
    return os.path.join(root, f"songs_{api_code}{suffix}.json")


def _read_cache_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        log.debug("Could not read JW media cache %s", path, exc_info=True)
        return None

    if not isinstance(data, dict):
        log.debug("Ignoring JW media cache with non-object root: %s", path)
        return None
    return data


def _is_cache_payload_fresh(data: dict | None, content_key: str) -> bool:
    if not data or not data.get(content_key):
        return False
    try:
        fetched_at = float(data.get("_fetched_at", 0))
    except (TypeError, ValueError):
        return False
    age_days = (time.time() - fetched_at) / 86400
    return age_days < CACHE_TTL_DAYS


def _is_cache_valid(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> bool:
    path = _cache_path(api_code, is_sign, cache_dir)
    data = _read_cache_json(path)
    return bool(
        data
        and data.get("_schema_version") == _SONG_CACHE_SCHEMA_VERSION
        and _is_cache_payload_fresh(data, "songs")
    )


def _load_cache(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> tuple:
    data = _read_cache_json(_cache_path(api_code, is_sign, cache_dir))
    if data is None:
        return None, "", 0
    return data.get("songs"), data.get("pub_name", ""), data.get("_fetched_at", 0)


def _save_cache(
    api_code: str,
    is_sign: bool,
    songs: list,
    pub_name: str,
    cache_dir: str | os.PathLike[str],
) -> None:
    path = _cache_path(api_code, is_sign, cache_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "_schema_version": _SONG_CACHE_SCHEMA_VERSION,
                "_fetched_at": time.time(),
                "pub_name": pub_name,
                "songs": songs,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


def parse_songs(data: dict, api_code: str, fmt: str) -> tuple[list, str]:
    """
    Parse a GETPUBMEDIALINKS response (sjjm or sjj) into a song list.

    Use pick_quality() to select video resolution according to
    VIDEO_PREFERRED_QUALITY and VIDEO_QUALITY_FALLBACK_DIR in constants.py.
    For MP3 audio, do not filter labels; deduplicate by number.
    Filter audio description versions by comparing parsed_num with track.
    """
    pub_name = data.get("pubName", "")
    is_audio = fmt.upper() == "MP3"
    try:
        items = data["files"][api_code][fmt.upper()]
    except (KeyError, TypeError):
        return [], pub_name

    # Video: select the best available quality through the centralized policy.
    chosen_label: str | None = None
    if not is_audio:
        chosen_label = pick_quality(items)

    songs: list[dict] = []
    seen_numbers: set[int] = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        if not is_audio:
            # Video: require the selected quality and no subtitles.
            if item.get("label") != chosen_label:
                continue
            if item.get("subtitled") is not False:
                continue
        else:
            # Audio: no subtitles; deduplicate by number.
            if item.get("subtitled") is not False:
                continue

        title = item.get("title", "").strip()
        match = re.match(r"^(\d+)(.*)$", title)
        if not match:
            continue

        num = int(match.group(1))

        # Filter audio description: official track ≠ song number (e.g. 502 ≠ 2).
        track = int(item.get("track", 0))
        if track != num:
            continue

        if is_audio and num in seen_numbers:
            continue

        name = match.group(2).lstrip(".． \t-").strip()
        url = (item.get("file") or {}).get("url", "")
        if not url:
            continue

        duration = item.get("duration", 0) or 0
        track_image = item.get("trackImage")
        if isinstance(track_image, dict):
            thumbnail_url = str(track_image.get("url") or "")
        elif isinstance(track_image, str):
            thumbnail_url = track_image
        else:
            thumbnail_url = ""
        songs.append(
            {
                "number": num,
                "title": name,
                "url": url,
                "duration": duration,
                "thumbnail_url": thumbnail_url,
            }
        )
        if is_audio:
            seen_numbers.add(num)

    return songs, pub_name


def fetch_songs(
    api_code: str,
    force: bool = False,
    fallback_code: str | None = None,
    is_sign_language: bool = False,
    *,
    cache_dir: str | os.PathLike[str],
) -> tuple[list, str, float, bool]:
    """
    Fetch JW songs (MP4 video).

    Parameters
    ----------
    api_code: JW media language code (e.g. 'T', 'ASL', 'BSL').
    force: bypass the cache.
    fallback_code: interface language used when api_code fails;
                   NEVER a sign language (always sjjm).
    is_sign_language: use pub=sjj if True, otherwise pub=sjjm.

    Return (songs, pub_name, fetched_at_timestamp, from_cache).
    """
    if not force and _is_cache_valid(api_code, is_sign_language, cache_dir):
        songs, pub_name, fetched_at = _load_cache(
            api_code,
            is_sign_language,
            cache_dir,
        )
        if songs is not None:
            return songs, pub_name, float(fetched_at), True

    url = _build_songs_url(api_code, is_sign_language, audio=False)
    fmt = _songs_fmt(is_sign_language, audio=False)

    try:
        data = http_get_json(url, timeout=_HTTP_TIMEOUT)
    except HttpError:
        # Fallback: interface language, never a sign language.
        if fallback_code and fallback_code != api_code:
            return fetch_songs(
                fallback_code,
                force=force,
                fallback_code=None,
                is_sign_language=False,
                cache_dir=cache_dir,
            )
        cached_songs, cached_pub_name, cached_at = _load_cache(
            api_code,
            is_sign_language,
            cache_dir,
        )
        if cached_songs is not None:
            return cached_songs, cached_pub_name, float(cached_at), True
        raise

    songs, pub_name = parse_songs(data, api_code, fmt)

    if not songs and fallback_code and fallback_code != api_code:
        return fetch_songs(
            fallback_code,
            force=force,
            fallback_code=None,
            is_sign_language=False,
            cache_dir=cache_dir,
        )

    _save_cache(api_code, is_sign_language, songs, pub_name, cache_dir)
    return songs, pub_name, time.time(), False


# ══════════════════════════════════════════════════════════════════════════════
# Songs: audio (MP3, or MP4 for sign languages; sjjm or sjj)
# ══════════════════════════════════════════════════════════════════════════════


def _songs_audio_cache_path(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> str:
    root = os.fspath(cache_dir)
    os.makedirs(root, exist_ok=True)
    suffix = "_sl" if is_sign else ""
    return os.path.join(root, f"songs_audio_{api_code}{suffix}.json")


def _is_songs_audio_cache_valid(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> bool:
    path = _songs_audio_cache_path(api_code, is_sign, cache_dir)
    return _is_cache_payload_fresh(_read_cache_json(path), "songs")


def _load_songs_audio_cache(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> tuple:
    data = _read_cache_json(_songs_audio_cache_path(api_code, is_sign, cache_dir))
    if data is None:
        return None, "", 0
    return data.get("songs"), data.get("pub_name", ""), data.get("_fetched_at", 0)


def _save_songs_audio_cache(
    api_code: str,
    is_sign: bool,
    songs: list,
    pub_name: str,
    cache_dir: str | os.PathLike[str],
) -> None:
    path = _songs_audio_cache_path(api_code, is_sign, cache_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"_fetched_at": time.time(), "pub_name": pub_name, "songs": songs},
            f,
            ensure_ascii=False,
            indent=2,
        )


def fetch_songs_audio(
    api_code: str,
    force: bool = False,
    fallback_code: str | None = None,
    is_sign_language: bool = False,
    *,
    cache_dir: str | os.PathLike[str],
) -> tuple[list, str, float, bool]:
    """
    Fetch JW songs in audio mode.

    Spoken languages: pub=sjjm, fileformat=MP3.
    Sign languages: pub=sjj, fileformat=MP4 (no separate audio;
    the "audio" is the sign language video itself).

    Parameters are identical to fetch_songs.
    """
    if not force and _is_songs_audio_cache_valid(api_code, is_sign_language, cache_dir):
        songs, pub_name, fetched_at = _load_songs_audio_cache(
            api_code,
            is_sign_language,
            cache_dir,
        )
        if songs is not None:
            return songs, pub_name, float(fetched_at), True

    url = _build_songs_url(api_code, is_sign_language, audio=True)
    fmt = _songs_fmt(is_sign_language, audio=True)

    try:
        data = http_get_json(url, timeout=_HTTP_TIMEOUT)
    except HttpError:
        if fallback_code and fallback_code != api_code:
            return fetch_songs_audio(
                fallback_code,
                force=force,
                fallback_code=None,
                is_sign_language=False,
                cache_dir=cache_dir,
            )
        raise

    songs, pub_name = parse_songs(data, api_code, fmt)

    if not songs and fallback_code and fallback_code != api_code:
        return fetch_songs_audio(
            fallback_code,
            force=force,
            fallback_code=None,
            is_sign_language=False,
            cache_dir=cache_dir,
        )
    _save_songs_audio_cache(api_code, is_sign_language, songs, pub_name, cache_dir)
    return songs, pub_name, time.time(), False


# ══════════════════════════════════════════════════════════════════════════════
# Original Songs (pub=osg)
# ══════════════════════════════════════════════════════════════════════════════


def _clips_cache_path(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> str:
    root = os.fspath(cache_dir)
    os.makedirs(root, exist_ok=True)
    suffix = "_sl" if is_sign else ""
    return os.path.join(root, f"clips_{api_code}{suffix}.json")


def _is_clips_cache_valid(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> bool:
    path = _clips_cache_path(api_code, is_sign, cache_dir)
    return _is_cache_payload_fresh(_read_cache_json(path), "clips")


def _load_clips_cache(
    api_code: str,
    is_sign: bool,
    cache_dir: str | os.PathLike[str],
) -> tuple:
    data = _read_cache_json(_clips_cache_path(api_code, is_sign, cache_dir))
    if data is None:
        return None, 0
    return data.get("clips"), data.get("_fetched_at", 0)


def _save_clips_cache(
    api_code: str,
    is_sign: bool,
    clips: list,
    cache_dir: str | os.PathLike[str],
) -> None:
    path = _clips_cache_path(api_code, is_sign, cache_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"_fetched_at": time.time(), "clips": clips},
            f,
            ensure_ascii=False,
            indent=2,
        )


def _parse_clips_mediator(data: dict) -> list:
    """
    Parse the mediator endpoint (/mediator/v1/categories/{code}/AudioOriginalSongs).

    Used for spoken languages. Filtering subtitled=False makes the mediator
    return only Original Songs without audio description.

    Structure:
      data["category"]["media"] → item list
      item["title"] → "The Best Life Ever"
      item["duration"] → 251.884
      item["files"][] → {"progressiveDownloadURL": "...", "subtitled": bool}
    """
    clips: list[dict] = []
    try:
        media_items = data["category"]["media"]
    except (KeyError, TypeError):
        return clips

    for item in media_items:
        if not isinstance(item, dict):
            continue
        title = item.get("title", "").strip()
        duration = item.get("duration", 0) or 0
        url = ""
        for f in item.get("files", []):
            if not isinstance(f, dict):
                continue
            if f.get("subtitled") is not False:
                continue
            candidate = f.get("progressiveDownloadURL", "")
            if candidate:
                url = candidate
                break
        if title and url:
            clips.append({"title": title, "url": url, "duration": duration})

    return clips


def parse_clips_osg(data: dict, api_code: str) -> list:
    """
    Parse GETPUBMEDIALINKS (pub=osg, fileformat=MP4).

    Used for sign languages. Select the best resolution via pick_quality()
    and reverse the list: osg returns oldest first, so reversing shows
    the latest releases first.

    Structure:
      data["files"][api_code]["MP4"] → item list
      item["title"] → "The Best Life Ever"
      item["label"] → "720p"
      item["file"]["url"] → MP4 URL
    """
    clips: list[dict] = []
    try:
        items = data["files"][api_code]["MP4"]
    except (KeyError, TypeError):
        return clips

    chosen_label = pick_quality(items)

    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("label") != chosen_label:
            continue
        title = item.get("title", "").strip()
        if not title:
            continue
        url = (item.get("file") or {}).get("url", "")
        if not url:
            continue
        duration = item.get("duration", 0) or 0
        clips.append({"title": title, "url": url, "duration": duration})

    # Reverse: osg orders oldest to newest; show newest first.
    clips.reverse()
    return clips


def fetch_clips(
    api_code: str,
    force: bool = False,
    fallback_code: str | None = None,
    is_sign_language: bool = False,
    *,
    cache_dir: str | os.PathLike[str],
) -> tuple[list, float, bool]:
    """
    Fetch Original Songs.

    • Spoken languages → mediator endpoint (/mediator/…/AudioOriginalSongs);
      exclude audio description via subtitled=False.
    • Sign languages → osg endpoint (GETPUBMEDIALINKS, pub=osg, fileformat=MP4);
      select resolution via pick_quality() and reverse to show newest first
      because osg returns oldest first.

    Always fall back to the interface language with is_sign_language=False.
    Return (clips, fetched_at_timestamp, from_cache).
    """
    if not force and _is_clips_cache_valid(api_code, is_sign_language, cache_dir):
        clips, fetched_at = _load_clips_cache(api_code, is_sign_language, cache_dir)
        if clips is not None:
            return clips, float(fetched_at), True

    url = _build_clips_url(api_code, is_sign_language)

    try:
        data = http_get_json(url, timeout=_HTTP_TIMEOUT)
    except HttpError:
        if fallback_code and fallback_code != api_code:
            return fetch_clips(
                fallback_code,
                force=force,
                fallback_code=None,
                is_sign_language=False,
                cache_dir=cache_dir,
            )
        raise

    if is_sign_language:
        clips = parse_clips_osg(data, api_code)
    else:
        clips = _parse_clips_mediator(data)

    if not clips and fallback_code and fallback_code != api_code:
        return fetch_clips(
            fallback_code,
            force=force,
            fallback_code=None,
            is_sign_language=False,
            cache_dir=cache_dir,
        )

    _save_clips_cache(api_code, is_sign_language, clips, cache_dir)
    return clips, time.time(), False


# ══════════════════════════════════════════════════════════════════════════════
# Cache utilities (used by settings_widget, etc.)
# ══════════════════════════════════════════════════════════════════════════════


def get_cache_date(
    api_code: str,
    is_sign_language: bool = False,
    *,
    cache_dir: str | os.PathLike[str],
) -> float | None:
    """Return the video song cache timestamp, if it exists."""
    data = _read_cache_json(_cache_path(api_code, is_sign_language, cache_dir))
    if data is None:
        return None
    ts = data.get("_fetched_at", 0)
    try:
        return float(ts) if ts else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "fetch_clips",
    "fetch_songs",
    "fetch_songs_audio",
    "get_cache_date",
    "parse_clips_osg",
    "parse_songs",
    "pick_quality",
    "song_publication_symbol",
]
