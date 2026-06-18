from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from solin.core.network.http import (
    BrowserImpersonatingHttpTransport,
    HttpBrowserImpersonationUnavailableError,
    HttpError,
    HttpTransport,
    get_bytes,
)

from .publication_links import (
    DEFAULT_TIMEOUT,
    DEFAULT_USER_AGENT,
    PUB_MEDIA_URL,
    build_pub_media_url,
    fetch_pub_media_json,
    select_pub_media_file,
)

log = logging.getLogger(__name__)


class MemorialDownloadError(RuntimeError):
    """Transport-independent failure while downloading Memorial resources."""


@dataclass(frozen=True, slots=True)
class MemorialJwpubInfo:
    download_url: str | None
    thumbnail_url: str = ""
    checksum: str = ""
    not_found: bool = False


def resolve_memorial_jwpub(pub: str, lang: str) -> MemorialJwpubInfo:
    params = {
        "pub": pub,
        "issue": "0",
        "langwritten": lang,
        "fileformat": "JWPUB",
        "output": "json",
        "alllangs": "0",
        "txtCMSLang": "E",
    }

    any_api_response = False
    data = fetch_pub_media_json(params)
    if data:
        any_api_response = True
        media_file = select_pub_media_file(
            data,
            lang,
            ("JWPUB",),
            fallback_languages=("E",),
        )
        if media_file is not None:
            log.debug(
                "resolve_memorial_jwpub: direct API returned %s/%s",
                pub,
                lang,
            )
            return MemorialJwpubInfo(
                media_file.url,
                media_file.thumbnail_url,
                media_file.checksum,
                False,
            )

    log.warning(
        "resolve_memorial_jwpub: direct API failed for %s/%s; trying fallback",
        pub,
        lang,
    )
    fallback_url = build_pub_media_url(params, PUB_MEDIA_URL)
    data = http_get_json(fallback_url)
    if data:
        any_api_response = True
        media_file = select_pub_media_file(
            data,
            lang,
            ("JWPUB",),
            fallback_languages=("E",),
        )
        if media_file is not None:
            log.debug(
                "resolve_memorial_jwpub: fallback API returned %s/%s",
                pub,
                lang,
            )
            return MemorialJwpubInfo(
                media_file.url,
                media_file.thumbnail_url,
                media_file.checksum,
                False,
            )

    log.error("resolve_memorial_jwpub: no URL for %s/%s", pub, lang)
    return MemorialJwpubInfo(None, "", "", any_api_response)


def download_memorial_bytes(
    url: str,
    timeout: int = 30,
    retries: int = 3,
    *,
    browser_transport: HttpTransport | None = None,
    fallback_transport: HttpTransport | None = None,
) -> bytes:
    last: HttpError | None = None
    impersonating_transport = browser_transport or BrowserImpersonatingHttpTransport()
    for attempt in range(1, retries + 1):
        try:
            try:
                return get_bytes(url, timeout=timeout, transport=impersonating_transport)
            except HttpBrowserImpersonationUnavailableError:
                return get_bytes(
                    url,
                    timeout=timeout,
                    headers={"User-Agent": DEFAULT_USER_AGENT},
                    transport=fallback_transport,
                )
        except HttpError as exc:
            last = exc
            if attempt < retries:
                time.sleep(2.0 ** attempt)
    if last is None:
        raise MemorialDownloadError(f"No download attempt was made for {url}")
    raise MemorialDownloadError(f"Could not download {url}: {last}") from last


def http_get_json(url: str) -> dict | None:
    try:
        data = download_memorial_bytes(url, timeout=DEFAULT_TIMEOUT)
        decoded = json.loads(data.decode())
    except (MemorialDownloadError, UnicodeError, json.JSONDecodeError) as exc:
        log.warning("GET JSON %s -> %s", url, exc)
        return None
    return decoded if isinstance(decoded, dict) else None


__all__ = [
    "MemorialDownloadError",
    "MemorialJwpubInfo",
    "download_memorial_bytes",
    "http_get_json",
    "resolve_memorial_jwpub",
]
