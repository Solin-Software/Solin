"""JW publication archive transport helpers."""

from __future__ import annotations

import hashlib
import logging
import lzma
import os
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import TracebackType
from typing import IO, Protocol

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
_MAX_CONTENTS_BYTES = 2 * 1024**3


def resolve_jwpub_archive(pub: str, lang: str, issue: str) -> JwpubMediaInfo:
    """
    Query the JW pub-media API for a JWPUB archive.

    not_found=True means the API has no matching JWPUB or rejected this query
    with HTTP 400/404. not_found=False with no URL means a transport, server,
    or decoding failure prevented confirming availability.
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
    expected_checksum: str = "",
) -> None:
    """Stream and validate a JWPUB before atomically replacing ``destination``.

    ``expected_checksum`` is the optional MD5 hex digest supplied by the JW API.
    Progress reports received bytes; completion also requires archive validation.
    """
    try:
        _check_cancelled(cancelled)
        if chunk_size <= 0:
            raise ValueError("Download chunk size must be positive")
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5(usedforsecurity=False)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            buffering=0,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete_on_close=False,
        ) as output:
            temporary_path = Path(output.name)
            with stream_factory(
                url,
                timeout=timeout,
                headers={"User-Agent": DEFAULT_USER_AGENT},
            ) as response:
                content_length = response.headers.get("Content-Length")
                total = int(content_length) if content_length is not None else None
                if total is not None and total < 0:
                    raise ValueError("Invalid Content-Length")
                done = 0
                last_pct = -1
                for chunk in response.iter_bytes(chunk_size):
                    _check_cancelled(cancelled)
                    done += len(chunk)
                    if total is not None and done > total:
                        raise JwpubArchiveDownloadError("Download exceeds Content-Length")
                    if output.write(chunk) != len(chunk):
                        raise OSError("Incomplete archive write")
                    digest.update(chunk)
                    pct = done * 100 // total if total else 0
                    if progress is not None and pct != last_pct:
                        last_pct = pct
                        progress(pct)
                if total is not None and done != total:
                    raise JwpubArchiveDownloadError("Download does not match Content-Length")

            # Close the file before validation/replacement so this also works on Windows.
            output.close()
            _check_cancelled(cancelled)
            if expected_checksum and digest.hexdigest() != expected_checksum.lower():
                raise JwpubArchiveDownloadError("Download checksum does not match expected MD5")
            _validate_jwpub_archive(temporary_path, cancelled)
            _check_cancelled(cancelled)
            os.replace(temporary_path, destination)
    except (HttpError, OSError, ValueError) as exc:
        raise JwpubArchiveDownloadError(str(exc)) from exc


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise JwpubArchiveDownloadError("Download cancelled")


def _validate_jwpub_archive(path: Path, cancelled: Callable[[], bool] | None) -> None:
    try:
        with zipfile.ZipFile(path) as outer, tempfile.TemporaryFile(dir=path.parent) as contents:
            payloads = [member for member in outer.infolist() if member.filename == "contents"]
            if len(payloads) != 1 or payloads[0].is_dir():
                raise zipfile.BadZipFile("Publication archive must have one contents payload")
            if payloads[0].file_size > _MAX_CONTENTS_BYTES:
                raise zipfile.BadZipFile("Publication contents exceeds temporary disk budget")
            _validate_archive_members(outer, cancelled, contents_output=contents)
            contents.seek(0)
            with zipfile.ZipFile(contents) as inner:
                _validate_archive_members(inner, cancelled)
    except JwpubArchiveDownloadError:
        raise
    except (
        EOFError,
        NotImplementedError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        lzma.LZMAError,
    ) as exc:
        raise JwpubArchiveDownloadError(str(exc)) from exc


def _validate_archive_members(
    archive: zipfile.ZipFile,
    cancelled: Callable[[], bool] | None,
    *,
    contents_output: IO[bytes] | None = None,
) -> None:
    contents_size = 0
    for member in archive.infolist():
        _check_cancelled(cancelled)
        with archive.open(member) as payload:
            while True:
                _check_cancelled(cancelled)
                chunk = payload.read(1024 * 1024)
                if not chunk:
                    break
                if contents_output is not None and member.filename == "contents":
                    contents_size += len(chunk)
                    if contents_size > _MAX_CONTENTS_BYTES:
                        raise zipfile.BadZipFile(
                            "Publication contents exceeds temporary disk budget"
                        )
                    contents_output.write(chunk)


__all__ = [
    "JwpubArchiveDownloadError",
    "download_jwpub_archive",
    "resolve_jwpub_archive",
    "resolve_meeting_media",
]
