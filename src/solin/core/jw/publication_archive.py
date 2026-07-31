"""JW publication archive transport helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import TracebackType
from typing import Protocol

from solin.core.network.http import HttpError, stream_get

from .publication_links import (
    DEFAULT_USER_AGENT,
    JwpubMediaInfo,
    JwpubMediaRequest,
    PublicationMediaRequest,
    PublicationMediaResolver,
)

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int], None]


class JwpubArchiveDownloadError(RuntimeError):
    """Transport-independent failure while downloading a JWPUB archive."""


class ArchiveStream(Protocol):
    @property
    def headers(self) -> Mapping[str, str]: ...

    def __enter__(self) -> ArchiveStream: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def iter_bytes(self, chunk_size: int) -> Iterable[bytes]: ...


StreamFactory = Callable[..., ArchiveStream]

_PUBLICATION_MEDIA_RESOLVER = PublicationMediaResolver()


def resolve_jwpub_archive(pub: str, lang: str, issue: str) -> JwpubMediaInfo:
    """
    Query the JW pub-media API for a JWPUB archive.

    not_found=True means the API responded successfully but has no files for
    this publication/language/issue. not_found=False with no URL means a network
    or parse error prevented confirming availability.
    """
    return _PUBLICATION_MEDIA_RESOLVER.resolve_jwpub(
        JwpubMediaRequest(pub=pub, language=lang, issue=issue)
    )


def resolve_meeting_media(
    key_symbol: str,
    track: int,
    issue_tag: int,
    meps_doc_id: int,
    lang: str,
    is_sign_language: bool = False,
    media_type: str = "video",
) -> dict:
    """
    Resolve a meeting media URL from JW publication media identifiers.

    Sign-language songbooks use ``sjj`` instead of the music edition ``sjjm``.
    """
    result = {"url": "", "title": "", "thumbnail": "", "duration_ticks": 0}
    try:
        media_file = _PUBLICATION_MEDIA_RESOLVER.resolve_media(
            PublicationMediaRequest(
                key_symbol=key_symbol,
                track=track,
                issue_tag=issue_tag,
                meps_doc_id=meps_doc_id,
                language=lang,
                is_sign_language=is_sign_language,
                media_type=media_type,
            )
        )
        if media_file is not None:
            result["url"] = media_file.url
            result["title"] = media_file.title
            result["thumbnail"] = media_file.thumbnail_url
            result["duration_ticks"] = media_file.duration_ticks
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        log.debug("Could not parse resolved meeting media metadata", exc_info=True)
    return result


def download_jwpub_archive(
    url: str,
    destination: Path,
    *,
    progress: ProgressCallback | None = None,
    stream_factory: StreamFactory = stream_get,
    timeout: int = 60,
    chunk_size: int = 256 * 1024,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Download a JWPUB archive to ``destination`` and report integer progress."""
    try:
        if cancelled is not None and cancelled():
            raise JwpubArchiveDownloadError("Download cancelled")
        with stream_factory(
            url,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            chunks: list[bytes] = []
            last_pct = -1
            for chunk in response.iter_bytes(chunk_size):
                if cancelled is not None and cancelled():
                    raise JwpubArchiveDownloadError("Download cancelled")
                chunks.append(chunk)
                done += len(chunk)
                pct = int(done / total * 100) if total else 0
                if progress is not None and pct != last_pct:
                    last_pct = pct
                    progress(pct)

        if cancelled is not None and cancelled():
            raise JwpubArchiveDownloadError("Download cancelled")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"".join(chunks))
    except (HttpError, OSError, ValueError) as exc:
        raise JwpubArchiveDownloadError(str(exc)) from exc


__all__ = [
    "JwpubArchiveDownloadError",
    "download_jwpub_archive",
    "resolve_jwpub_archive",
    "resolve_meeting_media",
]
