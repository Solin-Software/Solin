from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from solin.core.network.http import get_bytes

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


def download_memorial_bytes(url: str, timeout: int = 30, retries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            content = _http_get_with_browser_impersonation(url, timeout)
            if content is not None:
                return content
            return get_bytes(
                url,
                timeout=timeout,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
        except Exception as exc:  # noqa: BLE001 - curl_cffi/HTTP transport boundary
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


def _http_get_with_browser_impersonation(url: str, timeout: int) -> bytes | None:
    try:
        from curl_cffi import requests  # type: ignore[reportMissingImports]
    except ImportError:
        return None

    response = requests.get(
        url,
        headers=_chrome_headers(),
        timeout=timeout,
        impersonate="chrome124",
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.content


def _chrome_headers() -> dict[str, str]:
    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": (
            '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"'
        ),
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


__all__ = [
    "MemorialDownloadError",
    "MemorialJwpubInfo",
    "download_memorial_bytes",
    "http_get_json",
    "resolve_memorial_jwpub",
]
